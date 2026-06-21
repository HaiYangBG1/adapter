# Changelog — adapter

> 倒序(最新在上)。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
> 每个版本的**详细上线记录**(SAE ChangeOrder / 镜像 digest / 验证证据)见
> `../lxj-adapter-deploy/runbooks/deploy-YYYY-MM-DD-adapter-vX.Y.Z-*.md`。
> 线上实际版本以 `/health` 返回的 `version` + `git_sha` 为准。

---

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
