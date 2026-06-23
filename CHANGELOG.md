# Changelog — adapter

> 倒序(最新在上)。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
> 每个版本的**详细上线记录**(SAE ChangeOrder / 镜像 digest / 验证证据)见
> `../lxj-adapter-deploy/runbooks/deploy-YYYY-MM-DD-adapter-vX.Y.Z-*.md`。
> 线上实际版本以 `/health` 返回的 `version` + `git_sha` 为准。

---

## [v0.6.3-20260623] — viz 方案2:generate_html 自由生成 + 卡片一致 · ✅ 已上线(live 验证 gated)
> PM 拍「方案2 自由写 HTML + 卡片一致」(decisions 2026-06-23 viz 行)。解 v0.6.1/v0.6.2 的「模型
> 在工具长 string arg 写空壳」:**短 brief 工具参数 + 拦截后单独自由生成调用**两全。
> - `GENERATE_HTML_TOOL` 改收短 `brief`(模型填短参不偷懒,绕开长 string arg 弱点);`FILE_GEN_PROMPT` html 段同步。
> - adapter `HTML_BUILDER_PROMPT` + `_call_upstream_html_builder`(无工具、自由生成的上游调用,模型写完整含
>   chart.js 的 HTML,自由写=强项)+ `_strip_md_fence`(抠净 HTML 去围栏);`_render` special-case generate_html
>   走 builder→`html_generator.build_html`(full-doc 直用)→OSS→文件卡(与 pptx/xlsx 同款三态卡,UX 一致)。
>   env 旋钮 `ADAPTER_HTML_BUILDER_TIMEOUT`(默认 150s)/`ADAPTER_HTML_BUILDER_MAX_TOKENS`(默认 8000)。
> - 本地全验:`_strip_md_fence`(raw / fenced+解释抠净 script 保留)+ renderer html 分支(mock 上游)+ 空返回
>   优雅 error + 5 类型 dispatch 重构无回归;镜像内自检 file_gen 5 类。git `be8c9da`,digest `576a6ce7…`,
>   ChangeOrder `e5781d4e`。image-only(env 不动)。
> - ⚠️ **live 验证 gated**:B6 文件路由鉴权(前端 0.17.4)后无法无登录 BFF 触发 gen_file 验证 → 交 PM 登录态
>   实测「给我一个可视化看板」是否出 chart.js 图 / 或测试域 authed E2E。模型自由写富 HTML 有截图实证,风险有限。

## [v0.6.2-20260623] — generate_html 允许 js(可视化)· ✅ 已上线(⚠️ viz 成色待解)
> PM 拍「允许 js」(decisions 2026-06-23):让「可视化看板」能有 chart.js 交互图。
> - **v0.6.1**:`html_generator` 停止消毒,允许 `<script>/<style>/图表库`;full-doc 直用 / fragment 套壳。
>   爆炸半径界定(模块注释):产物是下载件,用户 file:// 打开 = 隔离 origin,够不到我方应用;前端
>   从不在应用内渲染该 HTML(Artifact 只有 downloadUrl、previewKind=none)。`GENERATE_HTML_TOOL` +
>   `FILE_GEN_PROMPT` 改「可写完整页 + 图表库」。git `b0d0a29`,digest `9c73de2d…`,ChangeOrder `3f4e1711`。
> - **v0.6.2**:强化 html prompt(逼模型写完整含图表页面、严禁空壳)。git `2aaf77c`,digest `548a045a…`,ChangeOrder `58f8d09e`。
> - ✅ 代码验证:本地 chart.js/script 保留 + full-doc 不双套壳 + 集成 dispatch 5 类型全过;生产新外壳实证(960px)。
> - ⚠️ **未达成 viz**:实测模型在 **gen_file 工具模式**下把 `html`(超长 string arg)写成**空壳**(只 `<h2>标题</h2>`,1288B),
>   即便强 prompt 也压不动;**同版本同模式 xlsx(结构化 arg)5341B 富** —— 确证是 LLM「长自由文本塞进工具参数偷懒」
>   的弱点(对比普通对话自由生成能写完整 chart.js 看板)。**修复方向待 PM 拍**(结构化图表渲染 vs 自由写 HTML 提取)。

## [v0.6.0-20260622] — B+ 多类型文件生成 + 自动识别 · ✅ 已上线生产
> 五期 B+(decisions 2026-06-22)。把 PPTX 生成泛化为**多类型**(+ Excel/CSV/Word/HTML)+
> **模型自动识别**触发。纯加法,**显式 PPTX(`gen_pptx`)路径行为不变**(无回归)。
> 本地全验:py_compile 全绿 + 5 类型渲染→reopen 校验 + 集成 dispatch 自测 + HTML 消毒单测 +
> reviewer 核查门(无 P0;P1 已修)。
> **部署证据(2026-06-22)**:镜像 `172.29.0.223:5000/lxj/adapter:v0.6.0-20260622`,digest
> `sha256:184c9315fca02bbea5372b6fc4b8a1a4f37a2157d60c4fb42690aa19bd3d3440`,git `b47c5c9`。
> **智能增量构建**(FROM v0.5.1-20260621 + COPY *.py〈9 模块,无新 pip 依赖〉)+ 镜像内自检
> `SELFCHECK_OK file_gen exts: csv/docx/html/pdf/pptx/xlsx`。SAE image-only 滚动部署(ChangeOrder
> `7d16f624-…` Status→2 ~90s;**env 不动**——`ADAPTER_ENABLE_PPTX_GEN` 回退逻辑使 file_gen 自动
> 启用、OSS env 沿用 B2;PreStop sleep25 保留)。**线上烟测全 PASS**(经生产前端 BFF):PPTX 回归
> 真 8 页 pptx + 多类型 gen_file 模型自选 xlsx/docx/csv/html 各合法可下载 + HTML 消毒在线生效
> + 普通 agent 无误触发 artifact。
### Added (v0.6.0)
- **4 个确定性生成器**(模型只出结构化数据,A 铁律):`xlsx_generator.py`(openpyxl,多 sheet +
  表头样式 + 冻结窗格 + 列宽)/ `docx_generator.py`(python-docx,标题/小节/段落/项目符号)/
  `csv_generator.py`(stdlib,UTF-8 BOM 便于 Excel)/ `html_generator.py`(stdlib;模型写正文
  HTML → **消毒** 去 script/style/外链/事件处理器/`javascript:` 后套干净文档外壳)。
  共享助手 `file_gen_common.py`(clean_text/clamp/as_cell/safe_filename/accent)。
- **`agentic_web.py` 泛化**:新增 `GENERATE_{XLSX,DOCX,CSV,HTML}_TOOL` schema + `FILE_GEN_PROMPT`
  (自动识别系统提示,不强制调工具)+ `ALL_FILE_GEN_TOOLS` + `FILE_GEN_TOOL_META`(tool→kind/
  mime/ext/preview);`generate_pptx` inline 拦截分支泛化为按 tool name 路由任一 `generate_*`。
- **`gen_file` 自动多类型触发**:挂全部 `generate_*`、`tool_choice=auto`,模型自决类型/是否生成。
  与 `gen_pptx`(显式 PPTX)互斥并存,`gen_pptx` 优先。两字段均不透传上游。
- **`/health`** 加 `file_gen_enabled` + `file_gen_types`(保留 `pptx_gen_enabled` 兼容键);
  重签端点 ext 白名单加 `csv`/`html`。
### Changed (v0.6.0)
- `AgentConfig.enable_pptx_gen`→`enable_file_gen`;`pptx_renderer`→`file_renderer`(签名加
  `tool_name` 首参);per-session `pptx_dispatch_done`/`PPTX_SYNTHESIS_HINT`→`file_gen_*`/
  `FILE_GEN_SYNTHESIS_HINT`(措辞泛化)。`adapter._make_pptx_renderer`→`_make_file_renderer`
  (按 tool name 分发到 5 个 generator)。master switch `ADAPTER_ENABLE_FILE_GEN`(回退旧名
  `ADAPTER_ENABLE_PPTX_GEN`,运维平滑)。`Dockerfile` COPY 5 个新模块。openpyxl/python-docx
  早已在 requirements(输入侧),生成侧复用,**无新增依赖**(csv/html 为 stdlib)。

## [v0.5.1] - 2026-06-21 — 文件生成 MVP(PPTX)· ✅ 已暗部署 + 线上闭环 PASS
> 五期 B(文件生成 MVP)。**暗部署**:整条通路被 per-request `gen_pptx` 标志门控,
> 前端无入口发该标志 → 真实用户够不着,部署对现有 chat/excel/web **零影响**(已实测回归)。
> **部署证据**:镜像 `v0.5.1-20260621`(digest `sha256:3a71d09…`,git `76578de`);
> 智能增量构建(FROM v0.4.5 + pip install python-pptx/oss2〈aliyun mirror〉+ COPY 4 模块);
> SAE 滚动 Batch=1(2 副本始终 ≥1 服务,PreStop 保留)。OSS = 桶 `lxj-ai-center` 前缀
> `ai-center/artifacts/`(lifecycle TTL 14d)+ RAM 子账号最小权限(仅该前缀 Put/Get)。
> **线上烟测全 PASS**:K2.6→大纲→python-pptx 渲染→OSS→presigned 下载真 9 页 pptx;
> 普通 agent 回归正常无误触发。详见 `../lxj-adapter-deploy/runbooks/deploy-2026-06-21-pptx-filegen-darklaunch.md`。
### Fixed (v0.5.1)
- **presign 去 `response-content-type` 覆盖**(线上实测 OSS 400 `InvalidRequest: Can not
  override response header on content-type`)。该覆盖冗余 —— 对象上传时已带正确 Content-Type;
  保留 `response-content-disposition`(下载文件名)。实测 disposition-only → 200 真 pptx。
### Added (v0.5.0 → 并入 v0.5.1)
- **PPTX 确定性生成**(A 铁律:模型只出大纲数据,渲染是写死代码):
  - `pptx_generator.py`(新):outline JSON → python-pptx 套模板渲染 → `.pptx` bytes。
    防御式 normalize(容忍模型松散输出),16:9 模板,封面 + 正文页(标题/要点/备注),
    页码/单主色(`PPTX_ACCENT_COLOR` env,默认绿)。generic/open-source safe,全 env 驱动。
  - `oss_store.py`(新):oss2 上传 + presigned GET(短时效,默认 15min)。**双端点**:
    上传走内网 `OSS_INTERNAL_ENDPOINT`、presign 走公网 `OSS_PUBLIC_ENDPOINT`(浏览器可达)。
    objectKey 确定性 `{prefix}{id}.{ext}`(无需 id→key 映射表);Content-Disposition 支持 CJK
    文件名(RFC5987)。缺 oss2/未配 env 时优雅降级。🔴 凭据只读 env,绝不落盘。
  - `agentic_web.py`:`generate_pptx` 工具(register_schema_only,inline 拦截,仿
    submit_analysis_plan)+ `_sse_artifact_chunk`(顶层 `x_adapter_artifact` 信封,三态
    generating→ready→error,同 id 覆盖)+ `AgentConfig.enable_pptx_gen/pptx_renderer`
    (依赖注入,agentic_web 不知 pptx/OSS 细节)+ run_agent_stream 拦截分支 +
    `PPTX_GEN_PROMPT` / `PPTX_SYNTHESIS_HINT`。
  - `adapter.py`:`gen_pptx` per-request flag(顶层或 extra_body)→ pptx_mode(优先级最高、
    与 excel/web 互斥),`force_first_tool_name=generate_pptx` 首轮强制 emit 大纲,
    `_make_pptx_renderer()` 注入渲染/存储。`GET /v1/artifact/{id}/url` presigned 重签端点
    (id 形态校验 + ext 白名单,防遍历/MIME 欺骗)。`/health` 加 `pptx_gen_enabled` +
    `object_storage` 就绪快照(非敏感)。
  - `requirements.txt`:`python-pptx>=1.0.0` + `oss2>=2.18.0`(缺失时该能力降级,adapter 仍启动)。
  - `Dockerfile`:COPY `pptx_generator.py` / `oss_store.py`。
### Notes
- 生产已注入 env(SAE,🔴 OSS AK/SK 只在运行时 env):`OSS_INTERNAL_ENDPOINT` /
  `OSS_PUBLIC_ENDPOINT` / `OSS_BUCKET` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` /
  `OSS_ARTIFACT_PREFIX` / `OSS_PRESIGN_EXPIRE_SECONDS` / `ADAPTER_ENABLE_PPTX_GEN`(全表见 DEPLOYMENT.md)。
- **剩余(非本次)= 前端 `gen_pptx` 触发入口**(产品 UX 待 PM 拍;后端契约已就绪,带 `gen_pptx:true` 走 `/api/agent` 即触发)。在它 ship 之前,功能对用户不可见(暗部署)。
- 契约:`../../contracts/PROTOCOL.md` §SSE + `../../llm-playground-pro/docs/BACKEND_REQUESTS_artifact_5期.md`。reviewer 核查门过(P1 已修)。

## [v0.4.5] - 2026-06-18
### Fixed
- 大表分析「多表盲查」超时根治(数据集十几张利润表的 case 全超时):
  - **plan prompt(agentic_web.py:449-463)**:原 prompt 鼓励全并行(`depends_on` 空)、连
    「先看表结构」都说"并行不耽误时间" → 查询 step 不等结构就在十几张表里盲猜表名列名、
    反复试错撞 240s。改成:数据集表多/结构不确定时,让查询 step `depends_on`「看表结构」step;
    表结构清楚时仍并行。
  - **step retry 跳过 timeout(agentic_web.py:2484)**:timeout 类失败不 retry(retry 同样
    撞 EXCEL_QUERY_TIMEOUT,只把 240s 白翻成 480s);只对瞬时错误(HTTP 5xx)retry。
    判据收窄为 `"timed out"`(不用宽 `"timeout"`,避免误杀 504 body,reviewer P1)。
- 配合部署:`ADAPTER_EXCEL_QUERY_TIMEOUT` 240→360 给复杂多表查询更多时间。
- 后续(未做):step_schema 直接读 profile json 零 LLM(需 excel-poc schema 端点)。

## [v0.4.4] - 2026-06-18
### Fixed
- 补 K2.6 `thinking` 字段(原来只塞 Qwen 旧名 `enable_thinking`,对 K2.6/vLLM0.18
  **静默失效**)—— adapter 的「关 thinking」(中间轮/force_answer/空响应重试,走
  `_build_no_thinking_extra`)与「检测 client 是否要 thinking」(`_client_wants_thinking`;
  前端 v0.9.0 起发 `thinking` 而非 `enable_thinking`)一直对 K2.6 失效。两处双写两字段修复。
  配合 excel-poc v0.2.21(写 SQL 默认关 thinking)治大表分析超时(reviewer 抓到 P0:
  检测字段不一致会静默忽略用户深度思考意图,已修)。详见 runbook deploy-2026-06-18-thinking-speedup。

## [v0.4.3] - 2026-06-17
### Fixed
- **带 tools 的请求跳过采样 penalty 注入** —— 治 `lxj` 工具调用乱码:代码/JSON 等
  结构化长输出里换行/缩进/关键字是合法高频 token,`frequency_penalty=0.3` +
  `presence_penalty=0.2`(v0.2.27 为自然语言防 thinking 死循环加的)会压低它们 →
  `tool_call` arguments 退化成词链死循环、JSON 不闭合。两处注入点都加豁免:
  `adapter.py _transform_payload`(`/v1` 直转)+ `agentic_web.py`(agent loop)。
  判据:`bool(payload.get("tools")) or payload.get("tool_choice") not in (None,"none")`。
  普通对话不带 tools → penalty 照旧;client 显式传 penalty 始终尊重。
  详见 `runbooks/deploy-2026-06-17-adapter-v0.4.3-penalty-tool-exempt.md`。

## [v0.4.2] - 2026-05-29
### Added
- plan step 失败 retry 机制(`ADAPTER_AGENT_PLAN_STEP_MAX_RETRIES`,默认 1)——
  下游 excel-poc 偶发 HTTP 500 时自动 retry 兜底。

## [v0.4.1] - 2026-05-28
### Fixed
- intent-leak synth fallback(意图泄漏时综合兜底作答)。

## [v0.4.0] - 2026-05-28
### Changed
- plan-and-execute 架构重构(plan refactor)。

---

> v0.3.x 及更早的演进串见 `../lxj-adapter-deploy/runbooks/deploy-2026-05-*` 系列。
