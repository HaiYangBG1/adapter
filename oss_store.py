"""Object-storage layer for generated file artifacts (S3/OSS-compatible).

Uploads a generated file to an object-storage bucket and returns a short-lived
presigned GET URL the browser can download directly.

Generic / open-source safe: every endpoint, bucket, prefix and credential is read
from the environment — nothing about a specific provider, bucket or account is
hard-coded. Credentials live only in the runtime environment; they are never
written to disk, logs, or responses.

Two-endpoint awareness (the detail that bites in production):
  - Uploads run server→storage and should use the *internal* endpoint when the
    service sits inside the provider's network (faster, no egress cost).
  - Presigned URLs are consumed by the end user's *browser* and must therefore be
    signed against a *public* endpoint, or they won't resolve outside the network.
Set ``OSS_INTERNAL_ENDPOINT`` (upload) and ``OSS_PUBLIC_ENDPOINT`` (presign); a
single ``OSS_ENDPOINT`` is used as the fallback for both.

Object key scheme (MVP): ``{prefix}{artifact_id}.{ext}`` — deterministic, so the
re-sign endpoint can rebuild the key from the id + extension without a side table.

Environment:
    OSS_ENDPOINT             generic endpoint fallback (e.g. https://oss-<region>.example.com)
    OSS_INTERNAL_ENDPOINT    upload endpoint (defaults to OSS_ENDPOINT)
    OSS_PUBLIC_ENDPOINT      presign endpoint, browser-facing (defaults to OSS_ENDPOINT)
    OSS_BUCKET               bucket name
    OSS_ACCESS_KEY_ID        credential id        🔴 env only, never persisted
    OSS_ACCESS_KEY_SECRET    credential secret    🔴 env only, never persisted
    OSS_ARTIFACT_PREFIX      key prefix (default "ai-center/artifacts/")
    OSS_PRESIGN_EXPIRE_SECONDS  presigned URL TTL (default 900 = 15 min)
"""

from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import quote

try:  # oss2 is optional at import time; absence degrades gracefully
    import oss2  # type: ignore
except ImportError:  # pragma: no cover - exercised only where the dep is absent
    oss2 = None  # type: ignore


class OssNotConfigured(RuntimeError):
    """Raised when object storage is unavailable (dep missing or env incomplete).

    Callers should catch this and surface a user-facing artifact error rather than
    letting it crash the request.
    """


DEFAULT_PREFIX = "ai-center/artifacts/"
DEFAULT_EXPIRE = 900


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _prefix() -> str:
    p = _env("OSS_ARTIFACT_PREFIX") or DEFAULT_PREFIX
    return p if p.endswith("/") else p + "/"


def _expire_seconds() -> int:
    try:
        v = int(_env("OSS_PRESIGN_EXPIRE_SECONDS") or DEFAULT_EXPIRE)
    except ValueError:
        v = DEFAULT_EXPIRE
    return max(60, min(v, 3600))


def is_configured() -> bool:
    """True when the dependency is importable and the required env is present."""
    if oss2 is None:
        return False
    have_endpoint = bool(_env("OSS_ENDPOINT") or _env("OSS_INTERNAL_ENDPOINT") or _env("OSS_PUBLIC_ENDPOINT"))
    return have_endpoint and bool(_env("OSS_BUCKET") and _env("OSS_ACCESS_KEY_ID") and _env("OSS_ACCESS_KEY_SECRET"))


def status() -> dict[str, object]:
    """Non-sensitive readiness snapshot for /health (never includes secrets)."""
    return {
        "configured": is_configured(),
        "dep_present": oss2 is not None,
        "bucket_set": bool(_env("OSS_BUCKET")),
        "internal_endpoint_set": bool(_env("OSS_INTERNAL_ENDPOINT") or _env("OSS_ENDPOINT")),
        "public_endpoint_set": bool(_env("OSS_PUBLIC_ENDPOINT") or _env("OSS_ENDPOINT")),
        "prefix": _prefix(),
        "presign_ttl_s": _expire_seconds(),
    }


_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def object_key_for(artifact_id: str, ext: str = "pptx") -> str:
    """Deterministic object key: ``{prefix}{id}.{ext}``.

    The id is sanitized so it can't traverse prefixes: all ``.`` are stripped
    from the id segment (uuids are hex, so unaffected), preventing ``..`` from
    surviving and constructing an unintended key/prefix.
    """
    # strip every "." from the id (not just runs) so no ".." can survive
    safe_id = _KEY_SAFE_RE.sub("", str(artifact_id or "")).replace(".", "") or "artifact"
    ext = _KEY_SAFE_RE.sub("", str(ext or "pptx")).lstrip(".") or "pptx"
    return f"{_prefix()}{safe_id}.{ext}"


def _bucket(endpoint: str):
    if oss2 is None:
        raise OssNotConfigured("oss2 not installed")
    if not endpoint:
        raise OssNotConfigured("no endpoint configured")
    auth = oss2.Auth(_env("OSS_ACCESS_KEY_ID"), _env("OSS_ACCESS_KEY_SECRET"))
    return oss2.Bucket(auth, endpoint, _env("OSS_BUCKET"))


def _upload_bucket():
    return _bucket(_env("OSS_INTERNAL_ENDPOINT") or _env("OSS_ENDPOINT"))


def _presign_bucket():
    return _bucket(_env("OSS_PUBLIC_ENDPOINT") or _env("OSS_ENDPOINT"))


def _content_disposition(filename: str) -> str:
    """RFC 5987 attachment disposition supporting non-ASCII (CJK) filenames."""
    ascii_fallback = re.sub(r'[^\x20-\x7e]', "_", filename or "download").replace('"', "")
    if not ascii_fallback.strip("_ "):
        ascii_fallback = "download"
    encoded = quote(filename or "download", safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def upload_bytes(artifact_id: str, ext: str, data: bytes, mime: str) -> str:
    """Upload bytes to the bucket; return the object key. Raises OssNotConfigured."""
    if not is_configured():
        raise OssNotConfigured("object storage not configured")
    key = object_key_for(artifact_id, ext)
    headers = {"Content-Type": mime} if mime else None
    _upload_bucket().put_object(key, data, headers=headers)
    return key


def presign_get(object_key: str, filename: Optional[str] = None, mime: Optional[str] = None) -> str:
    """Return a short-lived presigned GET URL (browser-facing public endpoint).

    Adds a Content-Disposition so the browser downloads with the friendly name.

    NOTE: we deliberately do NOT set ``response-content-type`` — OSS rejects
    overriding content-type on a GET ("Can not override response header on
    content-type", 400 InvalidRequest), and it is redundant anyway: the object is
    uploaded (``upload_bytes``) with the correct ``Content-Type`` header, so OSS
    already serves it with the right kind. The ``mime`` arg is kept for call-site
    compatibility but intentionally unused here.
    """
    if not is_configured():
        raise OssNotConfigured("object storage not configured")
    _ = mime  # intentionally unused — see note above
    params: dict[str, str] = {}
    if filename:
        params["response-content-disposition"] = _content_disposition(filename)
    return _presign_bucket().sign_url(
        "GET", object_key, _expire_seconds(), params=params or None, slash_safe=True
    )


def upload_and_presign(artifact_id: str, ext: str, data: bytes, mime: str, filename: str) -> tuple[str, str, int]:
    """Convenience: upload then presign. Returns (download_url, object_key, ttl_s)."""
    key = upload_bytes(artifact_id, ext, data, mime)
    url = presign_get(key, filename=filename, mime=mime)
    return url, key, _expire_seconds()
