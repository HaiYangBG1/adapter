# adapter

私有或自托管模型服务器的 OpenAI-compatible 适配层。

English: An OpenAI-compatible adapter for private or self-hosted model servers.

`adapter` 位于客户端和上游 OpenAI-compatible 模型服务之间，负责把文件输入转换成模型更容易理解的文本/图片上下文，可选地注入联网检索资料，保留流式响应，并把最终请求转发给上游模型。

English: `adapter` sits between clients and an upstream OpenAI-compatible model endpoint. It normalizes file inputs, optionally enriches requests with public web context, preserves streaming responses, and forwards the final request to the upstream model.

本项目默认保持部署中立，不包含公司专有服务名、私有服务器地址、内部模型别名或密钥。

English: This project is deployment-neutral and does not include company-specific service names, private server addresses, internal model aliases, or secrets.

## 能力概览 / Features

| 能力 | 中文说明 | English |
|---|---|---|
| OpenAI-compatible proxy | 暴露 `/v1/chat/completions`、`/v1/responses`、`/models` 和健康检查端点 | Exposes common OpenAI-compatible chat, responses, models, and health endpoints |
| 文件输入适配 | 将 `type=file` 内容转换为模型可读的 `text` 和 `image_url` | Converts `type=file` content parts into model-readable `text` and `image_url` parts |
| PDF 与 Office 处理 | 可提取文本；在 LibreOffice/PyMuPDF 可用时渲染部分页面、幻灯片或表格为图片 | Extracts text and can render selected pages/slides/sheets visually when LibreOffice/PyMuPDF are available |
| 表格理解 | 保留工作表名、预览行、单元格坐标、公式、缓存值、合并单元格和表格范围 | Preserves sheet names, preview rows, cell coordinates, formulas, cached values, merged ranges, and table ranges |
| 联网增强 | `/web/v1` 可读取 URL、执行搜索、注入来源上下文，并要求模型引用来源链接 | `/web/v1` can fetch URLs, search the web, inject source context, and ask the model to cite source URLs |
| 流式响应 | 保留 SSE streaming，可在模型生成前输出简短进度信息 | Preserves SSE streaming and can emit short progress messages before model generation |
| 安全防护 | 联网读取时阻断 localhost、私有网段、link-local、metadata endpoint 等目标 | Blocks localhost, private networks, link-local, metadata endpoints, and similar targets during web fetches |
| 可排障性 | 行为显式，便于对比 adapter 转发链路和上游直连链路 | Keeps behavior explicit so deployments can compare adapter-routed requests with direct upstream calls |

## 架构 / Architecture

```text
Client / SDK / Tool
        |
        v
adapter (/v1 or /web/v1)
        |
        v
OpenAI-compatible upstream model server
```

- `/v1` 默认关闭联网：`web_mode=off`
- `/web/v1` 默认自动判断是否联网：`web_mode=auto`

English:

- `/v1` defaults to `web_mode=off`.
- `/web/v1` defaults to `web_mode=auto`.

## API 形态 / API Shape

adapter 接受 OpenAI-style 请求。支持文件的客户端可以在 `content` 中传入 `type=file`：

English: The adapter accepts OpenAI-style requests. File-capable clients can pass `type=file` content parts.

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

English: Example web-augmented request:

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

## 支持的输入 / Supported Inputs

| 类型 | 支持情况 | 中文处理方式 | English |
|---|---|---|---|
| 图片 `png/jpg/jpeg/webp/gif` | 支持 | 转换为 `image_url` data URL | Converts to `image_url` data URLs |
| PDF | 支持 | 使用 `pypdf` 提取文本；使用 PyMuPDF 渲染选定页面 | Extracts text with `pypdf`; renders selected pages with PyMuPDF |
| DOCX | 支持 | 提取段落和表格 | Extracts paragraphs and tables |
| PPTX | 支持 | 提取幻灯片文本和内嵌图片 | Extracts slide text and embedded images |
| XLSX | 支持 | 提取工作表结构、预览行、公式、缓存值、坐标、合并范围和表格范围 | Extracts sheet structure, preview rows, formulas, cached values, coordinates, merged ranges, and table ranges |
| CSV/TSV | 支持 | 自动识别分隔符并生成表格预览 | Detects delimiter and produces table previews |
| Text/Markdown/JSON/XML/HTML/code | 支持 | 按文本解码 | Decodes as text |
| 旧版 Office `.doc/.ppt/.xls` | 有限支持 | 建议先转换为现代 Office 格式 | Convert to modern Office formats first |

## 联网能力 / Web Capability

| 功能 | 中文说明 | English |
|---|---|---|
| URL fetch | 从最新用户消息中读取显式 `http/https` URL | Fetches explicit `http/https` URLs from the latest user request |
| Search | 支持 `bing_html`、`duckduckgo`、`tavily` 和 `bing` 搜索 provider | Supports `bing_html`, `duckduckgo`, `tavily`, and `bing` providers |
| Source injection | 将标题、URL、正文和检索时间作为不可信外部上下文注入 | Adds untrusted web context with title, URL, content, and retrieval time |
| Citations | 要求模型在基于联网资料回答时列出来源 URL | Instructs the model to list source URLs when answering from web context |
| Progress | 流式响应中可输出简短进度提示 | Optional visible progress in SSE streams |
| History cleanup | 转发前清理历史 assistant 消息中的 adapter 进度文本 | Removes previous adapter progress lines from assistant history before forwarding |
| SSRF protection | 每次 fetch 和 redirect 都会校验目标地址 | Validates target URLs and redirects before every fetch |

当客户端已经传入 `system` 或 `developer` 消息时，adapter 会把联网检索上下文合并到第一条控制消息前部，并保留原始指令。这可以降低模型忽略联网资料、退回知识截止时间回答的概率。

English: If the client already sends `system` or `developer` messages, the adapter prepends web context to the first control message while preserving the original instruction. This helps prevent the model from ignoring retrieved sources and falling back to knowledge-cutoff answers.

## 环境变量 / Environment Variables

| 变量 | 默认值 | 中文说明 | English |
|---|---:|---|---|
| `ADAPTER_HOST` | `0.0.0.0` | 监听地址 | Listen host |
| `ADAPTER_PORT` | `8000` | 监听端口 | Listen port |
| `ADAPTER_UPSTREAM_BASE_URL` | `http://127.0.0.1:8001/v1` | 上游 OpenAI-compatible base URL | Upstream OpenAI-compatible base URL |
| `ADAPTER_UPSTREAM_API_KEY` | empty | 调用上游时使用的可选 bearer token | Optional bearer token used when calling upstream |
| `ADAPTER_UPSTREAM_AUTH_HEADER` | `Authorization` | 上游鉴权 header | Header used for upstream auth |
| `ADAPTER_MAX_FILE_BYTES` | `25MB` | 单文件大小上限 | Max accepted file size |
| `ADAPTER_MAX_TEXT_CHARS` | `16000` | 每个附件提取文本上限 | Max extracted text chars per attachment |
| `ADAPTER_MAX_RENDER_PAGES` | `6` | PDF/Office 渲染页数上限 | Max PDF/Office pages rendered as images |
| `ADAPTER_MAX_TABLE_ROWS` | `40` | 表格预览行数上限 | Max table preview rows |
| `ADAPTER_MAX_TABLE_COLS` | `24` | 表格预览列数上限 | Max table preview columns |
| `ADAPTER_MAX_SHEETS` | `8` | 工作簿读取 sheet 数上限 | Max workbook sheets |
| `ADAPTER_ENABLE_OFFICE_RENDER` | `1` | 是否启用 LibreOffice 渲染 | Enable LibreOffice rendering |
| `ADAPTER_LIBREOFFICE_BIN` | auto | 可选 `soffice` 路径 | Optional path to `soffice` |
| `ADAPTER_WEB_ENABLED` | `1` | 是否启用联网能力 | Enable web capability |
| `ADAPTER_WEB_SEARCH_PROVIDER` | `bing_html` | 搜索 provider | Search provider |
| `ADAPTER_WEB_PROGRESS_MODE` | `metadata` | 联网进度输出方式，`metadata` 或 `content` | Web progress mode, `metadata` or `content` |
| `ADAPTER_WEB_FORCE_IPV4` | `1` | 出站 fetch 优先使用 IPv4 | Prefer IPv4 for outbound fetches |
| `ADAPTER_WEB_AI_NEWS_SOURCE_URLS` | empty | 可选的逗号分隔精选新闻源 URL | Optional comma-separated curated news URLs |
| `TAVILY_API_KEY` | empty | 使用 `tavily` provider 时需要 | Tavily API key when using `tavily` provider |
| `BING_SEARCH_API_KEY` | empty | 使用 `bing` provider 时需要 | Bing Search API key when using `bing` provider |

## 本地运行 / Local Run

```bash
python3 -m pip install -r requirements.txt
ADAPTER_UPSTREAM_BASE_URL="http://127.0.0.1:8001/v1" \
ADAPTER_PORT=8000 \
python3 adapter.py
```

健康检查：

English: Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Docker

Docker 镜像包含 Python 3.12、LibreOffice、中文字体和 Python 解析依赖。

English: The Docker image includes Python 3.12, LibreOffice, Chinese fonts, and Python parsing dependencies.

```bash
docker build -t adapter:local .
docker run --rm -p 8000:8000 \
  -e ADAPTER_UPSTREAM_BASE_URL="http://host.docker.internal:8001/v1" \
  adapter:local
```

## 验证 / Validation

最小检查：

English: Minimum check:

```bash
python3 -m py_compile adapter.py
```

容器检查：

English: Container check:

```bash
docker build -t adapter:check .
docker run --rm adapter:check python -m py_compile /app/adapter.py
```

建议行为检查：

English: Suggested behavior checks:

| 检查项 | 期望结果 | Expected |
|---|---|---|
| `GET /health` | adapter 进程存活 | Adapter process is alive |
| 文本请求通过 `/v1/chat/completions` | 上游模型正常回答 | Upstream model answers normally |
| PDF/CSV/XLSX 请求 | 文件内容被转换为模型可读上下文 | File content is converted into model-readable context |
| URL 请求通过 `/web/v1/chat/completions` | 答案包含相关来源 URL | Answer includes relevant source URL |
| SSE 请求 | 返回多个流式 chunk | Multiple chunks are streamed |
| 私有 URL fetch 尝试 | 请求被阻断 | Request is blocked |

## 安全说明 / Security Notes

- 不要提交 API key、provider token、模型服务 token、`.env` 文件或私有 endpoint URL。
- 联网内容会被当作不可信上下文，不能覆盖更高优先级指令。
- SSRF 防护默认启用，但部署时仍建议使用最小网络权限。
- 如果将服务暴露到公网，请在前面增加鉴权、限流和请求大小限制。

English:

- Do not commit API keys, provider tokens, model-server tokens, `.env` files, or private endpoint URLs.
- Web content is treated as untrusted context and must not override higher-priority instructions.
- SSRF protection is enabled by default, but deployments should still run the adapter with least network privilege.
- If you expose this service publicly, put authentication, rate limits, and request-size limits in front of it.

## 仓库文件 / Repository Files

| 文件 | 中文说明 | English |
|---|---|---|
| `adapter.py` | 主 adapter 服务 | Main adapter service |
| `Dockerfile` | 生产容器镜像 | Production container image |
| `requirements.txt` | Python 依赖 | Python dependencies |
| `CAPABILITIES.md` | 详细能力矩阵 | Detailed capability matrix |
| `AGENTS.md` | coding agent 协作说明 | Instructions for coding agents |
| `scripts/build_remote_image.sh` | 通用远端 registry 构建辅助脚本 | Generic remote-registry build helper |
