"""Shared helpers for the deterministic file generators (五期 B+ 多类型扩展).

Every generator (``xlsx_generator`` / ``docx_generator`` / ``csv_generator`` /
``html_generator``) follows the same contract as the original
``pptx_generator``: the model emits a *structured payload* (A 铁律 — never
rendering code), and a fixed, code-controlled generator turns it into real file
bytes. These helpers centralize the defensive normalization those generators
share so each module stays small and focused on its own format.

Generic / open-source safe: no company names, internal services, or private
identifiers. The single accent color defaults to the product green but is
env-overridable with a neutral fallback.

``pptx_generator`` predates this module and keeps its own private copies of the
same helpers (it is already in production — left untouched on purpose). New
generators import from here.

Public API:
    clean_text(value, limit) -> str            # coerce arbitrary model output → trimmed str
    clamp_int(value, default, lo, hi) -> int   # tolerant int with bounds
    as_cell(value, limit) -> str | int | float | bool   # table-cell coercion (keeps numbers)
    safe_filename(title, ext) -> str           # filesystem/Content-Disposition-safe name
    accent_hex() -> str                        # 6-hex accent (no '#'), validated
"""

from __future__ import annotations

import os
import re
from typing import Any

# Single product accent (green). Env-overridable, neutral-validated default so
# this module stays generic / open-source safe.
_DEFAULT_ACCENT = "008042"


def accent_hex() -> str:
    """Return a validated 6-hex accent color (no leading '#').

    Reads ``FILE_GEN_ACCENT_COLOR`` then falls back to ``PPTX_ACCENT_COLOR``
    (shared with the existing PPTX renderer) then the product green.
    """
    raw = (
        os.environ.get("FILE_GEN_ACCENT_COLOR")
        or os.environ.get("PPTX_ACCENT_COLOR")
        or _DEFAULT_ACCENT
    ).strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
        raw = _DEFAULT_ACCENT
    return raw.upper()


def clean_text(value: Any, limit: int) -> str:
    """Coerce arbitrary model output to a single trimmed string, length-capped.

    Strips control characters that would break XML/Office payloads, collapses
    internal whitespace runs, and appends an ellipsis when truncated. Newlines
    are preserved (callers that want single-line should pass pre-joined text).
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    # drop control chars except newline/tab that break XML
    value = "".join(ch for ch in value if ch in "\n\t" or ch >= " ")
    value = re.sub(r"[ \t]+", " ", value).strip()
    if limit > 0 and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """Tolerant int coercion clamped to ``[lo, hi]`` (returns ``default`` on junk)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return max(lo, min(default, hi))
    return max(lo, min(n, hi))


# Numeric strings the model commonly emits in table cells; kept as numbers so
# spreadsheets stay sortable / summable instead of becoming text.
_INT_RE = re.compile(r"^-?\d{1,15}$")
_FLOAT_RE = re.compile(r"^-?\d{1,15}\.\d{1,12}$")


def as_cell(value: Any, limit: int = 2000) -> Any:
    """Coerce a model-supplied table cell to a spreadsheet-friendly scalar.

    - ``None`` → ``""`` (blank cell)
    - ``bool`` / ``int`` / ``float`` → kept as-is (numbers stay numeric)
    - numeric-looking strings → parsed to int/float (so cells sort/sum)
    - everything else → ``clean_text``
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if _INT_RE.match(s):
            try:
                return int(s)
            except ValueError:
                pass
        elif _FLOAT_RE.match(s):
            try:
                return float(s)
            except ValueError:
                pass
        return clean_text(value, limit)
    # lists / dicts / other → stringify defensively
    return clean_text(value, limit)


# Keep CJK + word chars, drop the rest; mirrors pptx_generator.safe_filename so
# downloaded names are consistent across file types.
_SLUG_RE = re.compile(r"[^\w一-鿿\- ]+", re.UNICODE)


def safe_filename(title: str, ext: str, fallback: str = "file") -> str:
    """Derive a filesystem / Content-Disposition-safe filename from a title.

    Keeps CJK + word chars, collapses the rest, caps length, guarantees ``ext``.
    """
    base = _SLUG_RE.sub("", title or "").strip()
    base = re.sub(r"\s+", " ", base)
    if not base:
        base = fallback
    if len(base) > 60:
        base = base[:60].rstrip()
    return f"{base}.{ext.lstrip('.')}"
