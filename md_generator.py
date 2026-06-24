"""Deterministic Markdown generator (五期 B 文件能力优化 · B14).

The model supplies either a raw markdown ``body`` (its strength — markdown is
*content/text*, fully A-铁律 compliant: not rendering code) or a structured
``sections`` payload (docx shape, accepted as a defensive fallback). This module
writes ``.md`` bytes using only the standard library. The model never writes
rendering code.

Unlike ``file_gen_common.clean_text`` (which collapses internal whitespace runs —
fine for table cells / headings but **destructive to markdown's significant
whitespace**: code fences, tables, nested lists, indentation), the free-form
body is cleaned *gently*: control chars dropped (keep ``\n``/``\t``), newlines
normalized, length-capped — spaces preserved.

Public API:
    normalize_md(raw) -> dict          # validate + clamp into a canonical shape
    build_md(data) -> bytes            # render canonical doc → .md bytes (UTF-8)
    safe_filename(title, ext="md")     # re-exported from file_gen_common
"""

from __future__ import annotations

from typing import Any

import file_gen_common as common

# --- Limits (clamp model output) ----------------------------------------------
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 200_000
MAX_SECTIONS = 100
MAX_ITEMS_PER_SECTION = 200
MAX_BLOCK_CHARS = 20_000
MAX_HEADING_CHARS = 200
MAX_BULLET_CHARS = 2000


def _clean_body(value: Any, limit: int) -> str:
    """Gently sanitize free-form text: drop control chars (keep ``\n``/``\t``),
    normalize newlines, cap length — **preserve significant whitespace** so
    markdown code blocks / tables / nested lists survive."""
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
    """Fallback path: docx-shape ``sections`` → [(heading, paragraphs, bullets)]."""
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


def normalize_md(raw: Any) -> dict[str, Any]:
    """Validate + clamp loose model output into a canonical markdown doc.

    Canonical shape::

        {"title": str, "body": str, "sections": [(heading, [para], [bullet]), ...]}

    ``body`` (raw markdown) takes precedence; ``sections`` is a structured
    fallback. Never raises — returns something renderable.
    """
    if not isinstance(raw, dict):
        raw = {}
    title = common.clean_text(raw.get("title") or raw.get("name"), MAX_TITLE_CHARS) or "未命名文档"
    body = _clean_body(
        raw.get("body") or raw.get("markdown") or raw.get("content") or raw.get("text"),
        MAX_BODY_CHARS,
    )
    sections = _sections_from(raw)
    return {"title": title, "body": body, "sections": sections}


def build_md(data: Any) -> bytes:
    """Render a (canonical or loose) doc into ``.md`` bytes (UTF-8)."""
    data = normalize_md(data)
    title, body, sections = data["title"], data["body"], data["sections"]
    out: list[str] = []
    if body:
        # Only inject an H1 title if the model didn't already lead with a heading.
        if not body.lstrip().startswith("#"):
            out.append(f"# {title}")
            out.append("")
        out.append(body)
    elif sections:
        out.append(f"# {title}")
        out.append("")
        for heading, paras, bullets in sections:
            if heading:
                out.append(f"## {heading}")
                out.append("")
            for p in paras:
                out.append(p)
                out.append("")
            for b in bullets:
                out.append(f"- {b}")
            if bullets:
                out.append("")
    else:
        out.append(f"# {title}")
    text = "\n".join(out).strip() + "\n"
    return text.encode("utf-8")


def safe_filename(title: str, ext: str = "md") -> str:
    return common.safe_filename(title, ext, fallback="笔记")


if __name__ == "__main__":  # pragma: no cover — local smoke test
    import sys

    sample = {
        "title": "周报 · 经营分析组",
        "body": (
            "# 周报 · 经营分析组\n\n"
            "## 本周进展\n\n"
            "- Q4 营收复盘**完成**,同比 +18.4%\n"
            "- 华南门店诊断报告初稿\n\n"
            "## 数据\n\n"
            "| 区域 | 营收(万) |\n|---|---|\n| 华东 | 1240 |\n| 华南 | 980 |\n\n"
            "```sql\nSELECT region, SUM(rev) FROM sales GROUP BY region;\n```\n"
        ),
    }
    data = build_md(sample)
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sample.md"
    with open(out, "wb") as f:
        f.write(data)
    print(f"OK: {len(data)} bytes → {out}")
    print(f"filename: {safe_filename(sample['title'])}")
    # structured-fallback smoke
    data2 = build_md({"title": "无 body 结构化", "sections": [
        {"heading": "目标", "paragraphs": ["把功能做扎实。"], "bullets": ["补 md/txt", "修预览"]},
    ]})
    assert b"## \xe7\x9b\xae\xe6\xa0\x87" in data2  # "## 目标"
    print(f"fallback OK: {len(data2)} bytes")
