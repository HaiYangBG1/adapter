# Changelog — adapter

> 倒序(最新在上)。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
> 每个版本的**详细上线记录**(SAE ChangeOrder / 镜像 digest / 验证证据)见
> `../lxj-adapter-deploy/runbooks/deploy-YYYY-MM-DD-adapter-vX.Y.Z-*.md`。
> 线上实际版本以 `/health` 返回的 `version` + `git_sha` 为准。

---

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
