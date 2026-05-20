# adapter

<p align="center">
  <a href="#中文">中文</a> |
  <a href="#english">English</a>
</p>

## 中文

私有或自托管模型服务器的 OpenAI-compatible 适配层。

`adapter` 位于客户端和上游 OpenAI-compatible 模型服务之间，负责把文件输入转换成模型更容易理解的文本/图片上下文，可选地注入联网检索资料，保留流式响应，并把最终请求转发给上游模型。

本项目默认保持部署中立，不包含公司专有服务名、私有服务器地址、内部模型别名或密钥。

### 能力概览

| 能力 | 说明 |
|---|---|
| OpenAI-compatible proxy | 暴露 `/v1/chat/completions`、`/v1/responses`、`/models` 和健康检查端点 |
| 文件输入适配 | 将 `type=file` 内容转换为模型可读的 `text` 和 `image_url` |
| PDF 与 Office 处理 | 可提取文本；在 LibreOffice/PyMuPDF 可用时渲染部分页面、幻灯片或表格为图片 |
| 表格理解 | 保留工作表名、预览行、单元格坐标、公式、缓存值、合并单元格和表格范围 |
| 联网增强 | `/web/v1` 可读取 URL、执行搜索、注入来源上下文，并要求模型引用来源链接 |
| Agentic 联网 | `/v1/agent` 由模型驱动多轮工具调用循环：搜索、抓取、浏览器截图，自带预算控制和引用合规校验 |
| 流式响应 | 保留 SSE streaming，可在模型生成前输出简短进度信息 |
| 安全防护 | 联网读取时阻断 localhost、私有网段、link-local、metadata endpoint 等目标 |
| 可排障性 | 行为显式，便于对比 adapter 转发链路和上游直连链路 |

### 架构

```text
Client / SDK / Tool
        |
        v
adapter (/v1  |  /web/v1  |  /v1/agent)
        |
        v
OpenAI-compatible upstream model server
```

- `/v1` 默认关闭联网：`web_mode=off`
- `/web/v1` 默认自动判断是否联网（被动注入）：`web_mode=auto`
- `/v1/agent` agentic 联网：模型自主多轮工具调用循环

### API 形态

adapter 接受 OpenAI-style 请求。支持文件的客户端可以在 `content` 中传入 `type=file`：

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

联网增强请求示例：

```json
{
  "model": "private-model",
  "messages": [
    {"role": "user", "content": "Read https://example.com and summarize it."}
  ],
  "web_mode": "auto",
  "web_options": {
    "max_urls": 3,
    "max_results": 5,
    "fetch_search_results": 3
  }
}
```

### 支持的输入

| 类型 | 支持情况 | 处理方式 |
|---|---|---|
| 图片 `png/jpg/jpeg/webp/gif` | 支持 | 转换为 `image_url` data URL |
| PDF | 支持 | 使用 `pypdf` 提取文本；使用 PyMuPDF 渲染选定页面 |
| DOCX | 支持 | 提取段落和表格 |
| PPTX | 支持 | 提取幻灯片文本和内嵌图片 |
| XLSX | 支持 | 提取工作表结构、预览行、公式、缓存值、坐标、合并范围和表格范围 |
| CSV/TSV | 支持 | 自动识别分隔符并生成表格预览 |
| Text/Markdown/JSON/XML/HTML/code | 支持 | 按文本解码 |
| 旧版 Office `.doc/.ppt/.xls` | 有限支持 | 建议先转换为现代 Office 格式 |

### 联网能力

| 功能 | 说明 |
|---|---|
| URL fetch | 从最新用户消息中读取显式 `http/https` URL |
| Search | 支持 `searxng`、`baidu`、`bing_html`、`duckduckgo`、`tavily`、`bing` 搜索 provider，主 provider 失败可降级 |
| Source injection | 将标题、URL、正文和检索时间作为不可信外部上下文注入 |
| Citations | 要求模型在基于联网资料回答时列出来源 URL |
| Progress | 流式响应中可输出简短进度提示 |
| History cleanup | 转发前清理历史 assistant 消息中的 adapter 进度文本 |
| SSRF protection | 每次 fetch 和 redirect 都会校验目标地址 |

当客户端已经传入 `system` 或 `developer` 消息时，adapter 会把联网检索上下文合并到第一条控制消息前部，并保留原始指令。这可以降低模型忽略联网资料、退回知识截止时间回答的概率。

### Agentic 联网（`/v1/agent`）

`/web/v1` 是被动联网 —— adapter 自己搜索并把资料注入上下文。`/v1/agent` 是 **agentic 联网** —— 由模型驱动一个多轮工具调用循环，自己决定查什么、查几次、读哪些页面。

```text
POST /v1/agent/chat/completions
```

循环：模型收到带 `tools` 的请求 → 发出 `tool_calls` → adapter 并行执行工具 → 结果回灌 → 循环若干轮 → 模型不再请求工具时输出最终答案。

| 工具 | 作用 |
|---|---|
| `web_search` | 搜索引擎查询，返回标题/URL/摘要列表 |
| `web_fetch` | 抓取 URL 正文；正文为空（如 JS 渲染的 SPA）时自动回退为截图 |
| `web_view` | headless 浏览器（Playwright + Chromium）渲染页面并截图，适合图表、SPA、扫描件，截图作为多模态 `image_url` 回传 |

特性：

- **N 轮预算控制** —— 迭代数 / 搜索数 / 抓取数三类上限，防止失控
- **并行工具调用** —— 单轮多个工具并发执行
- **SSE 流式** —— 实时进度事件 + 最终答案 token 流
- **引用合规校验** —— 标记答案中工具未实际访问过的 URL，识别伪造引用
- **当前日期注入** —— 让模型知道"今天"，避免凭过时记忆作答
- **并发闸** —— 限制同时在飞的 agent 请求，超限返回 HTTP 429

`web_view` 需要镜像内安装 Chromium。agentic 相关环境变量、SearXNG 部署、上线 checklist 见 `DEPLOYMENT.md`。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `ADAPTER_HOST` | `0.0.0.0` | 监听地址 |
| `ADAPTER_PORT` | `8000` | 监听端口 |
| `ADAPTER_UPSTREAM_BASE_URL` | `http://127.0.0.1:8001/v1` | 上游 OpenAI-compatible base URL |
| `ADAPTER_UPSTREAM_API_KEY` | empty | 调用上游时使用的可选 bearer token |
| `ADAPTER_UPSTREAM_AUTH_HEADER` | `Authorization` | 上游鉴权 header |
| `ADAPTER_MAX_FILE_BYTES` | `25MB` | 单文件大小上限 |
| `ADAPTER_MAX_TEXT_CHARS` | `16000` | 每个附件提取文本上限 |
| `ADAPTER_MAX_RENDER_PAGES` | `6` | PDF/Office 渲染页数上限 |
| `ADAPTER_MAX_TABLE_ROWS` | `40` | 表格预览行数上限 |
| `ADAPTER_MAX_TABLE_COLS` | `24` | 表格预览列数上限 |
| `ADAPTER_MAX_SHEETS` | `8` | 工作簿读取 sheet 数上限 |
| `ADAPTER_ENABLE_OFFICE_RENDER` | `1` | 是否启用 LibreOffice 渲染 |
| `ADAPTER_LIBREOFFICE_BIN` | auto | 可选 `soffice` 路径 |
| `ADAPTER_WEB_ENABLED` | `1` | 是否启用联网能力 |
| `ADAPTER_WEB_SEARCH_PROVIDER` | `bing_html` | 搜索 provider |
| `ADAPTER_WEB_PROGRESS_MODE` | `metadata` | 联网进度输出方式，`metadata` 或 `content` |
| `ADAPTER_WEB_FORCE_IPV4` | `1` | 出站 fetch 优先使用 IPv4 |
| `ADAPTER_WEB_AI_NEWS_SOURCE_URLS` | empty | 可选的逗号分隔精选新闻源 URL |
| `TAVILY_API_KEY` | empty | 使用 `tavily` provider 时需要 |
| `BING_SEARCH_API_KEY` | empty | 使用 `bing` provider 时需要 |

### 本地运行

```bash
python3 -m pip install -r requirements.txt
ADAPTER_UPSTREAM_BASE_URL="http://127.0.0.1:8001/v1" \
ADAPTER_PORT=8000 \
python3 adapter.py
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

### Docker

Docker 镜像包含 Python 3.12、LibreOffice、中文字体、Python 解析依赖，以及 agentic `web_view` 工具所需的 Playwright + Chromium。

```bash
docker build -t adapter:local .
docker run --rm -p 8000:8000 \
  -e ADAPTER_UPSTREAM_BASE_URL="http://host.docker.internal:8001/v1" \
  adapter:local
```

### 验证

最小检查：

```bash
python3 -m py_compile adapter.py
```

容器检查：

```bash
docker build -t adapter:check .
docker run --rm adapter:check python -m py_compile /app/adapter.py
```

建议行为检查：

| 检查项 | 期望结果 |
|---|---|
| `GET /health` | adapter 进程存活 |
| 文本请求通过 `/v1/chat/completions` | 上游模型正常回答 |
| PDF/CSV/XLSX 请求 | 文件内容被转换为模型可读上下文 |
| URL 请求通过 `/web/v1/chat/completions` | 答案包含相关来源 URL |
| SSE 请求 | 返回多个流式 chunk |
| 私有 URL fetch 尝试 | 请求被阻断 |

### 安全说明

- 不要提交 API key、provider token、模型服务 token、`.env` 文件或私有 endpoint URL。
- 联网内容会被当作不可信上下文，不能覆盖更高优先级指令。
- SSRF 防护默认启用，但部署时仍建议使用最小网络权限。
- 如果将服务暴露到公网，请在前面增加鉴权、限流和请求大小限制。

### 仓库文件

| 文件 | 说明 |
|---|---|
| `adapter.py` | 主 adapter 服务 |
| `agentic_web.py` | agentic 联网模块：工具 schema、N 轮循环、流式 |
| `Dockerfile` | 生产容器镜像 |
| `requirements.txt` | Python 依赖 |
| `CAPABILITIES.md` | 详细能力矩阵 |
| `DEPLOYMENT.md` | 部署指南：环境变量、SearXNG、上线 checklist |
| `AGENTS.md` | coding agent 协作说明 |
| `scripts/build_remote_image.sh` | 通用远端 registry 构建辅助脚本 |

[Back to top](#adapter) | [English](#english)

## English

An OpenAI-compatible adapter for private or self-hosted model servers.

`adapter` sits between clients and an upstream OpenAI-compatible model endpoint. It normalizes file inputs, optionally enriches requests with public web context, preserves streaming responses, and forwards the final request to the upstream model.

The project is deployment-neutral by default. It does not include company-specific service names, private server addresses, internal model aliases, or secrets.

### Features

| Capability | Description |
|---|---|
| OpenAI-compatible proxy | Exposes `/v1/chat/completions`, `/v1/responses`, `/models`, and health endpoints |
| File input adapter | Converts `type=file` content parts into model-readable `text` and `image_url` parts |
| Office and PDF handling | Extracts text and can render selected pages/slides/sheets visually when LibreOffice/PyMuPDF are available |
| Spreadsheet understanding | Preserves sheet names, preview rows, cell coordinates, formulas, cached values, merged ranges, and table ranges |
| Web augmentation | `/web/v1` can fetch URLs, search the web, inject source context, and ask the model to cite source URLs |
| Agentic web | `/v1/agent` runs a model-driven tool-calling loop: search, fetch, browser screenshot — with budget control and a citation guard |
| Streaming | Preserves SSE streaming and can emit short progress messages before model generation |
| Safety | Blocks localhost, private networks, link-local, metadata endpoints, and other internal targets during web fetches |
| Debuggability | Keeps adapter behavior explicit so deployments can compare adapter-routed requests with direct upstream calls |

### Architecture

```text
Client / SDK / Tool
        |
        v
adapter (/v1  |  /web/v1  |  /v1/agent)
        |
        v
OpenAI-compatible upstream model server
```

- `/v1` defaults to `web_mode=off`.
- `/web/v1` defaults to `web_mode=auto` (passive context injection).
- `/v1/agent` runs an agentic, model-driven tool-calling loop.

### API Shape

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
  "messages": [
    {"role": "user", "content": "Read https://example.com and summarize it."}
  ],
  "web_mode": "auto",
  "web_options": {
    "max_urls": 3,
    "max_results": 5,
    "fetch_search_results": 3
  }
}
```

### Supported Inputs

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

### Web Capability

| Feature | Description |
|---|---|
| URL fetch | Fetches explicit `http/https` URLs from the latest user request |
| Search | Supports `searxng`, `baidu`, `bing_html`, `duckduckgo`, `tavily`, `bing` providers, with fallback when the primary fails |
| Source injection | Adds untrusted web context with title, URL, content, and retrieval time |
| Citations | Instructs the model to list source URLs when answering from web context |
| Progress | Optional visible progress in SSE streams |
| History cleanup | Removes previous adapter progress lines from assistant history before forwarding |
| SSRF protection | Validates target URLs and redirects before every fetch |

If the client already sends `system` or `developer` messages, the adapter prepends web context to the first control message while preserving the original instruction. This helps prevent the model from ignoring retrieved sources and falling back to knowledge-cutoff answers.

### Agentic Web (`/v1/agent`)

`/web/v1` is passive — the adapter searches and injects context itself. `/v1/agent`
is **agentic** — the model drives a multi-iteration tool-calling loop and decides
for itself what to search, how many times, and which pages to read.

```text
POST /v1/agent/chat/completions
```

Loop: the model receives a request with `tools` → emits `tool_calls` → the adapter
runs the tools in parallel → results are fed back → repeat for several iterations
→ the model produces a final answer once it no longer requests tools.

| Tool | Purpose |
|---|---|
| `web_search` | Search-engine query; returns a list of title / URL / snippet |
| `web_fetch` | Fetches page text; auto-falls back to a screenshot when text is empty (e.g. JS-rendered SPAs) |
| `web_view` | Headless browser (Playwright + Chromium) renders a page and screenshots it — for charts, SPAs, scanned pages; the screenshot is returned as a multimodal `image_url` |

Features:

- **N-iteration budget** — caps on iterations / searches / fetches
- **Parallel tool calls** — multiple tools dispatched concurrently per turn
- **SSE streaming** — live progress events plus token-level answer streaming
- **Citation guard** — flags answer URLs the tools never actually visited
- **Current-date injection** — tells the model "today" so stale memory is not trusted
- **Concurrency gate** — caps in-flight agent requests; excess returns HTTP 429

`web_view` requires Chromium in the image. See `DEPLOYMENT.md` for agentic
environment variables, SearXNG setup, and the deploy checklist.

### Environment Variables

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
| `ADAPTER_MAX_TABLE_ROWS` | `40` | Max table preview rows |
| `ADAPTER_MAX_TABLE_COLS` | `24` | Max table preview columns |
| `ADAPTER_MAX_SHEETS` | `8` | Max workbook sheets |
| `ADAPTER_ENABLE_OFFICE_RENDER` | `1` | Enable LibreOffice rendering |
| `ADAPTER_LIBREOFFICE_BIN` | auto | Optional path to `soffice` |
| `ADAPTER_WEB_ENABLED` | `1` | Enable web capability |
| `ADAPTER_WEB_SEARCH_PROVIDER` | `bing_html` | Search provider |
| `ADAPTER_WEB_PROGRESS_MODE` | `metadata` | Web progress mode, `metadata` or `content` |
| `ADAPTER_WEB_FORCE_IPV4` | `1` | Prefer IPv4 for outbound fetches |
| `ADAPTER_WEB_AI_NEWS_SOURCE_URLS` | empty | Optional comma-separated curated news URLs |
| `TAVILY_API_KEY` | empty | Tavily API key when using `tavily` provider |
| `BING_SEARCH_API_KEY` | empty | Bing Search API key when using `bing` provider |

### Local Run

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

### Docker

The Docker image includes Python 3.12, LibreOffice, Chinese fonts, Python parsing dependencies, and Playwright + Chromium for the agentic `web_view` tool.

```bash
docker build -t adapter:local .
docker run --rm -p 8000:8000 \
  -e ADAPTER_UPSTREAM_BASE_URL="http://host.docker.internal:8001/v1" \
  adapter:local
```

### Validation

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

### Security Notes

- Do not commit API keys, provider tokens, model-server tokens, `.env` files, or private endpoint URLs.
- Web content is treated as untrusted context and must not override higher-priority instructions.
- SSRF protection is enabled by default, but deployments should still run the adapter with least network privilege.
- If you expose this service publicly, put authentication, rate limits, and request-size limits in front of it.

### Repository Files

| File | Purpose |
|---|---|
| `adapter.py` | Main adapter service |
| `agentic_web.py` | Agentic web module: tool schemas, N-iteration loop, streaming |
| `Dockerfile` | Production container image |
| `requirements.txt` | Python dependencies |
| `CAPABILITIES.md` | Detailed capability matrix |
| `DEPLOYMENT.md` | Deployment guide: env vars, SearXNG, deploy checklist |
| `AGENTS.md` | Instructions for coding agents |
| `scripts/build_remote_image.sh` | Generic remote-registry build helper |

[Back to top](#adapter) | [中文](#中文)
