# adapter

OpenAI-compatible adapter for private or self-hosted model servers.

`adapter` sits between clients and an upstream OpenAI-compatible model endpoint. It normalizes file inputs, optionally enriches requests with public web context, preserves streaming responses, and forwards the final request to the upstream model.

The project is intentionally deployment-neutral. It does not include company-specific service names, private server addresses, internal model aliases, or secrets.

## Features

| Capability | Description |
|---|---|
| OpenAI-compatible proxy | Exposes `/v1/chat/completions`, `/v1/responses`, `/models`, and health endpoints |
| File input adapter | Converts `type=file` content parts into model-readable `text` and `image_url` parts |
| Office and PDF handling | Extracts text and can render selected pages/slides/sheets visually when LibreOffice/PyMuPDF are available |
| Spreadsheet understanding | Preserves sheet names, preview rows, cell coordinates, formulas, cached values, merged ranges, and table ranges |
| Web augmentation | `/web/v1` can fetch URLs, search the web, inject source context, and ask the model to cite source URLs |
| Streaming | Preserves SSE streaming and can emit short progress messages before model generation |
| Safety | Blocks localhost, private networks, link-local, metadata endpoints, and other internal targets during web fetches |
| Debuggability | Keeps adapter behavior explicit so deployments can compare adapter-routed requests with direct upstream calls |

## Architecture

```text
Client / SDK / Tool
        |
        v
adapter (/v1 or /web/v1)
        |
        v
OpenAI-compatible upstream model server
```

`/v1` defaults to `web_mode=off`.

`/web/v1` defaults to `web_mode=auto`.

## API Shape

The adapter accepts OpenAI-style requests. For file-capable clients, `content` may include `type=file` parts:

```json
{
  "model": "private-model",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Summarize this document."},
        {
          "type": "file",
          "file": {
            "filename": "report.pdf",
            "file_data": "data:application/pdf;base64,..."
          }
        }
      ]
    }
  ]
}
```

For web-enabled calls:

```json
{
  "model": "private-model",
  "messages": [{"role": "user", "content": "Read https://example.com and summarize it."}],
  "web_mode": "auto",
  "web_options": {
    "max_urls": 3,
    "max_results": 5,
    "fetch_search_results": 3
  }
}
```

## Supported Inputs

| Type | Support | Handling |
|---|---|---|
| Images `png/jpg/jpeg/webp/gif` | Supported | Converts to `image_url` data URLs |
| PDF | Supported | Extracts text with `pypdf`; renders selected pages with PyMuPDF |
| DOCX | Supported | Extracts paragraphs and tables |
| PPTX | Supported | Extracts slide text and embedded images |
| XLSX | Supported | Extracts sheet structure, preview rows, formulas, cached values, coordinates, merged ranges, and table ranges |
| CSV/TSV | Supported | Detects delimiter and produces table previews |
| Text/Markdown/JSON/XML/HTML/code | Supported | Decodes as text |
| Legacy Office `.doc/.ppt/.xls` | Limited | Convert to modern Office formats first |

## Web Capability

| Feature | Description |
|---|---|
| URL fetch | Fetches explicit `http/https` URLs from the latest user request |
| Search | Supports `bing_html`, `duckduckgo`, `tavily`, and `bing` providers |
| Source injection | Adds untrusted web context as a system message with title, URL, content, and retrieval time |
| Citations | Instructs the model to list source URLs when answering from web context |
| Progress | Optional visible progress in SSE streams |
| History cleanup | Removes previous adapter progress lines from assistant history before forwarding |
| SSRF protection | Validates target URLs and redirects before every fetch |

## Environment Variables

| Variable | Default | Description |
|---|---:|---|
| `ADAPTER_HOST` | `0.0.0.0` | Listen host |
| `ADAPTER_PORT` | `8000` | Listen port |
| `ADAPTER_UPSTREAM_BASE_URL` | `http://127.0.0.1:8001/v1` | Upstream OpenAI-compatible base URL |
| `ADAPTER_UPSTREAM_API_KEY` | empty | Optional bearer token used when calling upstream |
| `ADAPTER_UPSTREAM_AUTH_HEADER` | `Authorization` | Header used for upstream auth |
| `ADAPTER_MAX_FILE_BYTES` | `25MB` | Max accepted file size |
| `ADAPTER_MAX_TEXT_CHARS` | `16000` | Max extracted text chars per attachment |
| `ADAPTER_MAX_RENDER_PAGES` | `6` | Max PDF/Office pages rendered as images |
| `ADAPTER_ENABLE_OFFICE_RENDER` | `1` | Enable LibreOffice rendering |
| `ADAPTER_LIBREOFFICE_BIN` | auto | Optional path to `soffice` |
| `ADAPTER_WEB_ENABLED` | `1` | Enable web capability |
| `ADAPTER_WEB_SEARCH_PROVIDER` | `bing_html` | Search provider |
| `ADAPTER_WEB_PROGRESS_MODE` | `metadata` | `metadata` or `content` |
| `ADAPTER_WEB_FORCE_IPV4` | `1` | Prefer IPv4 for outbound fetches |
| `ADAPTER_WEB_AI_NEWS_SOURCE_URLS` | empty | Optional comma-separated curated news URLs |
| `TAVILY_API_KEY` | empty | Tavily API key when using `tavily` provider |
| `BING_SEARCH_API_KEY` | empty | Bing Search API key when using `bing` provider |

## Local Run

```bash
python3 -m pip install -r requirements.txt
ADAPTER_UPSTREAM_BASE_URL="http://127.0.0.1:8001/v1" \
ADAPTER_PORT=8000 \
python3 adapter.py
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Docker

The Docker image includes Python 3.12, LibreOffice, Chinese fonts, and Python parsing dependencies.

```bash
docker build -t adapter:local .
docker run --rm -p 8000:8000 \
  -e ADAPTER_UPSTREAM_BASE_URL="http://host.docker.internal:8001/v1" \
  adapter:local
```

## Validation

Minimum check:

```bash
python3 -m py_compile adapter.py
```

Container check:

```bash
docker build -t adapter:check .
docker run --rm adapter:check python -m py_compile /app/adapter.py
```

Suggested behavior checks:

| Check | Expected |
|---|---|
| `GET /health` | Adapter process is alive |
| Text request through `/v1/chat/completions` | Upstream model answers normally |
| PDF/CSV/XLSX request | File content is converted into model-readable context |
| URL request through `/web/v1/chat/completions` | Answer includes relevant source URL |
| SSE request | Multiple chunks are streamed |
| Private URL fetch attempt | Request is blocked |

## Security Notes

- Do not commit API keys, provider tokens, model-server tokens, `.env` files, or private endpoint URLs.
- Web content is treated as untrusted context and must not override higher-priority instructions.
- SSRF protection is enabled by default, but deployments should still run the adapter with least network privilege.
- If you expose this service publicly, put authentication, rate limits, and request-size limits in front of it.

## Repository Files

| File | Purpose |
|---|---|
| `adapter.py` | Main adapter service |
| `Dockerfile` | Production container image |
| `requirements.txt` | Python dependencies |
| `CAPABILITIES.md` | Detailed capability matrix |
| `AGENTS.md` | Instructions for coding agents |
| `scripts/build_remote_image.sh` | Generic remote-registry build helper |
