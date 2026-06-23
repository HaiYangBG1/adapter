# Changelog — adapter

> 倒序(最新在上)。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
> 每个版本的**详细上线记录**(SAE ChangeOrder / 镜像 digest / 验证证据)见
> `../lxj-adapter-deploy/runbooks/deploy-YYYY-MM-DD-adapter-vX.Y.Z-*.md`。
> 线上实际版本以 `/health` 返回的 `version` + `git_sha` 为准。

---

## [v0.6.8-20260624] — file_gen auto 路径 narrate 续轮兜底:治 ~20-30%「我来生成…」不出文件 · 🟡 待部署
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
> - **待**:部署(授权,image-only env 不动 + `--PreStop`)→ ECS→pod auto 自验复刻 narrate(直打 pod `gen_file:true` 不带 force)→ 测试域 authed E2E auto 路径 N 次统计 narrate 率。⚠️ **无请求字段 / SSE 信封变更,前端无感**(`contracts/PROTOCOL.md` §4 已登记)。

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
