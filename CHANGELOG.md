# Changelog — adapter

> 倒序(最新在上)。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
> 每个版本的**详细上线记录**(SAE ChangeOrder / 镜像 digest / 验证证据)见
> `../lxj-adapter-deploy/runbooks/deploy-YYYY-MM-DD-adapter-vX.Y.Z-*.md`。
> 线上实际版本以 `/health` 返回的 `version` + `git_sha` 为准。

---

## [v0.6.19-20260710] — agent 流稳态三修:K2.6 reasoning 字段 + 静默保活 chunk + 上游瞬时错误重试 · ✅ 已上线 2026-07-10
> **背景(异常看板两桩未解决自动上报)**:① 朱鹏飞 2026-07-09 14:45「上游模型生成超时/暂时不可用」(问「可以生成可视化操作界面吗」,走 `/v1/agent`,agent_error=upstream timeout/5xx);② 何金妮 2026-07-08 16:18「Error in input stream」(普通提问 `chick-lxj`,浏览器读 SSE 中断,**`ai_answer` 空 = 一个字都没到就断**)。
> **根因链**(Langfuse 两时间点均无对应 trace = 死在中段;ALB/CLB/credential 均 CLI live 实查):
> 1. **60s 静默墙**:LiteLLM ALB(`llm.lxjchina.com.cn`,`lsn-2fco4wsu8dia1xl301`)`RequestTimeout=60s`/idle 15s —— 链路上任何一段静默超 60s 即被掐流。**何金妮案**:`chick-lxj` credential(`lxj_ppu`)live 实证指向 adapter **`/v1` 纯透传**(非 agent loop),首 token/prefill 排队期上游零字节 → ALB 60s 掐 BFF↔LiteLLM 连接 → 浏览器「Error in input stream」(`ai_answer` 空佐证)。→ **ops 修**:ALB `RequestTimeout` 60→180s(可逆,随本次上线一并调)。
> 2. **既有心跳护不到 LiteLLM 中转段**:v0.6.16 写出层 `: hb` **SSE 注释**心跳只在「adapter 直连 BFF」(`/api/agent`)段有效。**live 穿透实测(2026-07-10 上线后,chick-lxj-web 经 LiteLLM 全链帧普查)**:LiteLLM 对 SSE 重组转发,**`: hb` 注释、`x_adapter_agent_progress` 扩展块、空 delta 块一律不透传**(下游只见 content 6 帧 + finish 1 帧 + [DONE];adapter 侧确证发过 iteration_start)。∴ **任何 adapter 侧心跳都救不了 LiteLLM→ALB→BFF 段的静默,该段唯一解 = ALB `RequestTimeout` 60→180s(平台上限,已调)**;本次新增的空 delta 保活 chunk 有效护段 = `/api/agent` 直连全链 + adapter→LiteLLM 一跳(防 LiteLLM 读上游超时)。
> 3. **K2.6 思考字段错配(agent loop 路径)**:vLLM kimi_k2 parser 把思考放 `delta.reasoning`,`_speculative_iteration` 只认 Qwen/SGLang 时代的 `reasoning_content` → agent 路径 thinking 期间 chunk 全被当「role-only 块」缓冲,**对下游整段零字节**;且 `reasoning_buf` 恒空 → v0.2.27 思考死循环防护、v0.2.14 reasoning 兜底对 K2.6 一直失效。
> 4. **上游零重试**:`path=upstream_error` 直接终局 `agent_error`,一次瞬时抖动就把整轮判死(朱鹏飞案)。
>
> **改**(`agentic_web.py` + `adapter.py` 一处常量;对前端/契约零破坏,PROTOCOL §4 2026-07-10 行):
> - **认双字段**:`reasoning_delta = reasoning_content ∥ reasoning`(chunk 分类/缓冲行为不变,只修累积)。配套:死循环护栏 `ADAPTER_MAX_REASONING_CHARS` 默认 **16000→60000 字符**(≈17K token;旧值是 Qwen3 INT8 死循环时代定的,直接对 K2.6 生效会误杀合法长思考、静默降级为禁 thinking 重试 —— reviewer P1;上线后观测 `reasoning_too_long` 触发率再收紧)。
> - **静默保活 chunk**:新 `_iter_upstream_with_ticks`(后台线程读上游,主线程 15s 无数据吐 tick;消费方提前退出时 stop event 令 pump 在 chunk 边界退出释放上游,上界=socket 读超时 120s)+ `_speculative_iteration` 距上次真实下发 ≥`_UPSTREAM_KEEPALIVE_SECS=15s` 补一个空 delta 保活块(`id:"agent-keepalive"`,无扩展字段,前端/OpenAI SDK 天然忽略);真实下发处同步回写计时器(reviewer P2,对齐 v0.6.16 `_locked_write` 模式)。覆盖 thinking / tool_calls 拼装轮 / 首 token 排队三类静默。
> - **瞬时错误重试**:新 cfg `max_upstream_error_retries=1` —— `upstream_error` 且错误匹配 timeout/5xx/gateway/连接被掐、且 `visible_buf` 为空(未向用户下发任何可见正文,重试不会正文重复)→ 发 progress `agent_upstream_retry`(「上游模型瞬时超时,自动重试中…」)原样重试一次;仍败/4xx/已有可见正文 → 照旧 `agent_error` 终局。前端 `Playground.tsx` 已加该 stage 文案(随下次前端发版;未知 stage 本就安全忽略)。
>
> **自验**:行为 harness 14 case 全 PASS(reasoning 双字段累积 / thinking 静默保活 / 上游全静默 tick 保活 / 活跃出字流不插保活 / 瞬时 timeout 重试成功且上游只调 2 次 / HTTP 400 不重试 / 已有可见正文不重试 / 消费方退出 pump 释放)+ `py_compile` 绿 + reviewer 核查门(2 P1 已修:v0.6.16 心跳关系论证 + 阈值放宽;2 P2 已修:计时器语义 + 释放上界注释)。
> **上线**(2026-07-10,fast_path_build FROM v0.6.18):镜像 `:v0.6.19-20260710` digest `sha256:a8d35c26…8540e2`,git `e225959`。SAE ChangeOrder `138bd470-9cde-43d5-8202-3358d4a314a8`(image-only 不带 --Envs,Status=2);两 pod(`172.29.0.16`/`172.29.0.10`)`/health` = `v0.6.19 git_sha e225959`。**活体冒烟 PASS**:ECS→pod `/v1/agent` 流式(progress→逐 token 正文→trace `answered_streamed`→[DONE])+ 经 LiteLLM `chick-lxj`/`chick-lxj-web` 公网全链两问均正常作答收口。**ops 同批**:ALB `lsn-2fco4wsu8dia1xl301`(443)/`lsn-pqv1695408j8l8nqc3`(80)`RequestTimeout` 60→180s(CLI 实证生效;回滚=改回 60)。异常看板两条已置「已解决」。

## [v0.6.18-20260707] — 空作答兜底改判「可见缓冲」(治 sentinel 吞流假成功)· ✅ 已上线 2026-07-07
> **背景(异常看板 2026-07-06 新 bug,韩雪艳 22:11 报错自动,v0.6.17 上线后仍复发)**:大表 plan-and-execute run(22:09–22:11,约 40 个 excel-poc 并行查询全 success,SpendLogs 实证)步骤全成功,**综合轮**(tools=[])K2.6 把「想再调工具」当正文流出 `<|tool_calls_section_begin|>…` sentinel 段且**未闭合** → `_StreamToolLeakGuard` 按设计整段吞掉(flush 抑制态丢弃)→ **客户端 0 字**;但 `content_buf` 是**原始**累积(存着 sentinel,非空)→ v0.6.17 的空作答兜底(判 `content_buf.strip()`)不触发 → `answered_streamed` 假成功 → 前端只能兜「模型这一轮没生成任何内容」错误卡。另佐证:content 路径的承诺条件本就是「出现过非空白 content delta」,`content_buf` 全空白时走的是 `empty` 路径 —— v0.6.17 那个判空条件在 content 路径**近乎死分支**(行为 harness case4 实证)。
> **改**(`agentic_web.py`,零契约变更):
> - `_speculative_iteration` 新增 `visible_buf`:累积**实际下发给客户端**的正文(leak_guard `feed()` 放行的 safe 段 + `flush()` 残尾),随 done state 返回;另带 `suppressed_tool_leaks`(守卫吞块计数)供诊断。
> - content 路径空作答兜底改判 `visible_buf.strip()`(缺 key 回退 `content_buf`,行为同旧):sentinel 吞流 / 任何「content 承诺了但用户一个字没看到」的场景,统一走 `_synthesize_answer` 合成兜底(tool-free 强制作答,plan 结果都在 history 里),不再假成功。progress 事件 reason=`empty_visible_content`,带 `raw_content_chars`/`suppressed_tool_leaks`。
> **自验**:`py_compile` 绿;行为 harness 5 case PASS(未闭合 sentinel 全吞→visible 判空+兜底条件触发 / 正常回答不受影响 / 配对块前后正文保留 / 纯空白走 empty 路径佐证 / v0.6.17 vs v0.6.18 条件对比)。
> **上线**(2026-07-07,fast-path build FROM v0.6.17):镜像 `:v0.6.18-20260707` digest `sha256:736878533027b3cfbc0e3fc13e3952d453ad85c746acd5a5e173f31dbb7251e9`,git `c1e97f0`。SAE ChangeOrder `d18d79fd-1112-4c3a-b83a-869faa481059`(image-only 不带 --Envs,Status=2);两 pod(`172.29.0.5`/`172.29.0.12`)`/health` = `version v0.6.18 git_sha c1e97f0`,env 35 全保 + PreStop sleep25 + Replicas 2,capabilities 全保。**活体冒烟 PASS**(ECS→pod `/v1/agent/chat/completions` 流式提问:content 正常逐 token 下发、`answered_streamed` 收口、[DONE] 正常 —— 正常回答路径零回归)。异常看板该条已置「已解决」(生产 `已解决|17`,未解决清零)。
> **回滚**:镜像回 `v0.6.17-20260705`(零数据副作用)。

## [v0.6.17-20260705] — plan 重复 step id 自动修复 + 空作答合成兜底(治「模型这一轮没生成任何内容」)· ✅ 已上线 2026-07-05
> **背景(异常看板 2026-07-05 新 bug,韩雪艳 12:57 报错自动)**:PPT 出题套模板场景,K2.6 提交的 plan 两个 step 都叫 `step_1` → `_validate_plan_steps` 整盘 `plan_validation_failed` → plan 零结果 → 综合轮(plan_dispatch_done)模型只流出空白就 stop → adapter 记 `answered_streamed`(自以为答了),前端零可见内容,兜底成「模型这一轮没生成任何内容」错误卡。adapter 日志逐秒对上(12:55:10 起跑,plan 校验失败,run 结束 12:57:43 = 上报时刻)。
> **改**(`agentic_web.py`,零契约变更):
> - **重复 id 自动修复**:`_execute_plan_streaming` 校验前把后出现的重复 step id 自动改名 `<id>__<序号>`(depends_on 对旧名的引用仍解析到首个,与模型意图一致);只救重复,缺 id/缺 question 等结构问题照旧报错。
> - **空作答合成兜底**:content 路径 finalize 前,若累积 content 全空白(如 speculation flush 的 `\n\n`)且本轮没 finalize 出文件 → 不再按 `answered_streamed` 静默收场,改走既有 `_synthesize_answer` 合成兜底链(tool-free 强制作答;合成也失败才 `answered_empty_fallback` 致歉文案)。前端「模型这一轮没生成任何内容」错误卡从此只剩真·上游异常一种来源。
> **上线**(2026-07-05,fast-path build FROM v0.6.16):镜像 `:v0.6.17-20260705` digest `sha256:3c4ac03b39b92593e99928f67b0a31192fae70e92281b8b6e992f4e82d5a3fba`,git `d2822f8`。SAE ChangeOrder `c4d8ceae-97b2-4595-9655-eb59580eeb43`(image-only 不带 --Envs,Status=2);两 pod(`172.29.0.13`/`172.29.0.21`)`/health` = `version v0.6.17 git_sha d2822f8`,capabilities 全保。异常看板该条已置「已解决」(生产 `已解决|16`)。**回滚**:镜像回 `v0.6.16-20260703`(零数据副作用)。
> **自验**:`py_compile` 绿;去重纯函数自测(3×`step_1` → `step_1/step_1__2/step_1__3`,校验通过;非去重路径重复照报)。

## [v0.6.16-20260703] — agent SSE 写出层全局保活心跳(治长流静默段被 idle 掐流)· ✅ 已上线 2026-07-03
> **背景(异常看板 2026-07-02 全部 4 条未解决 bug)**:文件生成 / 大表分析等 agent 长流**中途断线**,前端报裸 "network error"、任务白跑(许晴×2 / 王燕 / 张超,全部 severity=完全用不了;时间 10:23–15:04,已排除当晚 3 次前端部署的嫌疑)。根因:agent loop 的**静默段**(模型生成工具参数〈xlsx/docx 全文在 tool args 里,数分钟〉/ 大表 excel-poc 查询执行 / 规划期 / narrate 续轮)对下游零字节输出 → 被链路 idle 墙掐流(**前端 BFF 是 Node fetch/undici,默认 `bodyTimeout=300s` 无 body 字节即断**;各层 LB 另有各自阈值)。v0.6.14 只给「文件渲染等待段」加了 20s 心跳,其它静默段裸奔;BFF→浏览器方向有 5s 心跳、adapter→BFF 方向无保活。
> **改**(`adapter.py`,零契约变更、对所有 agent 模式通用):
> - 新常量 `AGENT_SSE_HEARTBEAT_SECS`(env `ADAPTER_AGENT_SSE_HEARTBEAT_SECS`,默认 15s,`0`=关)。
> - `_handle_agent_chat_stream` 写出层加**后台心跳线程**:距上次真实写出 ≥ 阈值就写一行 SSE 注释 `: hb\n\n`(SSE 规范注释,前端解析器只认 `data:` 行天然忽略;BFF 逐字节转发,同时刷新其 undici bodyTimeout)。写出经 `write_lock` 互斥(`_locked_write`),注释只落在完整事件之间、不会插进半个事件;所有收口路径(正常 / HTTPError / Exception + finally)先 `hb_stop.set()` 再锁内写 `[DONE]`/chunked 终止,流结束后心跳不再写;心跳写失败(客户端已断)只停心跳、主循环按既有路径收场。
> - 选 handler 写出层做**唯一汇聚点**:上游 generator 无论在哪个静默点卡住(args 累积 / excel 步 / 规划 / narrate buffer),这里统一兜底,不必每个静默点各补一次(v0.6.14 的教训)。
> **自验**:`py_compile` 绿;行为 harness 3 case PASS(5s 静默/2s 阈值 → 恰好 2 个心跳、A/B 事件间只有心跳块、chunked 编码完整;generator 抛异常 → error 事件+DONE+终止收口、异常前有心跳;`0` 关闭无心跳)。配套前端(llm-playground-pro `lib/chat-client.ts`)同批:断流错误由裸 "network error" 换中文文案(部分内容已保留 + 引导「重新生成」)。
> **上线**(2026-07-03,fast-path build FROM v0.6.15):镜像 `:v0.6.16-20260703` digest `sha256:030cca6013628a4f792e24965b37ec9efaf04c22d14923f63f5122a9660ba5d2`,git `c0eeb6e`;镜像内自检(`_FILE_GEN_AVAILABLE`)过。SAE ChangeOrder `d1146079-32aa-456a-92d6-5f51890d3f04`(image-only 不带 --Envs,Status=2);`describeApplicationConfig` 实证 ImageUrl `:v0.6.16-20260703` + **35 env 全保** + PreStop sleep25 + Replicas 2;两 pod(`172.29.0.16`/`172.29.0.19`)`/health` = `version v0.6.16 git_sha c0eeb6e`。
> **部署后活体 PASS**(ECS→pod 直打,绕 BFF,gen_file_force xlsx 12-sheet 大生成):240s 观察窗收到 **15 个 `: hb` SSE 注释**(≈每 16s 一个),贯穿「工具参数生成」静默段 —— 修复前这段零字节、撞 BFF undici 300s bodyTimeout 必断,现在链路持续有活性字节。事件流本身(iteration_start/首轮 force 等 progress)完整无插坏。
> **回滚**:镜像回 `v0.6.15-20260701`(零数据副作用);或 env 应急 `ADAPTER_AGENT_SSE_HEARTBEAT_SECS=0` 关心跳(改 env 需重新部署生效)。

## [v0.6.15-20260701] — lxj-agent 计费专名加固:BILLING_MODEL 默认值/fallback lxj→lxj-agent · ✅ 已上线 2026-07-02
> **背景**:网页版 agent(文件生成/大表)透传用户 key 走 LiteLLM 计费,原用裸名 `lxj` —— 与外部编程工具**真直连**的 `lxj` 在用量统计里分不开(污染,做用量看板时暴露)。2026-07-01 起后端加 `lxj-agent` 专名区分(LiteLLM `/model/new` 加模型〈cost/credential 复制 lxj〉+ 28 team/40 key 批量授权 + adapter env `ADAPTER_BILLING_MODEL=lxj-agent`)。本次 = **代码层加固**:防 env 万一丢失时回退旧 `lxj` 重新污染。
> **改**(`adapter.py`,纯默认值/注释,零逻辑、零契约变更):
> - `BILLING_MODEL` 默认值 `lxj`→`lxj-agent`(:178);`_build_agent_config` fallback `BILLING_MODEL or "lxj"`→`or "lxj-agent"`(:2451);HTML builder `... or "lxj"`→`or "lxj-agent"`(:2147)。三处 env 缺失兜底都指向正确专名。注释 173/2442 同步。
> - env 模板 `sae-adapter.env.example`(lxj-adapter-deploy 仓)`ADAPTER_BILLING_MODEL=lxj-agent`。
> **自验**:PM 联网核查门(全 lxj 使用点清单 + agent 计费路径确认走 env,无遗漏硬编码)+ reviewer 代码核。`lxj-agent` 模型 live 调用通。
> **上线**:镜像 `:v0.6.15-20260701` digest `sha256:13ecc2f5b9bb2df0342875c36a94c28e21a7cb54921d8b227478b77e7900d08a`,git `78b73d6`。⚠️ build 撞 ECS→国外源网络卡(apt `deb.debian.org` / pypi / playwright chromium 全卡)→ 换 **apt+pip 阿里云 mirror**(`mirrors.cloud.aliyuncs.com`)+ **Chromium 官方 CDN**(npmmirror 无该新版本 404)治好。SAE ChangeOrder `c6fb3289-1c6a-4984-9df4-3e0e7ea56939`(Status=2);`describeApplicationConfig` 实证 ImageUrl `:v0.6.15` + `BILLING_MODEL=lxj-agent` + env 35 保全 + PreStop sleep25;chick-lxj 走 adapter 响应正常。
> 🅿️ build 换源 patch 仅落 ECS build 环境、**未落 Dockerfile** → 下次 adapter build 若网络仍卡需重 patch 或落 Dockerfile(apt/pip mirror);Chromium 保持官方 CDN。

## [v0.6.14-20260630] — HTML 文件生成「治截断」:续写分段 + 32K + 关 thinking + 保活心跳 + tab 修复 · ✅ 已上线 2026-06-30
> **背景/需求**(用户报 + 全栈实测复现):文件生成的 **HTML 看板**由模型自由生成整页 HTML,大看板会撞单次 `max_tokens` 被截断(`finish_reason=length`,末尾图表脚本丢=空 canvas/不收尾 `</html>`)。用户拍板:**解决截断,但绝不牺牲「自由生成 + 模型自由写 JS」**(不转结构化)。并修一个并发暴露的 tab bug:模型自由写的 tab 切换 JS 偶发 token 级语法错(如 `color:#hex` 漏引号)→ 整段 `<script>` 解析失败 → `switchTab` 未定义、点 tab 无反应(`node --check` 实证 + 生产产物复现)。
> **改**(`adapter.py` + `agentic_web.py`,纯后端、**零契约变更**、自由生成路径不动):
> - **① HTML 续写分段**(`_call_upstream_html_builder` + 新 `_stream_one_html_call`/`_stitch_html`):单段照常自由写;撞 `finish_reason=length` 且没写到 `</html>` → 把已写半成品当 assistant 上下文发「接着写」调用,`_stitch_html` 去接缝重叠后拼接,最多续 `ADAPTER_HTML_BUILDER_MAX_CONT`(默认 3)次。打破单次 token 墙而**不改成结构化**(守住用户「保自由生成 + 自由 JS」)。
> - **② builder 关 thinking**(复用 `_build_no_thinking_extra`,K2.6/vLLM0.18 双 key `thinking`+`enable_thinking`):续写轮更勾推理,reasoning 会吃光 `max_tokens` 致 content 空(自验复现「续写返回空」根因);关掉后续写正常,且单段自由生成产出更足(自验:段1 7298→12294 字符)。
> - **③ `max_tokens` 14K→32K**(`ADAPTER_HTML_BUILDER_MAX_TOKENS`):直连实测上游接受 32000、模型对超大文档**自然收尾 ~19K**(`finish_reason=stop`,不撞低位硬截)→ 旧 14K 会把这类 ~19K 自然产物截断;抬 32K 让它们**一次写完**(stop)、少触发续写。续写仍兜极端 >32K。
> - **④ 生成期保活**(`agentic_web.py` `run_agent_stream`):长生成期渲染同步阻塞会让 adapter→前端 SSE 静默被 idle 超时掐流 → **渲染丢 daemon 线程,主 generator 每 `_FILE_GEN_HEARTBEAT_SECS`(20s)吐 `agent_file_gen_progress` 心跳保活 + 反馈**;`_FILE_GEN_MAX_WAIT_SECS`(1500s)backstop 兜渲染器异常卡死。对**所有文件类型通用**,快渲染(首跳前完成)零回归。
> - **⑤ tab 修复**(`HTML_BUILDER_PROMPT` 补 3 条):JS 对象里颜色/字符串值一律加引号(漏引号=整段 script 失效)、`onclick` 调的函数必定义、隐藏 tab 图表懒初始化或显示后 `chart.resize()`。
> **自验**:`py_compile`×2 绿;**直连 live LiteLLM 续写实测**(强制小 `max_tokens` 逼多段:1 段 & 3 段/2 接缝 —— 接缝干净、`<!DOCTYPE`×1 不重启、`</html>` 收尾、`node --check` 全过、`<h2>` 无重复);关 thinking 修好「续写返回空」;`max_tokens=32000` 直连实测(接受、19K 自然停、54.7 tok/s、3.38 字符·token⁻¹);`_stitch_html` 单测 6/6(短尾 `</body>` 去重 + 不误删正文);保活逻辑测试 3/3(慢渲染吐心跳 / 快渲染零回归 / 异常兜底);tab 修复 live 3/3 JS 合法。**reviewer 核查门**:P0 无、P1(`_stitch_html` 短尾漏去重,下界 15→4)已修验、P2×2 已处理(渲染卡死 backstop + 续写输入窗口文档化)。**非契约变更**(`x_adapter_artifact` 信封不变;`agent_file_gen_progress` 是 `x_adapter_agent_progress` 新 stage 值,前端 `Playground.tsx` 已加分支对齐)。
> **上线**(2026-06-30,fast-path build FROM v0.6.13):digest `sha256:40d43baa5b7f54a88127793408ee31b9eb9680678b42a7c2967ad9b18cd75037`,git `82564e9`;SAE ChangeOrder `0f17b8f3-fb42-481b-bb45-54c471a94ae1`(Status=2);`describeApplicationConfig` 实证 ImageUrl `v0.6.14-20260630` + **35 env 全保** + PreStop sleep25 + Replicas 2;两 pod `/health` = `version v0.6.14 git_sha 82564e9`。
> **部署后大看板活体 PASS**(ECS→pod,绕 BFF):8-tab/24-chart 大看板 → 生成 454s(~7.5min)→ **`agent_file_gen_progress` 心跳 21 个**(每 20s,贯穿全程——没保活这 7.5min 静默必被 idle 掐死)→ artifact ready;产物 58335 字节、`</html>` 收尾、24 canvas/24 new Chart、8 tab、`node --check` 通过;**`finish_reason=length`=0**(25K-token 大看板一次写完,旧 14K 必截断它,32K 兜住)。配套前端 `0.17.46`(A3 基座 + 心跳标签)。详 runbook `../lxj-adapter-deploy/runbooks/deploy-2026-06-30-html-continuation.md`。

## [v0.6.13-20260628] — 大表 plan-and-execute 规划前预取真实表结构(治「盲规划」)· ✅ 已上线 2026-06-28
> **Bug**(用户报 + 看图坐实):大表分析「多步规划」里,**模型规划时根本不知道表结构** —— plan prompt `EXCEL_AGENT_PLAN_PROMPT` 结尾的 `{schema}` 占位符**从来没被填充**(`adapter.py` 裸赋值 `cfg.system_prompt = EXCEL_AGENT_PLAN_PROMPT`,全仓无 `.format(schema=...)`)。被迫盲规划 → 模型要么瞎猜分析 step 的维度/列名,要么自己加一个「查看表结构」step 当 step_1 却**不给后续 step 填 `depends_on`** → 拓扑排序把多步全塞进同一并行批 → **「查看表结构」还在跑,分析 step 已经各自盲查跑完**(用户截图现象)。更深一层:plan 的 `depends_on` **只排序、不把上游结果喂下游**(`_worker` 只传 `(question, timeout)`),即便串行也救不了。本质是从旧 iterative 模式(首轮强制 `excel_query` 自带「先查表」)切到 plan 模式时丢了 schema 发现、又没把 `{schema}` 注入补回来的**功能回退**。
> **改**(纯加法,best-effort,可 env 关 / 取不到自动回退,零破坏):
> - **excel-poc**(`v0.3.1`):新增 `GET /datasets/{id}/schema` → 返回 `prompts.render_schema(profile)` 完整表结构文本(表/列/dtype/明细表 grain 提示/取值样例,与 orchestrator 写 SQL 时**同源同格式**)。带 `X-User-Id`(B13 隔离,他人 dataset_id → 404)。
> - **adapter**(`adapter.py`):新增 `_fetch_excel_schema(dataset_id, user_id)`(GET 上述端点,**任何错误[老后端无端点 404 / 网络 / 超时]都返回 `""` 回退,不掀翻分析**);plan 分支**规划前预取 schema** → `EXCEL_AGENT_PLAN_PROMPT.replace("{schema}", schema_text)`;取不到则注入回退说明(模型按 prompt 内 `depends_on` 兜底指引自处理)。新增 env `ADAPTER_EXCEL_SCHEMA_PREFETCH`(默认 `1`,可关回退原行为)+ `ADAPTER_EXCEL_SCHEMA_TIMEOUT`(默认 `15`s)。
> - **prompt**(`agentic_web.py` `EXCEL_AGENT_PLAN_PROMPT`):更新 `depends_on` 指引 —— 「表结构已在【数据集结构】给出,直接用真实列名写 step、**不要再列查表 step**、正常全并行」;仅保留「结构未预取到」时的查表 step + `depends_on` 兜底分支。
> **效果**:planner 看到真实表名/列名 → step 用真列名不盲猜 → 基本不再产出「查看表结构」step → 彻底消掉「分析 step 在结构未知时并行盲查」的失败模式,正中用户「先得到表结构、再制定计划」。
> **自验**:本地 `py_compile` 两仓绿 + 本地脚本 12/12 PASS（`render_schema` 出真实列名 + prompt 注入无残留 `{schema}`〈真实+兜底两路〉/ `_fetch_excel_schema` 空 user・无 backend・不可达 三容错均返 `""` 不抛）+ reviewer 核查门 🟡 无 P0/P1。
> **上线**(2026-06-28,按序 excel-poc v0.3.1 先 → adapter v0.6.13 后):image-only fast-path build,digest `sha256:83dd1bf857f8d601131269e967dec077095ae82d9befa4ff5d07f88ef5696081`,git `51f9ccd`;SAE ChangeOrder `fbfb7e8d-2e3a-494c-99b1-0ebd1f485bc1`(Status=2);`describeApplicationConfig` 实证 ImageUrl `v0.6.13-20260628` + **35 env 全保**(`ADAPTER_EXCEL_SCHEMA_PREFETCH` 不在 env → 代码默认 `1` 即开)+ **PreStop sleep25 保留** + Replicas 2。`/health` 实证 `version=v0.6.13 git_sha=51f9ccd agent_plan_and_execute_excel_enabled=true`,pod Running+Healthy。
> **端到端活体 PASS**(ECS→pod,绕 BFF):合成 40 行 4 列 Excel 建集 → `GET /datasets/{id}/schema` 真返回完整结构文本(门店名称/省份〈可选值 江苏/安徽/浙江〉/营业额〈范围+均值〉/订单数,= 注入 planner 的内容)；无 X-User-Id→400(B13 守住)。🅿️ **post-launch 观察**:真用户大表分析 planner 出 schema-grounded plan、不再有「查看表结构」step(需真 LLM run,留观)。**非契约变更**:`/v1/agent` payload 不变;新端点是 adapter↔excel-poc 内部接口,前端 `PROTOCOL.md` 不受影响。详 runbook `../lxj-adapter-deploy/runbooks/deploy-2026-06-28-schema-prefetch.md`。

## [v0.6.12-20260626] — agent 文件生成计入鸡分额度(Bug1 Step1)· ✅ 已上线 2026-06-26
> **背景**:agent(文件生成)请求原 EAS 直连、绕过 LiteLLM → 算力不计用户鸡分。让其计费(用户拍「① 开始消耗积分 ② 进行中不中断」)。
> **改**(`adapter.py`,纯加法、无新依赖,fast-path build):
> - 新增 env `ADAPTER_BILLING_UPSTREAM_BASE_URL`(= LiteLLM main `https://llm.lxjchina.com.cn/v1`)+ `ADAPTER_BILLING_MODEL`(=`lxj`)。
> - `_build_agent_config(model, user_llm_key)`:BFF 经头 `X-User-LLM-Key` 传登录用户 LiteLLM key 时,基座调用改走 **LiteLLM main**(裸名 `lxj`,防 `chick-lxj` 回环)+ 该用户 key 鉴权(硬用 `Authorization`)→ spend 计在用户 key;无头 → 回退 EAS 现状、不计、零回归。
> - `_call_upstream_html_builder` + `_make_file_renderer(cfg)`:viz/html 自由生成调用沿用同一 cfg 上游(同计费身份)。
> - 🔴 user key 只本次取用、不落日志/盘。reviewer 核查门过(写者≠审者);py_compile 绿;build 内 `_FILE_GEN_AVAILABLE` 自检过。
> **上线**:digest `sha256:8fe56877895a83a1cd982e78627eee8f3e5b4dc9a30dcde2fcd8ce0d1fc574c7`,git `44dea3d`。SAE DeployApplication ChangeOrder `0ea32a51-e69b-4c6b-977e-c6a366911364`(Status=2 · batch1),线上 config 实证 ImageUrl `v0.6.12-20260626` + **35 env**(`ADAPTER_BILLING_*` 已加 + 33 原 env/secret 全保)+ **PreStop sleep25 保留** + Replicas 2。配套前端 `0.17.28`(发 `X-User-LLM-Key` + headroom 准入门)。🔴 **前置根因**:LiteLLM `chick-lxj`/`chick-lxj-web`/`lxj` 三模型原 cost=None(chat/agent 都不计费)→ 已配 kimi-k2.6 费率(`/model/update`,LIVE)。🅿️ Step1.1:loop 内 budget-exceeded 优雅收尾兜底(当前靠保守 RESERVE 1.0 元规避)。大表 excel-poc 计费=Step2。

## [v0.6.11-20260624] — B14 文件生成扩展:md / txt(文件能力优化标准包)· ✅ 已上线 2026-06-24
> **背景**:用户「继续做优化」,文件能力优化标准包(decisions 06-24)。原只生成 pptx/xlsx/docx/csv/html 5 类,补常见 **markdown(.md)** + **纯文本(.txt)**。
> **改**(纯加法,A 铁律):
> - 新增 `md_generator.py` / `txt_generator.py`(仿 `csv_generator.py`,**纯 stdlib 零新依赖**)。模型直接在 `body` 写 markdown / 纯文本(= 内容非渲染代码),退化兼容 docx 式 `sections`;`_clean_body` 保留 markdown 显著空白(代码块/表格/缩进)vs `clean_text` 折叠。
> - `agentic_web.py`:`GENERATE_MD_TOOL` / `GENERATE_TXT_TOOL` + `ALL_FILE_GEN_TOOLS` + `FILE_GEN_TOOL_META` + 两 prompt「选哪种文件」。
> - `adapter.py`:import md/txt generator(try/except)+ `_make_file_renderer` spec + `_ARTIFACT_EXT_MIME`(md/txt)+ `/health.file_gen_types`(7 类型)。**md/txt 单片**(不进 `_FILE_GEN_PART_KEYS`)。
> - `Dockerfile`:COPY md/txt generator。
> - 自验:py_compile + md/txt smoke(保留表格/代码块/换行)+ grep 7 类注册点一致 + 前端 tsc EXIT=0。reviewer 核查门过(subagent `a57540ba`,0 P0/P1,3 P2 非阻塞)。
> **✅ 已上线(2026-06-24)**:fast_path build(FROM v0.6.10,镜像内自检 `assert _FILE_GEN_AVAILABLE` 过 = md/txt 导入成功)→ image-only 部署 digest `sha256:09865de17d24bd3a7971fb018ac9084971fd8606f758615ca8293a768fdaaf84`,ChangeOrder `e2b4501d-c756-48a9-8b13-92908ef261e1`,**33 env + PreStop sleep25 保留**,Replicas 2。git `9b1c5de`。🔴 **待测试域**:md/txt 生成→下载带图。

## [v0.6.10-20260624] — B12 组合模式:修「分析表+做看板」看板编造数据(测试域 live FAIL)· ✅ 已上线 2026-06-24
> **背景**:B12(v0.6.9)给 html builder 注入了对话上下文,但**测试域 authed live 验收 FAIL** —— 「分析上传 Excel→做柱状图看板」产出的看板**数字是编的**(上传表真实值 `73219/48571/91864` 一个没进图,chart 填假数据 + 假月份标签)。
> **根因**:file_gen 与 excel 模式**架构互斥**。force chip 路径前端虽同时发 `gen_file_force`+`excel_dataset_id`,但 adapter `file_gen_mode` **忽略 excel_dataset_id**、首轮强制 `generate_html`,**没先跑 `excel_query`** → builder 无真数据 → 编造。v0.6.9 的 context 注入只在「先分析→再做看板」两轮流才有数据,单请求拿不到。
> **修 = 组合模式(`excel_file_gen_mode`)**:
> - handler:`gen_file` + `excel_dataset_id` 同时在 → 组合模式(**优先于纯 file_gen_mode**)。
> - `_build_agent_registry`:`enable_file_gen` + `excel_dataset_id` → 挂 **`excel_query`(真 impl)+ 全部 `generate_*`(schema-only,拦截渲染)** 两套。
> - **首轮 `force_first_tool_name="excel_query"`** 查真数据(**不** `force_required_tool`,否则首轮就生成、没数据),后续轮模型自决调 `generate_html` 用真数值。
> - `EXCEL_FILE_GEN_PROMPT`(新):铁律「先 `excel_query` 查真数据 → 再 `generate_*` 用真数值,**严禁编造**」。
> - **三重保真**:首轮强制查表 + 模型把真值写进 generate_html brief + B12 context 注入(augmented 含 excel_query 结果)。**前端 force 已发两字段,无需改**。
> - 自测:组合 registry 挂 `excel_query`+`generate_*`(html/xlsx/pptx/docx/csv)+ 纯 file_gen(无 dataset)零回归 + py_compile 绿。
> **✅ 已上线(2026-06-24)**:image-only(FROM v0.6.9,ChangeOrder `af613156-860e-4c51-8d44-7bf587f42b1a`,33env+PreStop 保留,自检 SELFCHECK_OK file_gen)。git `a3b61aa`。🔴 **待测试域复验**:「分析+看板」单请求 → 看板含真实数值带图。

## [v0.6.9-20260624] — B11 大文件分多步生成 + B12 Excel→看板 + B13 excel-poc 越权透传 · ✅ 已上线 2026-06-24
> 五期 B11/B12/B13 三项后端改动合并发布。

> ### B11 大文件分多步生成(治大纲 JSON 撞顶截断 / 超大文件退化)
> - **Phase0 扩 token**:`AGENT_DEFAULT_MAX_TOKENS` 8000→12000(`adapter.py`,治大纲 JSON 撞顶截断)+ `_HTML_BUILDER_MAX_TOKENS` 10000→14000(builder 已流式 v0.6.6,`_HTML_BUILDER_TIMEOUT` 是 **per-read 逐 chunk 超时非总时长上限** → 14000@~56tok/s≈250s 安全)。
> - **Phase1 = 数据级分多步累积**(**非产物级合并**;截断根因是模型输出 JSON 太长,合并已渲染 .pptx 极 brittle):
>   - `GENERATE_PPTX/XLSX/DOCX_TOOL` schema 加 `part_index` / `total_parts`(`agentic_web.py`)+ `FILE_GEN_PROMPT` / `FILE_GEN_FORCE_PROMPT` 加分多步指引。
>   - `_make_file_renderer` 闭包累积分片:**非末片返 `{partial:True}` 不渲染**,末片 `_merge_part_canonicals` 合并(slides / sections 拼接、sheets 按名合并 rows,总上限 **200 / 40 / 400**)。
>   - 拦截分支续片**复用同 artifact_id**;partial **不置 `file_gen_dispatch_done`**;模型提前停的**早停兜底**(content 路径用暂存分片 finalize,守卫避免 intent-leak 二次生成)。
>   - 🔴 **续片架构强制**(测试域核查门 P1,2026-06-24):`_force_this_turn=iteration==1` → force 模式续片轮(iter≥2)默认降回 auto、靠 prompt 兜,K2.6 可能续片时 narrate 不出下一片。**修**:partial 分支置 `force_tool_choice_next=True` → 下轮进 required 分支(force 模式 `force_required_tool` / auto 模式 `force_required_on_intent_leak` 任一满足)→ **架构强制**必调工具续片(复用 v0.2.30 续轮基建,无 intent-leak content 分隔符,只发 progress)。
>   - env 开关:`ADAPTER_FILE_GEN_MULTIPART`(默认**开**)/ `ADAPTER_FILE_GEN_MAX_PARTS`(默认 **12**)。**HTML 看板不分片**(自由生成)由 Phase0 14K 兜。
> - **自测**:merge 4 项 + renderer 6 项(part1/2 partial、part3 合并出 7 页真 pptx 无截断、单次零回归、finalize 兜底);续片强制 = 控制流逻辑核 + 测试域 live 组合验。

> ### B12 Excel→看板(给 html builder 注入对话上下文,看板用真实数据)
> - `_call_upstream_html_builder` 加 `context_messages` 参 + 新 `_digest_context_for_builder`(蒸馏对话历史:抽 user/assistant/tool 文本、跳 system、跳纯 tool_call 占位、末尾 cap **6000 字**)塞进 builder user message + 明确「**做图表必须用其中真实数值,不得编造**」。
> - `_make_file_renderer._render` 签名加第 4 参 `context_messages`,`agentic_web.py` 拦截点传 `augmented`,`AgentConfig.file_renderer` 类型放宽。
> - **无上下文时零回归**(退化原 title+brief)。自测 digest 4 项过。
> - ⚠️ **file_gen 与 excel 模式互斥** → 「分析+看板」走两轮流。

> ### B13 adapter 透传(B13 提案漏的缺口:adapter 也调 excel-poc `/ask`)
> - `_call_excel_backend` / `_make_excel_query_impl` / `_make_excel_run_step` 透 `user_id` → 发 `X-User-Id` header。
> - handler 从 `payload.excel_user_id` 取(BFF 注入,**从透传上游模型 API 的 extra 剔除**,不泄私有字段)。
> - **空 uid 短路返清晰错误**(reviewer P1-1)。

> ### 部署
> - **✅ 已上线(2026-06-24)**:image-only 部署(digest `sha256:205890f1fb1613220babb92857a349353b3b294199d7bfb9df00b5ce54011906`,ChangeOrder `838a9b0f-b800-4c6d-82c0-e93227cda725`;**33 env + PreStop sleep25 保留**,镜像内自检 `SELFCHECK_OK file_gen`,RUNNING 2 实例)。**部署顺序锁满足**:bus-check 实证前端 0.17.11 + adapter v0.6.9 **先 live**(都在发 `X-User-Id`)→ excel-poc v0.3.0 最后上,**无 400 中断窗口**。漂移基线刷 v0.6.8→v0.6.9。
> - ⚠️ **无 PROTOCOL 信封变更,前端无感**(`excel_user_id` 由 BFF 注入)。
> - runbook `lxj-adapter-deploy/runbooks/deploy-2026-06-24-b13-excel-isolation.md`。

## [v0.6.8-20260624] — file_gen auto 路径 narrate 续轮兜底:治 ~20-30%「我来生成…」不出文件 · ✅ 已上线(digest b919c587,ChangeOrder d0c43759,PreStop+33env 保留)
> **背景**:B9(v0.6.6)「生成文件」force chip(`tool_choice=required`)已治**显式**路径 narrate(force 16 次 0 narrate);但 **auto** 路径(`gen_file:true` 不含 `gen_file_force` —— 前端 `detectFileGenIntent` 预路由命中、用户没点 chip)仍 `tool_choice=auto`,给模型「不生成只文字」退路 → 偶发 ~20-30% 只回「我来为您生成一个 Excel…」**不 emit tool_call**,用户干等拿不到文件。NOW.md 🟡 viz follow-up 登记的独立项。
> **根因(双重,比文档记的更深)**:① **检测漏** —— 既有全局 intent-leak 检测器(`_INTENT_LEAD_PHRASES`/`_INTENT_NARRATE_RE`/`_DANGLING_INTENT_RE`)是给 web/excel「查询/分析」场景调校的:引导词无「我来」、动作词只有 查询/分析/调用/获取…,**完全漏掉 file_gen 场景的「我来…生成…文件」**;② **续轮强制不了** —— 就算命中,file_gen auto 不设 `force_required_tool`/`force_first_tool_name`,intent-leak 续轮的 `force_tool_choice_next` 在主循环两个强制分支都进不去 → 续轮仍 `auto`,模型可能继续 narrate。
> **修(architecture 兜底为主、prompt 为辅;只在 file_gen auto 叠加 → web/excel/pptx 零回归)**:
> - **`AgentConfig.force_required_on_intent_leak`**(新字段):file_gen auto 专用。**首轮仍 auto**(`force_tool_choice_next` 首轮为 False → 不进 required 分支,模型自决要不要出文件、只聊天/分析不硬出);**仅当本轮 narrate-then-stop 命中**,续轮才升 `tool_choice=required` 逼出文件。兼作「当前 file_gen auto 模式」标记。`adapter.py` file_gen auto 分支置 True(B9 force 分支不设、仍 `force_required_tool=True`)。
> - **`_looks_like_file_gen_narrate()` + `_FILE_GEN_NARRATE_RE`(re.IGNORECASE)+ `_FILE_GEN_OFFER_RE`**(新检测):引导词(我来/这就/我马上/让我/我帮你…)+ 动作(生成/制作/做/导出/创建/整理成/写…,**故意不含「分析」**)+ 文件类名词(文件/表格/Excel/PPT/Word/HTML/看板/图表/csv…)。只看**开头 40 字**(narrate-then-stop 开门见山宣告,天然避开句中「…如果需要我可以生成」提议)+ **征询排除**(「要不要我帮你做个表?」是完整问句、不强制)。
> - **叠加点**:`run_agent_stream` content 路径 `looks_intent`(流式主路径,命中→续轮 required **真出文件**)+ `_finalize_answer` `needs_synthesis`(非流式终结兜底,命中→合成文字,出不了文件但比 narrate 戛然而止强)。两处均以 `cfg.force_required_on_intent_leak and …` 短路 → 其他模式根本不调用新检测。
> - **续轮强制分支**:`if … and (cfg.force_required_tool or (cfg.force_required_on_intent_leak and force_tool_choice_next))` —— file_gen auto 续轮复用 B9 已验证的 `required + 全 generate_*` 出文件路径(**等价回归**,无新出文件风险)。
> - **prompt 强化**:`FILE_GEN_PROMPT` + `FILE_GEN_FORCE_PROMPT`【严禁】段对称补「禁止只宣告我来生成…却不 emit tool_call」。
> - **自测**:py_compile 绿 + **检测函数真代码单测 21/21**(11 POS narrate 含大写 Excel/PPT、口语「要不我来做」全命中;10 NEG 分析/解读/建议/提议/征询全放过)+ **reviewer 核查门过**(逻辑「首轮 auto/续轮 required」、B9 零影响、web·excel·pptx 零回归、A 铁律 全 PASS;P0 契约登记 / P2 prompt 同步+注释 已补;误伤实测确认 borderline「我来做个图表方案」命中=可接受〈auto 语境前端已判文件意图,强制出图符合原意〉)。
> - 🔴 **gotcha(reviewer P1-1)**:`AgentConfig.max_intent_leak_retries` dataclass 默认 0(generic 模块保守语义)、生产经 `ADAPTER_AGENT_MAX_INTENT_LEAK_RETRIES`(env 默认 1)注入才生效;**独立单测直接 `AgentConfig()` 须显式设 =1**,否则续轮静默不触发(false negative)。
> - **✅ 已上线(2026-06-24)**:fast_path 增量 build(FROM `v0.6.7-20260623`,镜像内自检 `SELFCHECK_OK file_gen exts` PASS,digest `b919c587…`,git `62f609b`)→ SAE image-only 部署(ChangeOrder `d0c43759` Status→2 ~70s;`describeApplicationConfig` 核 ImageUrl=v0.6.8 + PreStop sleep25 保留 + 33 env 保留 + Replicas=2;2 pod healthy `version=v0.6.8 git_sha=62f609b`)。**ECS→pod auto 自验**(绕 B6 直打 pod,`gen_file:true` **不带 force**):viz html **3/4 出文件 ready**(60/86/113s),第 4 次 builder 慢被客户端 `--max-time` 切断(非 narrate,narrate痕迹=0)→ **auto 路径不回归坐实**;这几次模型均首轮直接出文件(未自然触发 narrate,~20-30% 没碰上),续轮兜底逻辑由单测 21/21 + reviewer 保证,**生产 narrate 率统计 → 测试域 authed E2E**。漂移基线刷 v0.6.7→v0.6.8。⚠️ **无请求字段 / SSE 信封变更,前端无感**(`contracts/PROTOCOL.md` §4 已登记)。runbook `lxj-adapter-deploy/runbooks/deploy-2026-06-24-adapter-v0.6.8-narrate.md`。

## [v0.6.7-20260623] — viz HTML builder 改流式:治 504 ~37%(撞 183s 静默墙)· ✅ 已上线(digest 225ba9d1,ChangeOrder b8de07a4,PreStop 保留)
> **上线 + ECS→pod 真 builder 自验 PASS**(2026-06-23):image-only 部署(33env+PreStop sleep25 保留,Replicas 2)。**慢 viz 不再 504**:两个刻意详尽的 viz `gen_file` 请求 —— 199s「全国多分公司经营看板.html」46660B + 184s「电商运营数据大屏.html」33179B —— **都 >183s 墙且都出 ready 文件**(修复前这正是 504 的耗时区:测试域见 504 都 ≥184s)。漂移基线刷 v0.6.6→v0.6.7。配套前端 0.17.7 同期。🟡 治"慢"不治"大":超大文件截断/退化 = 分多步生成另立项(用户已记下、当下未决)。
> **测试域生产实证 escalate**(B9 收口附带,orthogonal 非 force):`generate_html` viz 生产 **504 ~37%**,
> 顺序+并发都现(推翻"并发争用"假设)。**根因**:`_call_upstream_html_builder` 用 `stream:False` 非流式
> —— 整个 ~178s 生成期 adapter→EAS **一个字节都不发**(静默),被上游 EAS 网关 **~183s idle 超时**掐成
> 504(测试域:成功 viz ≤182s、504 的 ≥184s;adapter 自己 240s timeout 在其上游够不着)。代码注释
> v0.6.4 早预见此风险(「240s 静默不被内层 LB idle 掐」)。
> - **修**:`_call_upstream_html_builder` 改 **`stream:True`** + 逐行读 SSE 累积 `delta.content`
>   (天然跳过 reasoning_content,与原读 message.content 同字段)。流式下边生成边吐 token、连接一直有
>   字节 → idle 计时永远到不了 183s → 撞墙消除。生成总时长不变,只是不再静默。`_strip_md_fence`/
>   `_render` 兜底/`MAX_TOKENS`/`TIMEOUT` 全不动。
> - 🔴 **先验 PASS(ECS→pod passthrough,不需部署新码)**:`stream:true` + max_tokens 12000 逼生成
>   **216s** → `[DONE]` 收尾、12000 chunks、**无 504**(非流式同时长必死)→ **流式扛过 183s 墙坐实**。
>   (旁证:12000 token 处模型退化乱码 = 过度施压,真 builder 10000 token 不及;**截断/退化是 token
>   上限问题、属"分多步生成"范畴,流式治"慢"不治"大"**。)
> - 自测:py_compile 绿。**治"大"(超大文件分多步)= 另立项**(用户已记下、当下未决)。
> - **待**:部署(授权,带 --PreStop)→ ECS→pod 自验真 builder 慢 viz 出文件不再 504。

## [v0.6.6-20260623] — 「生成文件」force 单开关:tool_choice=required 治 narrate · ✅ 已上线(digest 82d3f934,ChangeOrder d89ab452,PreStop 保留)
> **上线 + 全栈 ECS→pod force 自验 PASS**(2026-06-23):`/health` `version=v0.6.6 git_sha=dce2577 file_gen_enabled=true object_storage.configured=true`;**force 自验 4 类型全出文件**(`gen_file_force:true` 直打 pod):xlsx 各门店销售情况.xlsx 5306B / pptx Q4营收复盘.pptx 52766B / docx 项目实施方案.docx 38075B / **html 各产品线销售额可视化看板.html 9401B**(auto 模式 narrate 重灾区,force 下稳定出文件)—— **命门「force 治 narrate + 类型自判」机制证实**。image-only 部署(33 env + PreStop sleep25 保留,Replicas 2);漂移基线刷 v0.6.5→v0.6.6。配套前端 0.17.6 同期。剩 = 测试域 N≥10 统计带图 + 前端 BFF authed E2E。
> **B9 工单**(`pm/五期-需求-生成文件单开关-B9.md`):旧「生成 PPT」chip 泛化为「生成文件」force 单开关。
> 命门 = 治 narrate —— file_gen auto 模式(`tool_choice=auto`)给模型「不生成只文字作答」的退路,
> 偶发 ~20-30% 只回「我来为您生成…」不出文件;force 用 `tool_choice="required"` 杜绝该退路,模型
> 只能从 generate_* 里选一个调 → 出文件率 ~100%。
> - **新请求字段 `gen_file_force:true`**(隶属 `gen_file`):chip 开 → adapter 在 file_gen 模式内把
>   `tool_choice` 升为 `required`(挂全部 generate_*,模型**只判类型、强制必调其一**)。
> - **`AgentConfig.force_required_tool`**(新字段):True 时主循环 `_force_this_turn` 处注入
>   `tool_choice="required"`(与 `force_first_tool_name` 的 named-tool 分支互斥,required 分支优先)。
> - **`FILE_GEN_FORCE_PROMPT`**(新 prompt):去掉 auto prompt 的「先判断要不要生成」整段,只留「选哪种
>   类型 + 填全参数」—— 用户已点 chip 明确要文件,不该再留犹豫空间。
> - **adapter.py**:`gen_file_force_req` 读(顶层 + extra_body)→ `file_gen_mode` 分支按 force/auto 分流
>   (force=FORCE_PROMPT+force_required_tool;auto 不变);`gen_file_force` 同 `gen_pptx`/`gen_file` 从上游
>   payload 双层剔除(私有控制字段不透传)。旧 `gen_pptx` 路径保留兼容(前端已停发)。
> - 🔴 **先验**:`tool_choice=required` 在 K2.6/vLLM0.18 **实测支持**(生产网关 `lxj` 裸名,required+
>   thinking-off → tool_calls:1、finish_reason=tool_calls、模型选对 generate_xlsx)。老 SGLang 0.5.9
>   「指定工具名比 required 稳」顾虑(v0.2.25 runbook)对新引擎过时。
> - 🔴 **铁律**:force 轮必须关 thinking —— 实测开 thinking 时 reasoning 2500 token 还没 emit 就
>   `finish_reason=length` 出不来 tool_call;adapter 首轮 `intermediate_thinking_enabled` 默认 False
>   已满足。
> - 自测:py_compile 绿 + reviewer 核查门两端过(无 P0;P1×2 文档已修)+ 前端 tsc 绿。
> - **待**:部署(授权,带 `--PreStop`,runbook)→ `/health` 不变(force 是 per-request 非 capability)
>   → 测试域 force 治 narrate N≥10 出文件率带图实测(命门证据)。

## [v0.6.5-20260623] — viz HTML 截断修复:篇幅纪律 prompt + max_tokens 10000 · ✅ 已上线(digest ccf76a06,ChangeOrder ac7105b1,PreStop 保留)
> **v0.6.4 上线后 viz live 自验**(ECS→adapter 直打,真调 generate_html 一次:`ok=true` 142s
> builder 完成、artifact `ready` 24KB、无泄漏 —— **timeout + 泄漏修复证实有效**)发现**新坏法**:产物
> 被 `max_tokens=8000` **截断** —— 模型把预算全花在华丽 CSS,文档在「产品线占比」标题处戛然而止,
> **末尾 `<script>new Chart()</script>` 初始化脚本整段被切**(只剩 1 个空 `<canvas>`,无 `new Chart()`、
> 不以 `</html>` 收尾)→ 下载件是空 canvas、图表不渲染。v0.6.4「保 8000 token 求丰富度」前提被证伪
> (8000 不够写完一个完整看板)。
> - **`HTML_BUILDER_PROMPT` 加「篇幅纪律」**:逼模型 CSS 克制、把 token 预算优先留给「结构 + 每个图表的
>   `new Chart()` 初始化脚本**完整写完**」,并显式声明图表初始化是「能不能显示的关键、必须完整」。
> - **`_HTML_BUILDER_MAX_TOKENS` 8000→10000**(默认):给完整图表脚本留头;10000@~56tok/s≈178s 仍 <240
>   超时,timeout 不动。env 旋钮名不变。
> - 本地:py_compile 绿 + 知识点(MAX_TOKENS=10000 / prompt 含篇幅纪律)+ leak guard 回归无破。
> - ✅ **全栈 ECS→adapter 自验出图 PASS**:批量 4 连发全完美 —— 产物以 `</html>` 收尾、2 canvas、
>   2 个 `new Chart()`(`type:'line'` 季度趋势 + `type:'pie'` 产品线占比,真数据 120/150/180/210,
>   交互 tooltip/hover,主色 #008042)。**篇幅纪律生效**:产物精炼到 ~10-13KB(v0.6.4 截断版是 24-35KB)、
>   builder ~55-69s 完成(远低于 240 超时,也低于怕的 3-4min)。`ok=true` 无泄漏。**timeout/截断/泄漏三坑全闭环**。
>   🔴 剩(非 adapter):① 登录态 BFF E2E 带图(测试域,规则⑧;BFF 只代理、未改,风险低)② model 偶发
>   「narrate 不调 generate_html」(~20-30% 样本,intent-leak 守卫漏认「我来」)= 独立 follow-up,见 status/全栈。

## [v0.6.4-20260623] — viz builder 超时修复 + K2.6 工具 token 流式泄漏清洗 · ✅ 已上线(digest 122ef5df,ChangeOrder a6be3c7e ~55s,PreStop 保留)
> **全栈 viz live 自验**(绕 B6 BFF 鉴权,从 VPC 内 ECS RunCommand 直打 adapter pod 复刻 gen_file
> 请求)发现 v0.6.3 的「可视化看板」**生产稳定失败**:`generate_html` 的 `_call_upstream_html_builder`
> 非流式串行生成一个完整含 chart.js 的仪表盘(~8000 token @K2.6 ~50 tok/s)耗时 ≈ 150s,**恰好顶满
> 150s 超时墙** → `generate_html ok=false elapsed_ms=150104` → 用户得 error 卡 + 道歉转 Excel + 残留
> K2.6 工具 sentinel(`<|tool_calls_section_begin|>…`)泄漏进正文(超时后模型重试、agent loop 到 max iter)。
> - **builder 超时 150→240**(`adapter.py` `_HTML_BUILDER_TIMEOUT` 默认):PM 拍「保丰富度」(decisions
>   2026-06-23)→ 抬超时给 ~33 tok/s 留余量,`MAX_TOKENS` 8000 不动。产物落 OSS 不流式给用户,唯一旋钮
>   是 等待时长⟷丰富度,选丰富度。⚠️ builder 运行期 adapter→下游 SSE 静默 240s,真机验收须确认不被内层
>   LB idle 掐(150s 静默此前 ECS 实测可活)。核实:无 <240 的外层 agent 总超时会先掐(`AGENT_TIMEOUT=120`
>   只管 agent LLM 流式调用、首次实测 builder 跑到 150104ms>120 即证;`AGENT_PLAN_*` 是 plan 模式专用)。
> - **K2.6 工具 token 泄漏清洗**(`agentic_web.py`):① `_strip_tool_call_leaks` + `_TOOL_CALL_LEAK_PATTERNS`
>   扩 K2.6 管道式 sentinel(旧 guard 只认 Qwen `<tool_call>` 子串故 `leaks_stripped=0`)——覆盖缓冲路径
>   (synthesis/forced/finalize)。② 新增 `_StreamToolLeakGuard`(块抑制 `<|tool_calls_section_begin|>…
>   <|tool_calls_section_end|>`、split-across-chunks 安全、fail-open、块前后正文保留)+ `_chunk_with_content`,
>   接进 stream loop content 透传分支——因 `answered_streamed` 是裸逐 token 流出,后处理拦不住已发的流。
>   ③ reviewer P1-1:intent-leak 续轮入 history 前 strip content_buf(防裸 sentinel 喂回 EAS 致 400)。
> - **自测**:py_compile 绿 + 15 项行为测试全过(buffered strip / 流式块抑制 / 逐字符==整喂 split-safe /
>   正常散文含 `<`·`<|`·`<html>` 零误伤 / 块前后正文保留 / 未闭合抑制 / resume after end)。reviewer 核查门
>   过(无 P0;P1-1 已修;P1-2=240s 静默真机验收 gated;P2 边界不影响生产)。**契约 `x_adapter_artifact` 未变**。

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
