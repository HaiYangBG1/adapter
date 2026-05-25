# adapter Deployment Guide

This guide covers deploying `adapter` with the agentic web mode
(`/v1/agent/chat/completions`) enabled. For the passive `/v1` and `/web/v1`
paths, only the *Core* section applies.

## Architecture

```
client ──► adapter ──► upstream OpenAI-compatible model server
              │
              ├─ web_search ──► SearXNG (self-hosted) or Baidu
              ├─ web_fetch  ──► direct HTTP(S) fetch (+ SSRF guard)
              └─ web_view   ──► headless Chromium screenshot
```

The agent loop runs entirely inside `adapter`: it calls the upstream model,
dispatches tools, and loops until the model produces a final answer.

## Endpoints

| Path | Behaviour |
|---|---|
| `/v1/*` | Transparent OpenAI-compatible proxy + document adaptation |
| `/web/v1/*` | Same, plus passive web-context injection |
| `/v1/agent/chat/completions` | Agentic tool-calling loop (search / fetch / screenshot) |
| `GET /health` | Status + capability report |

## Environment Variables

### Core (required)

| Variable | Default | Notes |
|---|---|---|
| `ADAPTER_UPSTREAM_BASE_URL` | `http://127.0.0.1:8001/v1` | Upstream model server base URL |
| `ADAPTER_UPSTREAM_API_KEY` | _(empty)_ | Sent as `Bearer <key>` to upstream |
| `ADAPTER_HOST` / `ADAPTER_PORT` | `0.0.0.0` / `8000` | Listen address |

### Agentic web — loop

| Variable | Default | Notes |
|---|---|---|
| `ADAPTER_AGENT_MODEL` | _(empty)_ | Model name sent upstream; if empty, taken from request payload |
| `ADAPTER_AGENT_MAX_ITERATIONS` | `5` | Hard cap on agent turns |
| `ADAPTER_AGENT_MAX_SEARCHES` | `8` | Per-session `web_search` budget |
| `ADAPTER_AGENT_MAX_FETCHES` | `8` | Per-session `web_fetch` + `web_view` budget |
| `ADAPTER_AGENT_MAX_CONCURRENT` | `20` | Simultaneous in-flight agent requests; excess gets HTTP 429 |
| `ADAPTER_AGENT_TIMEOUT` | `120` | Per upstream call timeout (seconds) |
| `ADAPTER_AGENT_MAX_TOOL_RESULT_CHARS` | `8000` | Truncation cap per tool result |
| `ADAPTER_AGENT_PARALLEL_WORKERS` | `4` | Thread pool size for parallel tool dispatch |
| `ADAPTER_AGENT_DEFAULT_MAX_TOKENS` | `2000` | Injected when the client omits `max_tokens` |

### Agentic web — search

| Variable | Default | Notes |
|---|---|---|
| `ADAPTER_WEB_SEARCH_PROVIDER` | `bing_html` | `searxng` \| `baidu` \| `bing_html` \| `duckduckgo` \| `tavily` \| `bing` |
| `ADAPTER_WEB_SEARCH_FALLBACK` | `baidu` | Provider used when the primary fails; empty to disable |
| `ADAPTER_SEARXNG_URL` | _(empty)_ | Required when provider is `searxng`, e.g. `http://searxng:8080` |
| `ADAPTER_WEB_SEARCH_RESULTS` | `5` | Default result count |

`searxng` and `baidu` need **no API key**. `tavily` / `bing` need a key
(`TAVILY_API_KEY` / `BING_SEARCH_API_KEY`).

### Agentic web — vision (web_view)

| Variable | Default | Notes |
|---|---|---|
| `ADAPTER_AGENT_WEB_VIEW_ENABLED` | `1` | Set `0` to disable screenshots entirely |
| `ADAPTER_AGENT_WEB_VIEW_MAX_CONCURRENT` | `3` | Simultaneous Chromium instances — see memory note below |
| `ADAPTER_AGENT_WEB_VIEW_VIEWPORT` | `1280x1600` | Browser viewport |
| `ADAPTER_AGENT_WEB_VIEW_TIMEOUT_MS` | `20000` | Page-load timeout |
| `ADAPTER_AGENT_WEB_VIEW_IMAGE_MAX_WIDTH` | `1280` | Screenshot is downscaled to this width |
| `ADAPTER_AGENT_WEB_VIEW_JPEG_QUALITY` | `75` | Screenshot JPEG quality |
| `ADAPTER_AGENT_FETCH_FALLBACK_MIN_CHARS` | `200` | `web_fetch` falls back to `web_view` when extracted text is shorter than this |

**Memory note:** each concurrent Chromium instance uses roughly 300 MB RSS.
`MAX_CONCURRENT × 300 MB` is the screenshot memory ceiling — size it to the
host (e.g. 3 on a 2 GB box, 10+ on an 8 GB box).

## SearXNG Setup (recommended search provider)

SearXNG is a free, self-hosted metasearch engine — no API key, queries stay
inside your infrastructure. JSON output must be enabled.

`searxng/settings.yml`:

```yaml
use_default_settings: true
server:
  secret_key: "CHANGE-ME"
  limiter: false
search:
  formats:
    - html
    - json
  languages:
    - zh-CN
```

`docker-compose.yml` snippet:

```yaml
services:
  searxng:
    image: searxng/searxng
    volumes:
      - ./searxng:/etc/searxng:rw
    restart: unless-stopped

  adapter:
    build: .
    environment:
      ADAPTER_UPSTREAM_BASE_URL: "http://your-model-server/v1"
      ADAPTER_UPSTREAM_API_KEY: "your-key"
      ADAPTER_AGENT_MODEL: "your-model-name"
      ADAPTER_WEB_SEARCH_PROVIDER: "searxng"
      ADAPTER_SEARXNG_URL: "http://searxng:8080"
      ADAPTER_WEB_SEARCH_FALLBACK: "baidu"
    ports:
      - "8000:8000"
    depends_on:
      - searxng
    restart: unless-stopped
```

If you do not want to run SearXNG, set `ADAPTER_WEB_SEARCH_PROVIDER=baidu` —
it needs no extra service.

## Build Notes

- The Dockerfile installs Chromium via `playwright install --with-deps
  chromium`. This adds ~300 MB to the image. To skip it, remove that line
  and run with `ADAPTER_AGENT_WEB_VIEW_ENABLED=0`.
- Both `adapter.py` and `agentic_web.py` must be present in the image.

## Pre-Deploy Checklist

- [ ] `ADAPTER_UPSTREAM_BASE_URL` + `ADAPTER_UPSTREAM_API_KEY` point at the model server
- [ ] Upstream vLLM started with `--enable-auto-tool-choice --tool-call-parser <parser>`
- [ ] `ADAPTER_AGENT_MODEL` set (or clients always pass `model`)
- [ ] Search provider chosen; if `searxng`, the container is up with JSON enabled
- [ ] `GET /health` reports `agentic_web: true` and the expected provider
- [ ] `web_view` smoke test passes (or it is disabled)
- [ ] `ADAPTER_AGENT_MAX_CONCURRENT` tuned to upstream capacity
- [ ] `ADAPTER_AGENT_WEB_VIEW_MAX_CONCURRENT` tuned to host memory
- [ ] adapter is **not** exposed to untrusted networks — `/v1/agent` has no auth of its own

## Observability

Every agent request emits one structured log line prefixed `AGENT_METRICS`,
containing iteration count, tool usage, citation health, truncation, and
latency. It is JSON after the prefix — pipe it to your log stack and parse
with `jq` / Loki / equivalent.

## Known Limitations

- `baidu` provider returns Baidu redirect URLs (`baidu.com/link?url=...`);
  they resolve correctly when fetched but look opaque in citations.
- `/v1/agent` performs no authentication of its own — deploy it behind a
  trusted gateway or network boundary.
- Real-streaming mode (v0.2.13+) cannot rewrite content once it leaves the
  wire, so the `<tool_call>` leak strip and "dig deeper" pushback paths
  are bypassed in the streaming endpoint. The non-streaming JSON endpoint
  still runs the full audit. Citation audit still runs and surfaces as a
  separate `citation_warn` progress event after the answer streams.

## Streaming Protocol (`/v1/agent/chat/completions`, `stream: true`)

The agent emits OpenAI-compatible chunks interleaved with these extension
fields:

| Field | When | Shape |
|---|---|---|
| `x_adapter_agent_progress` | every loop boundary / tool call / warning | `{stage, message, ...meta}` |
| `x_adapter_sources` | after each successful `web_search` tool call | **flat array** `[{url, title, favicon, snippet, n}]` — `n` is session-level monotonic id |
| `x_adapter_agent_trace` | once, immediately before `data: [DONE]` | full run trace |

Frontend matches inline `[N]` citations against `x_adapter_sources[*].n`.
The same source URL surfacing in multiple `web_search` calls gets distinct
`n` values (sources event fires per call) — dedup by `url` on the frontend
to render one chip per source.

Real-streaming behaviour (v0.2.13+): for every iteration, the adapter
forwards upstream `delta.content` chunks live the moment the model starts
writing prose. If the model instead emits `delta.tool_calls`, the adapter
swallows those silently, dispatches the tools, and continues the loop —
the frontend never sees partial tool-call markup. Only the forced-answer
fallback path (final iteration still requesting tools, rare) buffers the
synthesized answer and slices it artificially; it is announced with an
`agent_force_answer_fallback` progress event.
