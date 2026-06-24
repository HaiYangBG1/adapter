"""Deterministic plain-text generator (五期 B 文件能力优化 · B14).

The model supplies a raw ``body`` (plain text) or a structured ``sections``
payload (docx shape, defensive fallback); this module writes ``.txt`` bytes
using only the standard library. The model never writes rendering code
(A 铁律 — plain text is content).

Free-form body is cleaned *gently* (control chars dropped except ``\n``/``\t``,
newlines normalized, length-capped) so the user's intended line breaks survive;
unlike ``clean_text`` it does not collapse internal whitespace.

Public API:
    normalize_txt(raw) -> dict
    build_txt(data) -> bytes           # → .txt bytes (UTF-8)
    safe_filename(title, ext="txt")
"""

from __future__ import annotations

from typing import Any

import file_gen_common as common

MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 200_000
MAX_SECTIONS = 100
MAX_ITEMS_PER_SECTION = 200
MAX_BLOCK_CHARS = 20_000
MAX_HEADING_CHARS = 200
MAX_BULLET_CHARS = 2000


def _clean_body(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n\n".join(str(x) for x in value)
    if not isinstance(value, str):
        value = str(value)
    value = "".join(ch for ch in value if ch in "\n\t" or ch >= " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if limit > 0 and len(value) > limit:
        value = value[:limit].rstrip() + "\n\n…(内容过长已截断)"
    return value


def _sections_from(raw: dict[str, Any]) -> list[tuple[str, list[str], list[str]]]:
    out: list[tuple[str, list[str], list[str]]] = []
    raw_sections = raw.get("sections") or raw.get("blocks")
    if not isinstance(raw_sections, list):
        return out
    for s in raw_sections:
        if isinstance(s, str):
            if s.strip():
                out.append(("", [_clean_body(s, MAX_BLOCK_CHARS)], []))
        elif isinstance(s, dict):
            heading = common.clean_text(s.get("heading") or s.get("title"), MAX_HEADING_CHARS)
            raw_paras = s.get("paragraphs") or s.get("body") or s.get("text") or []
            if isinstance(raw_paras, str):
                raw_paras = [raw_paras]
            paras = [
                _clean_body(p, MAX_BLOCK_CHARS)
                for p in (raw_paras if isinstance(raw_paras, list) else [])
                if str(p).strip()
            ][:MAX_ITEMS_PER_SECTION]
            raw_bullets = s.get("bullets") or s.get("points") or s.get("items") or []
            if isinstance(raw_bullets, str):
                raw_bullets = [raw_bullets]
            bullets = [
                common.clean_text(b, MAX_BULLET_CHARS)
                for b in (raw_bullets if isinstance(raw_bullets, list) else [])
                if str(b).strip()
            ][:MAX_ITEMS_PER_SECTION]
            if heading or paras or bullets:
                out.append((heading, paras, bullets))
        if len(out) >= MAX_SECTIONS:
            break
    return out


def normalize_txt(raw: Any) -> dict[str, Any]:
    """Canonical shape: ``{"title": str, "body": str, "sections": [...]}``.
    ``body`` (raw text) takes precedence; ``sections`` is a structured fallback.
    """
    if not isinstance(raw, dict):
        raw = {}
    title = common.clean_text(raw.get("title") or raw.get("name"), MAX_TITLE_CHARS) or "未命名"
    body = _clean_body(
        raw.get("body") or raw.get("content") or raw.get("text"),
        MAX_BODY_CHARS,
    )
    sections = _sections_from(raw)
    return {"title": title, "body": body, "sections": sections}


def build_txt(data: Any) -> bytes:
    """Render a (canonical or loose) doc into ``.txt`` bytes (UTF-8)."""
    data = normalize_txt(data)
    title, body, sections = data["title"], data["body"], data["sections"]
    out: list[str] = [title, ""]
    if body:
        out.append(body)
    elif sections:
        for heading, paras, bullets in sections:
            if heading:
                out.append(heading)
                out.append("")
            for p in paras:
                out.append(p)
                out.append("")
            for b in bullets:
                out.append(f"  • {b}")
            if bullets:
                out.append("")
    text = "\n".join(out).strip() + "\n"
    return text.encode("utf-8")


def safe_filename(title: str, ext: str = "txt") -> str:
    return common.safe_filename(title, ext, fallback="文本")


if __name__ == "__main__":  # pragma: no cover — local smoke test
    import sys

    sample = {
        "title": "会议纪要 2026-06-24",
        "body": "参会:PM、全栈、测试\n\n决议:\n1. 文件能力做标准包\n2. 先 B14 后 B15\n",
    }
    data = build_txt(sample)
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sample.txt"
    with open(out, "wb") as f:
        f.write(data)
    print(f"OK: {len(data)} bytes → {out}")
    print(f"filename: {safe_filename(sample['title'])}")
    data2 = build_txt({"title": "结构化", "sections": [
        {"heading": "目标", "bullets": ["补 md/txt", "修预览"]},
    ]})
    assert "• 补 md/txt".encode() in data2
    print(f"fallback OK: {len(data2)} bytes")
