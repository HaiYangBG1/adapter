# adapter Capability Matrix

This document describes the public, deployment-neutral capabilities of `adapter`.

## Summary

| Area | Support | Notes |
|---|---|---|
| OpenAI-compatible proxy | Yes | Supports common chat, responses, models, and health paths |
| File adaptation | Yes | Converts file parts into text/image parts before forwarding |
| Web augmentation | Yes | Optional `/web/v1` path for URL fetch and search |
| SSE streaming | Yes | Streams upstream chunks and optional progress chunks |
| SSRF protection | Yes | Blocks internal/private targets and validates redirects |
| Upstream auth | Yes | Optional bearer token through environment variable |

## File Inputs

| File type | Status | Extracted content | Visual handling | Limitations |
|---|---|---|---|---|
| Images | Supported | Filename metadata | Forwarded as data URL | GIF frame-by-frame understanding depends on upstream model |
| PDF | Supported | Text via `pypdf` | Rendered pages via PyMuPDF | Scanned PDFs rely on visual model ability unless OCR is added |
| DOCX | Supported | Paragraphs and tables | Embedded images where available | Legacy `.doc` is not fully supported |
| PPTX | Supported | Slide text | Embedded images where available | Animations and master layouts are not fully reconstructed |
| XLSX | Supported | Sheet names, dimensions, preview rows, formulas, cached values, coordinates, merged ranges, table ranges | Optional LibreOffice visual render | Formulas are read, not recalculated |
| CSV/TSV | Supported | Delimiter-detected table preview | None | Large files are previewed and truncated |
| Text/code/JSON/XML/HTML | Supported | Decoded text | None | No schema validation |
| `.doc/.ppt/.xls` | Limited | Not guaranteed | Not guaranteed | Convert to modern Office formats first |

## Web Inputs

| Feature | Status | Description |
|---|---|---|
| Explicit URL fetch | Supported | Extracts `http/https` URLs from user text |
| Search | Supported | `bing_html`, `duckduckgo`, `tavily`, and `bing` providers |
| Curated news URLs | Optional | Configure with `ADAPTER_WEB_AI_NEWS_SOURCE_URLS` |
| Source context | Supported | Injects title, URL, content, and retrieval time |
| Citation instruction | Supported | Asks model to list source URLs |
| Progress messages | Optional | Controlled by `ADAPTER_WEB_PROGRESS_MODE` |
| Cache | Supported | In-memory per-process cache |

## Web Control Parameters

| Parameter | Values | Description |
|---|---|---|
| `web_mode` | `off`, `auto`, `on` | Main web switch |
| `web_search` | boolean | Compatibility switch |
| `enable_web_search` | boolean | Compatibility switch |
| `web_options.max_urls` | integer | Max explicit URLs to fetch |
| `web_options.max_results` | integer | Max search results to collect |
| `web_options.fetch_search_results` | integer | Max search result pages to fetch |

## Default Limits

| Limit | Default | Environment variable |
|---|---:|---|
| File size | `25MB` | `ADAPTER_MAX_FILE_BYTES` |
| Extracted text per attachment | `16000` chars | `ADAPTER_MAX_TEXT_CHARS` |
| Rendered pages | `6` | `ADAPTER_MAX_RENDER_PAGES` |
| Table preview rows | `40` | `ADAPTER_MAX_TABLE_ROWS` |
| Table preview columns | `24` | `ADAPTER_MAX_TABLE_COLS` |
| Workbook sheets | `8` | `ADAPTER_MAX_SHEETS` |
| Office embedded images | `4` | `ADAPTER_MAX_OFFICE_IMAGES` |
| XLSX formula cells | `120` | `ADAPTER_MAX_XLSX_FORMULA_CELLS` |
| XLSX formula scan | `1000 x 80` | `ADAPTER_MAX_XLSX_FORMULA_SCAN_ROWS`, `ADAPTER_MAX_XLSX_FORMULA_SCAN_COLS` |
| Web explicit URLs | `3` | `ADAPTER_WEB_MAX_URLS` |
| Web search results | `5` | `ADAPTER_WEB_SEARCH_RESULTS` |
| Search pages fetched | `3` | `ADAPTER_WEB_FETCH_SEARCH_RESULTS` |
| Web page bytes | `2MB` | `ADAPTER_WEB_MAX_PAGE_BYTES` |
| Web page chars | `30000` | `ADAPTER_WEB_MAX_PAGE_CHARS` |
| Web context chars | `100000` | `ADAPTER_WEB_MAX_CONTEXT_CHARS` |
| Web timeout | `10s` | `ADAPTER_WEB_TIMEOUT` |
| Web cache TTL | `600s` | `ADAPTER_WEB_CACHE_TTL` |

## Security Boundaries

| Boundary | Behavior |
|---|---|
| Localhost | Blocked |
| Private networks | Blocked |
| Link-local addresses | Blocked |
| Metadata endpoints | Blocked |
| Redirects | Revalidated on every hop |
| External pages | Injected as untrusted context |
| Secrets | Must be provided through environment variables, not code |

## Recommended Smoke Tests

| Test | Expected result |
|---|---|
| `GET /health` | JSON health response |
| Plain chat through `/v1/chat/completions` | Upstream response is proxied |
| PDF upload | Extracted text and/or rendered pages are sent upstream |
| XLSX upload | Sheet structure and formula metadata are visible to upstream |
| Explicit URL through `/web/v1/chat/completions` | Answer uses fetched source |
| Private URL attempt | Fetch is rejected |
| Streaming request | Response remains chunked |
