"""Deterministic HTML generator (五期 B+ 多类型扩展).

Two input shapes, both producing a single self-contained ``.html`` file:

1. **Raw body** — the model writes the page body HTML directly (``html`` field).
   Per the 06-14 decision, model-authored HTML is an accepted pattern (it is
   *data*, never server-executed code — A 铁律 holds: we only write bytes, the
   user's own browser renders the downloaded file). We still **sanitize**
   conservatively (defense-in-depth) before wrapping it in a clean document
   shell with the product's typography + accent.
2. **Structured** — title + sections (heading / paragraphs / bullets), rendered
   by us into safe markup. Used when no raw ``html`` is supplied.

Sanitization is regex-based best-effort (not a full HTML parser): it strips
``<script>`` / external-resource / framing tags, inline ``on*`` event handlers,
and ``javascript:`` / ``vbscript:`` URLs. Adequate for an MVP whose output is a
*downloaded* file from a trusted model source; a full DOM sanitizer is a future
hardening if HTML is ever rendered inline in-app.

Public API:
    normalize_html(raw) -> dict         # validate + clamp into a canonical shape
    build_html(data) -> bytes           # render canonical data → .html bytes (UTF-8)
    safe_filename(title, ext="html")    # re-exported from file_gen_common
"""

from __future__ import annotations

import html as _html
import re
from typing import Any

import file_gen_common as common

MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 200_000
MAX_SECTIONS = 80
MAX_PARAS_PER_SECTION = 80
MAX_BULLETS_PER_SECTION = 80
MAX_PARA_CHARS = 8000
MAX_BULLET_CHARS = 1000

# Tags removed wholesale (content + tag): active code, external loaders, framing,
# and author <style> (the doc shell supplies styling; <style> can carry url()/
# expression() vectors — the tool prompt already tells the model not to emit it).
_DANGEROUS_BLOCK_TAGS = ("script", "iframe", "object", "embed", "noscript", "template", "style")
# Void / standalone tags removed (no closing tag): external links, refresh metas, applets.
_DANGEROUS_VOID_TAGS = ("link", "base")
# URL-bearing attributes that can carry javascript:/vbscript: payloads.
_URL_ATTRS = "href|src|formaction|action|xlink:href"


def _sanitize_fragment(fragment: str) -> str:
    """Best-effort strip of active/loader vectors from a model-authored HTML body.

    Regex-based (not a full DOM parser) — adequate for an MVP whose output is a
    *downloaded* file from a trusted model source. Covers the common vectors:
    <script>/<style>/framing/loader tags, meta-refresh, inline ``on*`` handlers,
    and ``javascript:``/``vbscript:`` URLs (both quoted and unquoted).
    """
    s = fragment or ""
    # 1) block tags with their content (handles unclosed by also nuking a dangling open tag)
    for tag in _DANGEROUS_BLOCK_TAGS:
        s = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}\s*>", "", s, flags=re.IGNORECASE | re.DOTALL)
        s = re.sub(rf"<{tag}\b[^>]*>", "", s, flags=re.IGNORECASE)
        s = re.sub(rf"</{tag}\s*>", "", s, flags=re.IGNORECASE)
    # 2) standalone loader tags
    for tag in _DANGEROUS_VOID_TAGS:
        s = re.sub(rf"<{tag}\b[^>]*/?>", "", s, flags=re.IGNORECASE)
    # 3) meta refresh (redirect vector)
    s = re.sub(r"<meta\b[^>]*http-equiv\s*=\s*['\"]?refresh[^>]*>", "", s, flags=re.IGNORECASE)
    # 4) inline event handlers: on...="..." / '...' / unquoted
    s = re.sub(r"\son[a-zA-Z]+\s*=\s*\"[^\"]*\"", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\son[a-zA-Z]+\s*=\s*'[^']*'", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\son[a-zA-Z]+\s*=\s*[^\s>]+", "", s, flags=re.IGNORECASE)
    # 5) javascript:/vbscript: URLs in url-bearing attrs — quoted then unquoted.
    s = re.sub(rf"({_URL_ATTRS})\s*=\s*([\"'])\s*(?:javascript|vbscript)\s*:[^\"']*\2",
               r'\1=\2#\2', s, flags=re.IGNORECASE)
    s = re.sub(rf"({_URL_ATTRS})\s*=\s*(?:javascript|vbscript)\s*:[^\s>\"']*",
               r'\1=#', s, flags=re.IGNORECASE)
    return s


def normalize_html(raw: Any) -> dict[str, Any]:
    """Validate + clamp loose model output into a canonical HTML payload.

    Canonical shape::

        {"title": str, "body_html": str | None, "sections": [ {heading, paragraphs, bullets} ]}

    ``body_html`` (sanitized) wins when present; otherwise ``sections`` are
    rendered. Never raises.
    """
    if not isinstance(raw, dict):
        raw = {}
    title = common.clean_text(raw.get("title") or raw.get("name"), MAX_TITLE_CHARS) or "网页"

    body_raw = raw.get("html") or raw.get("body") or raw.get("content")
    body_html: str | None = None
    if isinstance(body_raw, str) and body_raw.strip():
        body_html = _sanitize_fragment(body_raw[:MAX_BODY_CHARS])

    sections: list[dict[str, Any]] = []
    raw_sections = raw.get("sections")
    if isinstance(raw_sections, list):
        for s in raw_sections:
            if not isinstance(s, dict):
                continue
            heading = common.clean_text(s.get("heading") or s.get("title"), MAX_TITLE_CHARS)
            paragraphs = _norm_str_list(s.get("paragraphs") or s.get("text"), MAX_PARA_CHARS, MAX_PARAS_PER_SECTION)
            bullets = _norm_str_list(s.get("bullets") or s.get("points"), MAX_BULLET_CHARS, MAX_BULLETS_PER_SECTION)
            if heading or paragraphs or bullets:
                sections.append({"heading": heading, "paragraphs": paragraphs, "bullets": bullets})
            if len(sections) >= MAX_SECTIONS:
                break

    if body_html is None and not sections:
        # minimal renderable page
        sections = [{"heading": title, "paragraphs": [], "bullets": []}]

    return {"title": title, "body_html": body_html, "sections": sections}


def _norm_str_list(raw: Any, limit_chars: int, max_items: int) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        t = common.clean_text(item, limit_chars)
        if t:
            out.append(t)
        if len(out) >= max_items:
            break
    return out


def _render_sections(sections: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for sec in sections:
        if sec["heading"]:
            parts.append(f"<h2>{_html.escape(sec['heading'])}</h2>")
        for p in sec["paragraphs"]:
            parts.append(f"<p>{_html.escape(p)}</p>")
        if sec["bullets"]:
            lis = "".join(f"<li>{_html.escape(b)}</li>" for b in sec["bullets"])
            parts.append(f"<ul>{lis}</ul>")
    return "\n".join(parts)


_DOC_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --accent: #{accent}; --ink: #1a1a1a; --body: #3c3c3c; --muted: #8a8a8a; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 40px 20px; color: var(--body);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
      "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    line-height: 1.7; background: #fff; }}
  main {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ color: var(--ink); font-size: 28px; margin: 0 0 8px;
    border-bottom: 3px solid var(--accent); padding-bottom: 12px; }}
  h2 {{ color: var(--accent); font-size: 20px; margin: 32px 0 12px; }}
  p {{ margin: 0 0 14px; }}
  ul {{ margin: 0 0 14px; padding-left: 22px; }}
  li {{ margin: 0 0 6px; }}
  a {{ color: var(--accent); }}
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 14px; }}
  th, td {{ border: 1px solid #e5e5e5; padding: 8px 10px; text-align: left; }}
  th {{ background: var(--accent); color: #fff; }}
  img {{ max-width: 100%; height: auto; }}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
{body}
</main>
</body>
</html>
"""


def build_html(data: Any) -> bytes:
    """Render a (canonical or loose) payload into a standalone ``.html`` (UTF-8)."""
    data = normalize_html(data)
    title_esc = _html.escape(data["title"])
    body = data["body_html"] if data["body_html"] is not None else _render_sections(data["sections"])
    doc = _DOC_TEMPLATE.format(title=title_esc, accent=common.accent_hex(), body=body)
    return doc.encode("utf-8")


def safe_filename(title: str, ext: str = "html") -> str:
    return common.safe_filename(title, ext, fallback="page")


if __name__ == "__main__":  # pragma: no cover — local smoke test
    import sys

    # Sanitizer unit assertions — test the FRAGMENT cleaner directly (the full doc
    # always contains the shell's own legitimate <style>, so assert on the fragment).
    malicious = (
        "<h2>ok</h2>"
        "<script>alert('xss')</script>"  # block tag + content
        "<style>body{background:url(javascript:alert(2))}</style>"  # author style block
        "<a href=\"javascript:alert(1)\">q</a>"  # quoted js:
        "<a href=javascript:alert(3)>u</a>"  # UNQUOTED js: (P1-2)
        "<button formaction=javascript:alert(4)>b</button>"  # formaction
        "<img src=x onerror=alert(5)>"  # inline handler
    )
    cleaned = _sanitize_fragment(malicious).lower()
    assert "<script" not in cleaned, "script not stripped!"
    assert "<style" not in cleaned, "author style not stripped!"
    assert "javascript:" not in cleaned, "js url not neutralized!"
    assert "onerror" not in cleaned, "inline handler not stripped!"
    assert "<h2>ok</h2>" in cleaned, "benign content wrongly removed!"

    sample = {
        "title": "产品发布说明",
        "html": (
            "<h2>核心特性</h2><p>本次发布带来三项能力。</p>"
            "<ul><li>多类型文件生成</li><li>自动识别意图</li></ul>"
        ),
    }
    data = build_html(sample)
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sample.html"
    with open(out, "wb") as f:
        f.write(data)
    print(f"OK: {len(data)} bytes, sanitizer assertions passed → {out}")
    print(f"filename: {safe_filename(sample['title'])}")
