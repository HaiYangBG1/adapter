"""Deterministic CSV generator (五期 B+ 多类型扩展).

The model supplies a *structured table* (headers + rows); this module renders it
into ``.csv`` bytes using only the standard library. The model never writes
rendering code (A 铁律).

Design goals mirror ``pptx_generator``: deterministic, dependency-light,
defensive (tolerate loose / oversized model output by normalizing before
rendering), and generic / open-source safe.

UTF-8 **with BOM**: Excel on Windows/macOS otherwise mis-decodes CJK columns as
mojibake when double-clicking a .csv. The BOM makes Excel pick UTF-8.

Public API:
    normalize_table(raw) -> dict      # validate + clamp into a canonical shape
    build_csv(table) -> bytes         # render canonical table → .csv bytes (UTF-8 BOM)
    safe_filename(title, ext="csv")   # re-exported from file_gen_common
"""

from __future__ import annotations

import csv
import io
from typing import Any

import file_gen_common as common

# --- Limits (clamp model output so a runaway table can't blow up render) ------
MAX_COLS = 100
MAX_ROWS = 5000
MAX_CELL_CHARS = 2000
MAX_TITLE_CHARS = 120


def normalize_table(raw: Any) -> dict[str, Any]:
    """Validate + clamp loose model output into a canonical single-table shape.

    Canonical shape::

        {"title": str, "headers": [str, ...], "rows": [[cell, ...], ...]}

    Never raises on bad input — returns a renderable (possibly empty) table.
    Rows are padded / truncated to the header width so the CSV stays rectangular.
    """
    if not isinstance(raw, dict):
        raw = {}

    title = common.clean_text(raw.get("title") or raw.get("name"), MAX_TITLE_CHARS) or "数据表"

    raw_headers = raw.get("headers") or raw.get("columns") or []
    if not isinstance(raw_headers, list):
        raw_headers = []
    headers = [common.clean_text(h, 120) for h in raw_headers][:MAX_COLS]

    raw_rows = raw.get("rows") or raw.get("data") or []
    if not isinstance(raw_rows, list):
        raw_rows = []

    # If headers were omitted but rows are dicts, derive headers from keys (order
    # of first row), then map each row dict → ordered cell list.
    if not headers and raw_rows and isinstance(raw_rows[0], dict):
        seen: list[str] = []
        for r in raw_rows[:MAX_ROWS]:
            if isinstance(r, dict):
                for k in r.keys():
                    kk = common.clean_text(k, 120)
                    if kk and kk not in seen:
                        seen.append(kk)
                        if len(seen) >= MAX_COLS:
                            break
        headers = seen

    width = len(headers)
    rows: list[list[Any]] = []
    for r in raw_rows:
        if isinstance(r, dict):
            cells = [common.as_cell(r.get(h, ""), MAX_CELL_CHARS) for h in headers]
        elif isinstance(r, list):
            cells = [common.as_cell(c, MAX_CELL_CHARS) for c in r[:MAX_COLS]] if width == 0 else \
                    [common.as_cell(c, MAX_CELL_CHARS) for c in r[:width]]
        else:
            # bare scalar row → single cell
            cells = [common.as_cell(r, MAX_CELL_CHARS)]
        # pad/truncate to header width (when headers known) to stay rectangular
        if width:
            if len(cells) < width:
                cells = cells + [""] * (width - len(cells))
            else:
                cells = cells[:width]
        rows.append(cells)
        if len(rows) >= MAX_ROWS:
            break

    # Guarantee something renderable: at least a header or one row.
    if not headers and not rows:
        headers = ["列1"]
        rows = [[""]]

    return {"title": title, "headers": headers, "rows": rows}


def build_csv(table: Any) -> bytes:
    """Render a (canonical or loose) table into ``.csv`` bytes (UTF-8 with BOM).

    Always normalizes first, so callers may pass raw model output directly.
    """
    table = normalize_table(table)
    sio = io.StringIO(newline="")
    writer = csv.writer(sio, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    if table["headers"]:
        writer.writerow(table["headers"])
    for row in table["rows"]:
        writer.writerow(row)
    # UTF-8 BOM so Excel decodes CJK correctly on double-click.
    return b"\xef\xbb\xbf" + sio.getvalue().encode("utf-8")


def safe_filename(title: str, ext: str = "csv") -> str:
    return common.safe_filename(title, ext, fallback="data")


if __name__ == "__main__":  # pragma: no cover — local smoke test
    import sys

    sample = {
        "title": "Q4 区域销售",
        "headers": ["区域", "销售额(万)", "门店数", "环比"],
        "rows": [
            ["华东", 1240.5, 47, "+12%"],
            ["华南", "980", "33", "+8%"],
            {"区域": "华北", "销售额(万)": 610, "门店数": 21, "环比": "-3%"},
        ],
    }
    data = build_csv(sample)
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sample.csv"
    with open(out, "wb") as f:
        f.write(data)
    print(f"OK: {len(data)} bytes → {out}")
    print(f"filename: {safe_filename(sample['title'])}")
