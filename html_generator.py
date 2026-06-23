"""Deterministic HTML generator (五期 B+ 多类型扩展).

Produces a single self-contained ``.html`` file from one of two input shapes:

1. **Model-authored HTML** (``html`` field) — the model writes the page directly.
   It may be a **complete document** (``<!DOCTYPE html>…`` — used verbatim) or a
   **body fragment** (wrapped in a clean document shell with the product's
   typography). Per the **2026-06-23 PM decision「允许 js」**, generated HTML
   **may contain `<script>` / `<style>` / charts** (chart.js etc.) so that
   "可视化看板" requests get real interactive charts.
2. **Structured** — title + sections (heading / paragraphs / bullets), rendered
   by us into HTML-escaped markup. Used when no raw ``html`` is supplied.

🔒 **Security posture (decision 2026-06-23, blast radius bounded)**: we **no
longer sanitize** the model's HTML. This is a *deliberate* relaxation, contained
to **generated download files**: the artifact is served ``Content-Disposition:
attachment`` and opened by the user as a local ``file://`` document — an isolated
origin that **cannot reach ai.lxjchina cookies / storage / our app**. We do NOT
render model HTML anywhere in our own origin. Residual risk = prompt-injection
producing a malicious download (PM accepted). If model HTML is ever rendered
**in-app**, this must be revisited (sandboxed iframe + DOM sanitizer).

Public API:
    normalize_html(raw) -> dict         # validate + clamp into a canonical shape
    build_html(data) -> bytes           # render canonical data → .html bytes (UTF-8)
    safe_filename(title, ext="html")    # re-exported from file_gen_common
"""

from __future__ import annotations

import html as _html
from typing import Any

import file_gen_common as common

MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 400_000  # a chart dashboard with inline data can be large
MAX_SECTIONS = 80
MAX_PARAS_PER_SECTION = 80
MAX_BULLETS_PER_SECTION = 80
MAX_PARA_CHARS = 8000
MAX_BULLET_CHARS = 1000


def _is_full_document(html: str) -> bool:
    """True when the model already emitted a complete HTML document (use as-is)."""
    head = (html or "").lstrip()[:256].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def normalize_html(raw: Any) -> dict[str, Any]:
    """Validate + clamp loose model output into a canonical HTML payload.

    Canonical shape::

        {"title": str, "body_html": str | None, "full_doc": bool,
         "sections": [ {heading, paragraphs, bullets} ]}

    ``body_html`` (model-authored, **not** sanitized — see module docstring) wins
    when present; otherwise ``sections`` are rendered. Never raises.
    """
    if not isinstance(raw, dict):
        raw = {}
    title = common.clean_text(raw.get("title") or raw.get("name"), MAX_TITLE_CHARS) or "网页"

    body_raw = raw.get("html") or raw.get("body") or raw.get("content")
    body_html: str | None = None
    full_doc = False
    if isinstance(body_raw, str) and body_raw.strip():
        # length-cap only; do NOT clean_text (would mangle JS/CSS/whitespace).
        body_html = body_raw[:MAX_BODY_CHARS]
        full_doc = _is_full_document(body_html)

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
        sections = [{"heading": title, "paragraphs": [], "bullets": []}]

    return {"title": title, "body_html": body_html, "full_doc": full_doc, "sections": sections}


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
  main {{ max-width: 960px; margin: 0 auto; }}
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
  img, canvas, svg {{ max-width: 100%; height: auto; }}
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
    """Render a (canonical or loose) payload into a standalone ``.html`` (UTF-8).

    - model wrote a **full document** → used verbatim (interactive charts intact);
    - model wrote a **body fragment** → wrapped in the doc shell (script/style kept);
    - **structured sections** only → rendered HTML-escaped inside the shell.
    """
    data = normalize_html(data)
    if data["body_html"] is not None and data["full_doc"]:
        # model authored a complete page (e.g. chart.js dashboard) — use as-is.
        return data["body_html"].encode("utf-8")
    title_esc = _html.escape(data["title"])
    body = data["body_html"] if data["body_html"] is not None else _render_sections(data["sections"])
    doc = _DOC_TEMPLATE.format(title=title_esc, accent=common.accent_hex(), body=body)
    return doc.encode("utf-8")


def safe_filename(title: str, ext: str = "html") -> str:
    return common.safe_filename(title, ext, fallback="page")


if __name__ == "__main__":  # pragma: no cover — local smoke test
    import sys

    # 1) full document with chart.js → preserved verbatim (interactive charts work)
    full = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>成绩看板</title><script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script>"
        "</head><body><canvas id=\"c\"></canvas>"
        "<script>new Chart(document.getElementById('c'),{type:'bar',data:{labels:['语文','数学'],datasets:[{data:[88,95]}]}});</script>"
        "</body></html>"
    )
    out_full = build_html({"title": "成绩看板", "html": full}).decode("utf-8")
    assert "<script" in out_full and "chart.js" in out_full and "new Chart" in out_full, "full-doc script not preserved!"
    assert out_full.count("<!DOCTYPE") == 1, "full doc should not be double-wrapped"

    # 2) body fragment with a script → wrapped in shell, script kept
    frag = build_html({"title": "图表", "html": "<canvas id='x'></canvas><script>console.log('ok')</script>"}).decode("utf-8")
    assert "<script>console.log('ok')</script>" in frag and frag.count("<!DOCTYPE") == 1, "fragment script not kept / not wrapped"

    # 3) structured sections → escaped rendering in shell
    structured = build_html({"title": "说明", "sections": [{"heading": "概述", "bullets": ["要点A", "要点B"]}]}).decode("utf-8")
    assert "<h2>概述</h2>" in structured and "要点A" in structured

    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sample.html"
    with open(out, "wb") as f:
        f.write(out_full.encode("utf-8"))
    print(f"OK: full-doc {len(out_full)}B (script preserved) + fragment + structured 全部通过 → {out}")
    print(f"filename: {safe_filename('成绩看板')}")
