"""Agentic web module — tool-calling loop for OpenAI-compatible upstream.

Phase 1 scope: tool schemas + dispatch registry + a single-round 2-hop run_agent
(send → tool_calls → execute → send → answer). No iteration yet — Phase 2 will
turn this into a real loop with budget control.

Design constraints baked in from Phase 0 validation against Qwen3-VL-235B-A22B:
- tool_choice="auto" works only with a strong system prompt that explicitly
  forbids guessing real-time info; without it the model hallucinates answers
  instead of calling tools.
- Parallel tool_calls work — Qwen3-VL emits multiple tool_calls in one turn
  when the user asks multi-faceted questions.
- finish_reason="tool_calls" is the loop signal.

This module has zero direct dependency on adapter.py: it receives a Callable
for each tool through ToolRegistry.register(), so adapter.py wires the
existing _search_web / _fetch_web_page implementations in at import time.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# Hotfix P0-2 — citation guard.
# Regex matches absolute http(s) URLs in free text. We deliberately accept
# trailing punctuation and clean it later in _normalize_url so URLs at end of
# sentences are caught (e.g. "see https://x.com.").
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'，。；、]+", re.IGNORECASE)
_URL_TRAILING_PUNCT = ".,;!?)]}>'\"，。、；！？）】"


def _normalize_url(url: str) -> str:
    """Strip trailing punctuation, lowercase scheme+host, drop fragment.

    Lets us compare URLs the model wrote against URLs the tools actually
    touched, even when surface forms differ ("https://x.com/." vs
    "https://x.com/")."""
    cleaned = url.strip().rstrip(_URL_TRAILING_PUNCT)
    # Drop fragment
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0]
    # Strip trailing slash on root paths for stable comparison
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        netloc = parsed.netloc.lower()
        path = parsed.path or "/"
        if not path.endswith("/") and "." not in path.split("/")[-1]:
            # leave as-is
            pass
        return f"{parsed.scheme.lower()}://{netloc}{path}{('?' + parsed.query) if parsed.query else ''}"
    except Exception:  # noqa: BLE001
        return cleaned


def _extract_urls(text: str) -> list[str]:
    """Return all http(s) URLs in ``text`` as normalized strings (de-duplicated, order-preserving)."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _URL_IN_TEXT_RE.finditer(text):
        norm = _normalize_url(m.group(0))
        if norm and norm not in seen:
            seen.add(norm)
            found.append(norm)
    return found


def _collect_verified_urls_from_result(result: Any, target: set[str]) -> None:
    """Walk a tool result dict and add any URL-bearing fields to ``target``."""
    if isinstance(result, dict):
        for key in ("url", "image_url"):
            v = result.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                target.add(_normalize_url(v))
        # web_search returns {"results": [{"url": ...}, ...]}
        for item in result.get("results", []) or []:
            if isinstance(item, dict):
                u = item.get("url")
                if isinstance(u, str) and u.startswith(("http://", "https://")):
                    target.add(_normalize_url(u))
    elif isinstance(result, list):
        for item in result:
            _collect_verified_urls_from_result(item, target)


def _shape_web_search_sources(result: Any) -> list[dict[str, Any]] | None:
    """Pull search results out of a web_search tool output and shape them for
    the SSE ``x_adapter_sources`` event.

    Returns None when the tool errored / returned nothing / shape is unexpected.
    Each item carries title / url / domain / favicon / snippet — enough for
    the frontend to render a coral cite chip + hover preview without parsing
    the markdown ``Sources:`` footer.
    """
    if not isinstance(result, dict):
        return None
    raw_items = result.get("results")
    if not isinstance(raw_items, list) or not raw_items:
        return None
    out: list[dict[str, Any]] = []
    for r in raw_items:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()
        if not url:
            continue
        title = str(r.get("title") or "").strip()
        snippet = str(r.get("snippet") or r.get("text") or "").strip()
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:  # noqa: BLE001
            host = ""
        domain = host[4:] if host.startswith("www.") else host
        favicon = f"https://{domain}/favicon.ico" if domain else ""
        if len(snippet) > 240:
            snippet = snippet[:240].rstrip() + "…"
        out.append({
            "title": title or domain or url,
            "url": url,
            "domain": domain,
            "favicon": favicon,
            "snippet": snippet,
        })
    return out or None


# Patterns for stripping tool-call markup that leaks into content text when
# the upstream parser is bypassed (e.g. when tools=[] is sent but the model
# still tries to invoke tools — see Phase 2 testing).
_TOOL_CALL_LEAK_PATTERNS = (
    re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL),
    re.compile(r"<function_call>.*?</function_call>", re.DOTALL),
    # Standalone <tool_call> / </tool_call> with no closing pair (truncated)
    re.compile(r"<tool_call>.*", re.DOTALL),
    re.compile(r"</?tool_call>"),
    re.compile(r"</?function_call>"),
)


def _strip_tool_call_leaks(text: str) -> tuple[str, int]:
    """Remove leaked <tool_call> markup from a content string.

    Returns (cleaned_text, num_blocks_removed). Cleanup is best-effort: matches
    are removed greedily. Used only on the *final* iteration where we expect
    pure natural-language output.
    """
    if not text or "<tool_call>" not in text and "<function_call>" not in text:
        return text, 0
    removed = 0
    cleaned = text
    for pat in _TOOL_CALL_LEAK_PATTERNS:
        new_cleaned, n = pat.subn("", cleaned)
        removed += n
        cleaned = new_cleaned
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed


# =============================================================================
# Tool schemas (OpenAI tools format)
# =============================================================================

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "在互联网上搜索信息。返回若干条结果，每条含 title、url、snippet。"
            "用于查询实时信息（天气、价格、新闻、事件、人物近况、产品参数等）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。可包含时间词（如 '2026年5月'）以提高时效性。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数，1-10。",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "抓取并返回指定 URL 的正文内容。通常用于读取 web_search 返回的结果页。"
            "返回 title、url、content（已截断到合理长度）。"
            "对于 SPA、图表为主、或文本提取失败的页面，工具会自动回退为截图（视觉模式）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "完整的 http(s) URL。",
                },
            },
            "required": ["url"],
        },
    },
}

WEB_VIEW_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_view",
        "description": (
            "用浏览器渲染指定 URL 并返回页面截图。适用于以下场景：\n"
            "1. JS 重度渲染的 SPA（普通 web_fetch 抓不到正文）\n"
            "2. 图表、数据可视化、地图等需要视觉信息的页面\n"
            "3. 文档中关键内容是图片而非文本（财报、研报、政策文件等）\n"
            "返回一张截图，模型可以直接读图作答。注意：每次调用成本较高，"
            "若 web_fetch 能拿到文本就优先用 web_fetch。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "完整的 http(s) URL。",
                },
                "viewport": {
                    "type": "string",
                    "description": "可选，浏览器视窗尺寸，格式 '宽x高'，例如 '1280x1600'。默认 1280x1600。",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "是否截取整页（含滚动到下方的内容）。默认 false（只截首屏）。",
                },
            },
            "required": ["url"],
        },
    },
}

DEFAULT_TOOLS: list[dict[str, Any]] = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL, WEB_VIEW_TOOL]

# excel_query —— 仅在请求带有表格数据集时由调用方按需注册(非默认工具)。实现
# (调后端代码执行服务)由 adapter.py 注入,见 ToolRegistry.register()。
EXCEL_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "excel_query",
        "description": (
            "对用户已上传的表格数据集做精确计算与统计 —— 求和、计数、分组、"
            "排名、占比、多表关联对比等。传入一个用自然语言描述的、要计算的"
            "具体子问题;工具会写 SQL 在沙箱中执行,返回真实的计算结果(含所用 "
            "SQL)。\n"
            "凡涉及具体数字、统计口径、排名或对比,**必须**调用本工具取真实"
            "结果,不要凭记忆、看图估算或心算。一次只问一个明确的子问题;需要"
            "多个口径时分多次调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "要计算的子问题,自然语言描述,尽量具体并自带口径。"
                        "例:「按城市分组,统计每个城市的门店数量与平均日均"
                        "销售额,按门店数量降序排列」。"
                    ),
                },
            },
            "required": ["question"],
        },
    },
}

# 带表格数据集的请求,用这套系统提示词**取代**联网导向的 DEFAULT 提示词。
# 由来:联网那套的「必搜 / 没调工具就加"未联网核实"免责声明」对 Excel 分析
# 不适用,会把模型带偏;且数据集内容不在上下文里(在 excel_query 工具后面),
# 必须明确告诉模型这点,否则它会误以为「没材料」而拒答。
EXCEL_AGENT_SYSTEM_PROMPT = """你是严谨的数据分析助手。本次对话用户提供了一个表格数据集(Excel),你有一个 excel_query 工具,可对它做精确计算。

工作方式:
1. 数据集的内容**不在对话上下文里** —— 必须通过 excel_query 工具去查。任何涉及具体数字、统计、计数、分组、排名、占比、对比的结论,都必须先调用 excel_query 取真实计算结果,再据此作答。禁止心算、估算、编造,禁止凭记忆作答。
2. 面对宽泛的请求(如「帮我详细分析一下」),先调用 excel_query 了解数据集有哪些表、各表的列与概况,再据此规划要分析哪些维度、逐个查询并展开 —— **不要**因为「没有具体问题」就拒答或要求用户补充材料(数据集已经提供了,就在工具后面)。查询要抓重点:优先分析最关键的几个维度,不要把每一列都查一遍。给最终结论时,综合已查到的全部结果作结构化总结,**禁止**在结尾宣告「接下来我将查询…」这类未完成的动作 —— 要么把它查了,要么就此收尾。
3. 一次 excel_query 只问一个明确的子问题;需要多个口径就分多次调用。
4. 禁止声称调用了某工具而实际没有调用 —— 回答只能如实反映你真正执行过的操作。
5. 若 excel_query 的结果表明数据集中没有所需的列 / 维度 / 口径,如实说明「数据集中没有这项数据,无法据此分析」并指出需补充什么,不得用其他列硬凑。
6. 本次分析的数据来自 excel_query 工具(不是网页)—— 回答中**不要**编造 URL、**不要**追加「Sources」/「参考来源」章节、**不要**用 [1][2] 之类的引用编号。需要说明出处时,直接写「据 excel_query 查询」即可。"""


# =============================================================================
# System prompt — proven necessary by Phase 0
# =============================================================================

DEFAULT_SYSTEM_PROMPT_TEMPLATE = (
    "你是一个具备联网检索 + 视觉读图能力的助手。\n"
    "\n"
    "【当前时间】现在是 {current_date}（{current_weekday}），这是**真实世界的今天**。\n"
    "- 你的训练数据比今天旧，所以 {current_date} 在你的直觉里可能「像是未来」—— "
    "但它就是现在，请完全接受这一点。\n"
    "- 「今天」「明天」「本周」「最近」一律以 {current_date} 为基准。明天、后天都是"
    "**近期、可正常查询**的日期（天气预报、新闻都查得到），**绝不能**因为年份数字较大、"
    "或感觉「太遥远」「属于未来」就拒绝回答或说查不到。\n"
    "- 任何涉及具体日期、版本号、公司动态、产品发布、价格、天气、官方信息的问题，"
    "你的训练记忆很可能已经过时，必须以工具查到的结果为准。\n"
    "\n"
    "工具使用规则（必须严格遵守）：\n"
    "1. 对以下任一类问题，你**必须**先调用 web_search，**绝对禁止**凭记忆回答：\n"
    "   - 任何涉及**最新 / 当前 / 最近 / 现在 / 今天 / 本月 / 今年**的查询\n"
    "   - 任何询问**某产品 / 某模型 / 某公司**的版本号、发布日期、参数、定价、动态\n"
    "   - 实时信息：天气、新闻、价格、汇率、股价、比赛、政策法规、库存\n"
    "   - 任何**带年份数字**（如 '2025'、'2026'）的查询\n"
    "   - 用户主动要求「联网 / 查一下 / 搜一下」的问题\n"
    "2. 当 web_search 返回的 snippet **不包含**回答问题所需的具体数据"
    "（如具体的预报数值、价格、日期、参数、统计数字等）时，"
    "**必须**调用 web_fetch 抓取对应结果页的完整正文 —— 不要只凭摘要勉强作答或绕开问题。\n"
    "3. 当页面是图表、数据可视化、JS 重度渲染的 SPA、扫描版 PDF 等"
    "**正文是图像而非文本**的场景，调用 web_view 让浏览器渲染并截图。\n"
    "4. 同一时刻可发出多个并行的工具调用，提高效率。\n"
    "   对于较复杂、含多个子问题或需要交叉验证的查询，"
    "**首轮就并行发出 2-3 个不同角度的 web_search**"
    "（例如换关键词、加时间限定、拆成子问题），不要只搜一次就作答。\n"
    "5. 真正不需要外部信息的问题（基本常识、数学计算、纯逻辑题）可直接作答。\n"
    "\n"
    "工具结果信任原则（极其重要）：\n"
    "- 工具返回的信息**优先于**你的训练记忆。\n"
    "- 当工具结果与你的记忆冲突时（尤其是日期、版本号、价格、人物职位等），"
    "**一律以工具结果为准**，不要用「这可能是网站模板默认值」「应该是未来规划」"
    "之类的理由去推翻工具查到的事实。\n"
    "- 你的训练数据已经过时，工具查到的就是当前现实。\n"
    "\n"
    "信息时效性原则（极其重要）：\n"
    "- 用户问「现在 / 最新 / 当前」的信息时，你必须尽力拿到**尽可能新**的数据。\n"
    "- 如果你只搜了一两次、或只看到明显过时（比当前日期早很多）的数据 —— "
    "这是**任务没完成**，不是可接受的答案。\n"
    "- **严禁**用「可能已增长」「尚未发布最新」「建议查阅官方」「数据持续变化」"
    "这类免责声明来代替继续努力。出现这种情况时，你应当继续：换不同的关键词重新"
    "web_search（加上「财报」「年报」「最新」「官方」等词，或拆成子问题），"
    "并 web_fetch 抓取更权威的来源（官网、财报、近期新闻报道）。\n"
    "- 只有在确实换了多个角度搜索、也抓取了像样的来源之后仍找不到更新的数据时，"
    "才可以给出「目前能查到的最新是 X（截至 X 时间）」并说明检索过程。\n"
    "\n"
    "答案规范（极其重要）：\n"
    "- 引用规则：在句末用 [1][2] 标注来源。编号按来源在你**答案中首次出现的先后顺序**，"
    "从 [1] 起**连续递增**（[1]、[2]、[3]…）。\n"
    "- **不要**用来源在搜索结果列表里的位置当编号 —— 哪怕你引用的是搜索结果里的第 2 条和第 5 条，"
    "在答案里也必须写成 [1] 和 [2]。\n"
    "- 每一轮回答都**独立从 [1] 重新编号**，不延续历史对话轮次的编号。\n"
    "- 答案最后追加 'Sources:'，逐行列出 [1][2][3]… 各编号对应的 URL，"
    "必须与正文里的编号**一一对应、连续、不重不漏**。\n"
    "- **Sources 里的每一个 URL 必须是你本轮回答中真正通过 web_search / web_fetch / web_view 接触过的 URL**，"
    "**绝对禁止**列出你「印象中」「应该存在」或来自历史对话的 URL —— 这是事实性谎言。\n"
    "- 如果你这次没有调用任何工具（即基于训练数据作答），"
    "请在答案末尾明确写："
    "'⚠️ 本回答基于训练数据，未联网核实，可能已过时。'"
    "并且**不要**追加任何 Sources 列表。\n"
    "- 看图作答时，明确指出关键数字 / 趋势 / 文本是从截图中读到的。\n"
    "- 信息有冲突或不确定时，必须明确说明。\n"
)


def _render_system_prompt(template: str, now: Optional[_dt.datetime] = None) -> str:
    """Substitute {current_date} / {current_weekday} placeholders with server time.

    The model needs to know "today" so it can correctly judge what is
    time-sensitive. Without this, the model anchors on its training-data
    cutoff and treats post-cutoff facts as "I already know this".
    """
    if "{current_date}" not in template and "{current_weekday}" not in template:
        return template
    if now is None:
        now = _dt.datetime.now().astimezone()
    weekdays_zh = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return template.format(
        current_date=now.strftime("%Y-%m-%d"),
        current_weekday=weekdays_zh[now.weekday()],
    )


# Back-compat: the static string used by tests / non-templated callers.
DEFAULT_SYSTEM_PROMPT = _render_system_prompt(DEFAULT_SYSTEM_PROMPT_TEMPLATE)


# =============================================================================
# Tool registry — pluggable implementations
# =============================================================================

ToolImpl = Callable[[dict[str, Any]], Any]


class ToolRegistry:
    """Maps tool name → callable. Callable takes dict of args, returns
    any JSON-serializable value (usually str or dict)."""

    def __init__(self) -> None:
        self._impls: dict[str, ToolImpl] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(self, schema: dict[str, Any], impl: ToolImpl) -> None:
        name = schema["function"]["name"]
        self._impls[name] = impl
        self._schemas[name] = schema

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas.values())

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name not in self._impls:
            return {"error": f"unknown tool: {name}"}
        try:
            return self._impls[name](args)
        except Exception as exc:  # noqa: BLE001 — tool errors must not crash loop
            return {"error": f"{type(exc).__name__}: {exc}"}


# =============================================================================
# Agent loop (Phase 1: 2-hop fixed flow)
# =============================================================================


@dataclass
class AgentConfig:
    upstream_url: str  # full URL e.g. http://.../v1/chat/completions
    upstream_auth_header: str = "Authorization"
    upstream_auth_value: str = ""  # e.g. "Bearer xxx" or a raw EAS token
    model: str = ""
    # Stored as a *template* with {current_date}/{current_weekday} placeholders;
    # the placeholders are rendered per-request via _render_system_prompt(),
    # so a long-running adapter always tells the model the actual current date.
    system_prompt: str = DEFAULT_SYSTEM_PROMPT_TEMPLATE
    request_timeout: int = 120
    max_tool_result_chars: int = 8000
    parallel_dispatch_workers: int = 4
    # Phase 2: budget control
    max_iterations: int = 6          # hard cap on agent turns (room for dig-deeper pushbacks)
    max_fetches: int = 8             # cap on web_fetch + web_view calls per session
    max_searches: int = 8            # cap on web_search calls per session
    max_pushbacks: int = 2           # times the loop forces "dig deeper" on a stale/hedged answer
    # 引用合规审计只对 web 检索有意义;数据类请求(带 excel 数据集)关掉它 ——
    # 否则模型为工具调用编造的占位 URL 会被审计当真、给用户弹"引用合规警告"。
    citation_guard: bool = True
    # Force-answer 前的 prompt 字数预算:工具结果会累积,实测 EAS Qwen3.5 在
    # 6 轮 search/fetch + force_answer 时会撞 input 上限(HTTP 400 21ms 立刻拒),
    # 比纸面 256K context 紧得多。v0.2.16 把默认从 600K(基本=disabled)降到 100K:
    # 100K char × 1.5 token/char ≈ 150K token,留 ~100K token 给输出和模板。
    # 超阈值时丢最早 role=tool 消息为占位(_truncate_messages_for_budget)。
    max_context_chars: int = 100_000
    # force_answer 单独的更紧预算(留更多输出空间)。默认 = max_context_chars * 0.6。
    # 0 表示沿用 max_context_chars。
    force_answer_max_context_chars: int = 60_000
    # v0.2.14 空响应兜底:上游 Qwen thinking 模式偶发"只发 reasoning_content,content
    # 为空或只有 \\n\\n,finish_reason 已结束"的 case。检测到这种空响应时,**自动
    # 重试一次**,带 chat_template_kwargs.enable_thinking=false 强制模型直出 content。
    # 仍空就用 reasoning_buf 作兜底答案。0 关闭重试,纯走 reasoning 兜底。
    max_empty_retries: int = 1
    # v0.2.17 thinking 策略:agentic 循环每轮单独决定是否启用 thinking。
    # - intermediate(前 N-1 轮,主要任务是"决策调哪个工具"):默认**关**,因为模型
    #   有 tool_call 这个外置思考机制,内置 chain-of-thought 在 tool-call 决策场景
    #   下基本是浪费输出 token + 拖慢推理 + 增加触发"只输出 reasoning 忘了 content"
    #   bug 的概率。关掉直接消灭 v0.2.14 兜底的整类场景。
    # - force_answer(最后一轮综合所有工具结果作答):默认**开**,质量优先。
    intermediate_thinking_enabled: bool = False
    force_answer_thinking_enabled: bool = True


@dataclass
class AgentTrace:
    iterations: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    upstream_latencies_ms: list[int] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    # Phase 2
    stopped_reason: str = ""             # "answered" | "max_iterations" | "no_choices"
    searches_used: int = 0
    fetches_used: int = 0
    duplicate_calls_skipped: int = 0
    tool_call_leaks_stripped: int = 0    # safety-net cleanup of leaked <tool_call> markup
    pushbacks_used: int = 0              # times the loop forced "dig deeper" on a stale/hedged answer
    # Hotfix P0-2: citation guard
    verified_urls: set[str] = field(default_factory=set)  # URLs actually touched by tools this session
    unverified_urls_in_answer: list[str] = field(default_factory=list)  # URLs in final content that weren't touched
    # Phase 4 P2: observability
    final_finish_reason: str = ""  # upstream finish_reason of the final answer ("stop" / "length" / ...)
    answer_truncated: bool = False  # True when final_finish_reason == "length"


ProgressCallback = Callable[[str, str, dict[str, Any]], None]
# stage, message, meta

SourcesCallback = Callable[[str, list[dict[str, Any]]], None]
# query, items —— 调用一次 = 一次 web_search 的结构化结果。前端累加去重。


def _emit(cb: Optional[ProgressCallback], stage: str, message: str, **meta: Any) -> None:
    if cb is None:
        return
    try:
        cb(stage, message, meta)
    except Exception:  # noqa: BLE001 — progress callback failures are non-fatal
        pass


def _build_upstream_request(
    cfg: AgentConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
    stream: bool = False,
) -> urllib.request.Request:
    """Build the upstream POST request (shared by streaming + non-streaming)."""
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = True
    if extra:
        for key, value in extra.items():
            # `stream`/`stream_options` excluded: internal agentic calls are non-streaming.
            if key in {"messages", "tools", "tool_choice", "stream", "stream_options", "model"}:
                continue
            payload[key] = value
    headers = {
        "Content-Type": "application/json",
        cfg.upstream_auth_header: cfg.upstream_auth_value,
    }
    if not cfg.upstream_auth_value:
        headers.pop(cfg.upstream_auth_header, None)
    return urllib.request.Request(
        cfg.upstream_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _call_upstream(
    cfg: AgentConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Non-streaming POST. Returns the full parsed response dict (with tool_calls
    intact when finish_reason == 'tool_calls')."""
    req = _build_upstream_request(cfg, messages, tools, extra, stream=False)
    with urllib.request.urlopen(req, timeout=cfg.request_timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _stream_upstream(
    cfg: AgentConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
):
    """Streaming variant of ``_call_upstream``:逐 chunk yield 上游 SSE 块解出的
    OpenAI 标准 dict。``data: [DONE]`` 触发正常退出。

    调用方负责处理 ``urllib.error.HTTPError`` / 连接异常(包在 try/except)。

    v0.2.16: HTTPError 抛出前,**把 response body 头 500 char 拼到异常 message**,
    上层 ``agent_error`` 事件就能看到 EAS 的实际报错(context too long / 鉴权失败
    等),不再像之前那样只看到一句"HTTP Error 400: Bad Request"。
    """
    req = _build_upstream_request(cfg, messages, tools, extra, stream=True)
    try:
        resp = urllib.request.urlopen(req, timeout=cfg.request_timeout)
    except urllib.error.HTTPError as exc:
        # 读取 response body 拼进异常,方便上层定位 EAS 拒因
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            body = ""
        # 重新抛一个带 body 的 HTTPError 给上层 try/except 捕获
        raise urllib.error.HTTPError(
            exc.url, exc.code, f"{exc.reason} | body: {body}", exc.headers, None
        ) from None
    with resp:
        buffer = b""
        while True:
            try:
                chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
            except Exception:  # noqa: BLE001 — 连接断了,正常退出
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


def _assemble_tool_calls_from_deltas(delta_fragments: list) -> list[dict[str, Any]]:
    """从流式 ``choices[0].delta.tool_calls`` 增量片段拼出完整 tool_calls 列表。

    OpenAI 流式协议:首个 fragment 给 ``index / id / type / function.name``;
    后续 fragment 把 ``function.arguments`` 一段段拼上(并发工具按 ``index`` 区分)。
    """
    by_index: dict[int, dict[str, Any]] = {}
    for fragment in delta_fragments:
        if not isinstance(fragment, list):
            continue
        for tc in fragment:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            cur = by_index.setdefault(
                idx,
                {"id": "", "type": "function",
                 "function": {"name": "", "arguments": ""}},
            )
            if tc.get("id"):
                cur["id"] = tc["id"]
            if tc.get("type"):
                cur["type"] = tc["type"]
            fn = tc.get("function") or {}
            if isinstance(fn.get("name"), str):
                cur["function"]["name"] += fn["name"]
            if isinstance(fn.get("arguments"), str):
                cur["function"]["arguments"] += fn["arguments"]
    return [by_index[k] for k in sorted(by_index)]


def _filter_valid_tool_calls(
    assembled: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """过滤掉 ``arguments`` 不合法的 tool_call(真流式拼装"半成品")。

    问题来源:Qwen 真流式 tool_calls 偶尔同时 emit 多个调用,但只发完其中一个
    的 ``function.arguments`` fragments 就 ``finish_reason=tool_calls``,留下
    其他的 ``arguments=""``。这种 tool_call 一旦塞进 assistant_message 喂回上游,
    EAS 会**立刻 HTTP 400**(OpenAI 协议:``arguments`` 必须是合法 JSON 字符串)。

    过滤规则:``arguments`` 必须是非空字符串且能 ``json.loads``。``arguments=="{}"``
    是合法的(代表"无参数调用"),保留 —— 让 dispatcher 报错给模型,模型下一轮
    能自行修正;直接 drop 会让模型看不到自己的失败。

    Returns: (valid_list, dropped_count)
    """
    valid: list[dict[str, Any]] = []
    dropped = 0
    for tc in assembled:
        fn = tc.get("function") or {}
        raw = fn.get("arguments", "")
        if not isinstance(raw, str) or not raw.strip():
            dropped += 1
            continue
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            dropped += 1
            continue
        valid.append(tc)
    return valid, dropped


def _call_signature(tool_call: dict[str, Any]) -> str:
    """Stable signature for de-duplication: name + canonicalized args."""
    fn = tool_call.get("function", {}) or {}
    name = fn.get("name", "")
    raw = fn.get("arguments", "{}")
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError, json.JSONDecodeError):
        canonical = str(raw)
    return f"{name}::{canonical}"


def _truncate_tool_result(value: Any, max_chars: int) -> str:
    """Serialize a tool result to a string that fits the per-tool budget."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[truncated, {len(text) - max_chars} more chars]"
    return text


# ── Context 预算估算 + 裁剪(防 force_answer 把 EAS 输入撑爆 400)──────────
# 真实问题:多轮 web_search + web_fetch 累积后,force_answer 把整段 history
# 喂给上游,可能超 Qwen3.5 输入上限(256K - 输出预留)。每张 web_view 截图按
# 多模态算 ~1000-2000 tokens,粗略按 2000 char 估算。

_IMAGE_PSEUDO_CHARS = 2000


def _estimate_messages_size(messages: list) -> int:
    """估算 messages 总 char 数(图片按 _IMAGE_PSEUDO_CHARS 算)。不精确但够用。"""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += len(part.get("text") or "")
                elif part.get("type") == "image_url":
                    total += _IMAGE_PSEUDO_CHARS
        tcs = m.get("tool_calls") or []
        if tcs:
            try:
                total += len(json.dumps(tcs, ensure_ascii=False))
            except (TypeError, ValueError):
                total += 200 * len(tcs)
    return total


def _truncate_messages_for_budget(messages: list, max_chars: int) -> list:
    """超预算时,**只丢 role=tool 的早期消息**,替换为占位(保留对应的 assistant
    tool_call 消息,不破坏对话结构)。system / user / assistant prose 不动。
    """
    if _estimate_messages_size(messages) <= max_chars:
        return messages
    out = list(messages)
    placeholder = "[早期工具结果已省略以适配上下文长度限制]"
    for i, m in enumerate(out):
        if m.get("role") != "tool":
            continue
        out[i] = {
            "role": "tool",
            "tool_call_id": m.get("tool_call_id", ""),
            "content": placeholder,
        }
        if _estimate_messages_size(out) <= max_chars:
            break
    return out


def _strip_citation_scaffolding(text: str) -> str:
    """Remove citation scaffolding from a historical assistant answer.

    Strips the trailing 'Sources:' / '来源:' block, any '⚠️' warning block,
    and inline [N] citation markers. Prior-turn citation numbering must not
    leak into the current turn — otherwise the model continues numbering from
    where the last turn left off (e.g. [5], [6]) instead of restarting at [1].
    """
    if not isinstance(text, str) or not text:
        return text
    cut = len(text)
    for marker in ("\nSources:", "\nSources：", "\n来源:", "\n来源：", "\n⚠️", "⚠️"):
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    cleaned = text[:cut]
    cleaned = re.sub(r"\s*\[\d{1,3}\]", "", cleaned)  # drop inline [N] markers
    return cleaned.rstrip()


def _sanitize_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with citation scaffolding stripped from
    historical assistant messages. Keeps prior-turn answers as clean prose so
    the current turn numbers its citations fresh from [1]."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            out.append({**m, "content": _strip_citation_scaffolding(m["content"])})
        else:
            out.append(m)
    return out


def _ensure_system_prompt(
    messages: list[dict[str, Any]],
    system_prompt: str,
) -> list[dict[str, Any]]:
    """Inject the agentic system prompt at the front if not already present.

    The ``system_prompt`` is treated as a *template* — {current_date} and
    {current_weekday} placeholders are substituted with server time on every
    invocation. This is critical: a long-running adapter must tell the model
    the actual current date, otherwise the model anchors on its training-data
    cutoff and treats post-cutoff facts as "already known".

    If the caller supplied their own system message, we *prepend* the agentic
    requirement so both are preserved (agentic rules apply on top of the
    user's domain-specific instructions).
    """
    if not system_prompt:
        return list(messages)
    rendered = _render_system_prompt(system_prompt)
    head = [{"role": "system", "content": rendered}]
    if messages and messages[0].get("role") == "system":
        existing = messages[0].get("content", "")
        head = [{"role": "system", "content": rendered + "\n\n" + str(existing)}]
        return head + list(messages[1:])
    return head + list(messages)


def _make_tool_message(
    tc: dict[str, Any],
    result: Any,
    max_chars: int,
) -> dict[str, Any]:
    """Convert a tool result into an OpenAI-format tool message.

    Special-case: if the result is a dict with content_type='image' (returned by
    web_view, or by web_fetch's vision fallback), build a multimodal content
    list with both a text annotation and the image_url part. This requires the
    upstream model + vLLM tool parser to accept content arrays in tool messages
    — verified to work for Qwen3-VL-235B-A22B via vLLM 0.11.0 hermes parser.
    """
    if isinstance(result, dict) and result.get("content_type") == "image":
        image_url = result.get("image_url", "")
        annotation = result.get("description") or f"[web_view screenshot of {result.get('url','?')}]"
        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": annotation[:max_chars]},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
        return {"role": "tool", "tool_call_id": tc.get("id", ""), "content": content_parts}
    content = _truncate_tool_result(result, max_chars)
    return {"role": "tool", "tool_call_id": tc.get("id", ""), "content": content}


def _dispatch_tool_calls_parallel(
    tool_calls: list[dict[str, Any]],
    registry: ToolRegistry,
    cfg: AgentConfig,
    progress_cb: Optional[ProgressCallback],
    trace: AgentTrace,
    seen_signatures: set[str],
    sources_cb: Optional[SourcesCallback] = None,
) -> list[dict[str, Any]]:
    """Execute tool_calls in parallel, deduping by signature and enforcing budgets.

    Returns one tool message per input tool_call (matched by id), so the
    upstream conversation history stays valid. Duplicate or budget-blocked
    calls return synthetic tool results instead of running.
    """
    indexed: dict[int, dict[str, Any]] = {}

    def _make_msg(tc: dict[str, Any], content: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": tc.get("id", ""), "content": content}

    fresh: list[tuple[int, dict[str, Any], str, dict[str, Any]]] = []
    # (idx, tool_call, name, args)

    for idx, tc in enumerate(tool_calls):
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "")
        raw_args = fn.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            args = {}

        sig = _call_signature(tc)
        if sig in seen_signatures:
            trace.duplicate_calls_skipped += 1
            _emit(progress_cb, "tool_skip_duplicate", f"跳过重复调用 {name}", name=name, args=args)
            indexed[idx] = _make_msg(
                tc,
                "[skipped: this exact tool call was already executed earlier in this session — "
                "consult prior tool result or pick a different query/url.]",
            )
            continue

        # Budget checks (per-tool). web_view shares the fetch budget since
        # both consume page-read quota.
        if name in ("web_fetch", "web_view") and trace.fetches_used >= cfg.max_fetches:
            _emit(progress_cb, "tool_budget_block", f"{name} 预算耗尽，跳过", name=name)
            indexed[idx] = _make_msg(
                tc,
                f"[blocked: {name} budget exhausted ({cfg.max_fetches}). "
                "Answer using already-fetched material.]",
            )
            continue
        if name == "web_search" and trace.searches_used >= cfg.max_searches:
            _emit(progress_cb, "tool_budget_block", f"web_search 预算耗尽，跳过", name=name)
            indexed[idx] = _make_msg(
                tc,
                f"[blocked: web_search budget exhausted ({cfg.max_searches}). "
                "Answer using already-collected results.]",
            )
            continue

        seen_signatures.add(sig)
        if name in ("web_fetch", "web_view"):
            trace.fetches_used += 1
        elif name == "web_search":
            trace.searches_used += 1
        fresh.append((idx, tc, name, args))

    def _run_one(idx: int, tc: dict[str, Any], name: str, args: dict[str, Any]) -> None:
        _emit(progress_cb, "tool_start", f"调用 {name}", name=name, args=args)
        t0 = time.time()
        result = registry.dispatch(name, args)
        elapsed_ms = int((time.time() - t0) * 1000)
        ok = "error" not in str(result)[:50] if not isinstance(result, dict) else not result.get("error")
        # web_search → emit structured sources to the SSE stream for the
        # frontend cite chips / hover preview (零干扰主对话上下文)。
        if sources_cb is not None and name == "web_search" and ok:
            shaped = _shape_web_search_sources(result)
            if shaped:
                try:
                    sources_cb(str(args.get("query") or ""), shaped)
                except Exception:  # noqa: BLE001 — emission failure must not break the loop
                    pass
        is_image = isinstance(result, dict) and result.get("content_type") == "image"
        # Citation guard: record URLs the tool actually touched (args + result)
        if name in ("web_fetch", "web_view"):
            arg_url = args.get("url") if isinstance(args, dict) else None
            if isinstance(arg_url, str) and arg_url.startswith(("http://", "https://")):
                trace.verified_urls.add(_normalize_url(arg_url))
        _collect_verified_urls_from_result(result, trace.verified_urls)
        trace.tool_calls.append(
            {
                "name": name,
                "args": args,
                "elapsed_ms": elapsed_ms,
                "ok": ok,
                "modality": "image" if is_image else "text",
            }
        )
        _emit(
            progress_cb,
            "tool_end",
            f"{name} 完成 ({elapsed_ms}ms{', 图片' if is_image else ''})",
            name=name,
            elapsed_ms=elapsed_ms,
            modality="image" if is_image else "text",
        )
        indexed[idx] = _make_tool_message(tc, result, cfg.max_tool_result_chars)

    if fresh:
        with ThreadPoolExecutor(max_workers=max(cfg.parallel_dispatch_workers, 1)) as pool:
            futures = [pool.submit(_run_one, idx, tc, name, args) for idx, tc, name, args in fresh]
            for fut in as_completed(futures):
                fut.result()

    return [indexed[i] for i in sorted(indexed)]


FORCE_ANSWER_SYSTEM_HINT = (
    "【最重要的指令】你已达到工具调用预算上限。\n"
    "现在你必须**立即用自然语言**直接回答用户的问题，基于已经收集到的工具结果。\n"
    "\n"
    "严格禁止以下行为：\n"
    "- 禁止输出 <tool_call>、</tool_call>、<function_call> 等任何标签\n"
    "- 禁止输出 JSON 形式的 {\"name\":\"...\",\"arguments\":...} 工具调用语法\n"
    "- 禁止再尝试调用 web_search、web_fetch 或任何其他工具\n"
    "\n"
    "如果信息不足，请用自然语言告诉用户：哪部分信息你能确认，哪部分缺失或不确定。"
)


# Loop-level "dig deeper" pushback (v0.2.2). When the model finishes with an
# answer that hedges on stale/incomplete data instead of digging, the loop
# injects DIG_DEEPER_HINT and runs another iteration (bounded by max_pushbacks).
DIG_DEEPER_HINT = (
    "你刚才的回答承认了数据可能过时、不完整、不是用户要的精确信息，"
    "或者以为某个日期太遥远而查不到。\n"
    "你的工具预算还很充足 —— 不要用免责声明或「这是未来日期」搪塞过去。\n"
    "请注意：系统提示里的【当前时间】就是真实的今天，用户问的「明天」「最近」"
    "都是近期、可以正常查到的日期 —— 搜索结果里与之匹配的天气/新闻就是答案。\n"
    "现在请立刻继续深挖：\n"
    "① 换不同的关键词重新 web_search（加上「财报」「年报」「最新」「2026」「官方」"
    "等限定词，或把问题拆成更具体的子问题）；\n"
    "② web_fetch 抓取更权威的来源 —— 官网、财报/年报、近期新闻报道，而不是百科条目；\n"
    "尽力拿到尽可能新的数据，然后再给最终答案。"
)

# Substrings that signal the answer is hedging with stale/incomplete data
# rather than having dug for the current figure.
_HEDGE_PHRASES = (
    "可能已增长", "可能已经增长", "可能有所增长", "可能进一步", "可能已进一步",
    "尚未发布", "尚未公布", "尚未更新", "暂未发布", "未发布最新", "尚无最新", "尚无官方",
    "未找到最新", "没找到最新", "未能找到最新", "未查到", "没有找到关于", "没有查到",
    "并非最新", "不是最新", "可能并非", "可能不是最新", "未必是最新",
    "建议查阅", "建议关注", "建议访问", "请查阅", "查阅其官方", "以官方",
    "无法确认", "无法查询到", "无法获取", "无法提供", "暂无最新", "暂时没有",
    "再查询", "稍后查询", "自行查询", "自行核实", "建议您查询",
    "可能已经发生变化", "可能已发生变化", "持续动态变化", "动态变化", "持续增长",
    "需要查阅", "需查阅", "最新精确数字", "确切.*数据", "可能并不准确",
)


def _answer_needs_more_digging(content: str, searches_used: int = 0) -> bool:
    """Heuristic: does the assistant's final answer hedge with stale/incomplete
    data — or give up — instead of having dug for the current figure? Triggers
    the loop's bounded 'dig deeper' pushback."""
    if not isinstance(content, str) or not content:
        return False
    # Self-contradiction: the model ran searches this turn yet still appended the
    # "based on training data, not verified online" disclaimer. It searched, came
    # up short, and punted instead of digging deeper — a clear under-dig signal.
    if searches_used > 0 and ("未联网核实" in content or "本回答基于训练数据" in content):
        return True
    if any(p in content for p in _HEDGE_PHRASES if "." not in p):
        return True
    return bool(re.search("|".join(p for p in _HEDGE_PHRASES if "." in p), content))


def _audit_final_citations(content: str, trace: AgentTrace) -> None:
    """Find URLs in final content that weren't actually visited by tools.

    These are likely fabricated. We record them in trace.unverified_urls_in_answer
    and append a warning to the content so the human can see something is off.
    Stripping is intentionally NOT done by default — Phase 4 may add an env flag
    for hard-strip mode.
    """
    if not isinstance(content, str) or not content:
        return
    urls_in_answer = _extract_urls(content)
    if not urls_in_answer:
        return
    unverified = [u for u in urls_in_answer if u not in trace.verified_urls]
    if unverified:
        trace.unverified_urls_in_answer = unverified


def _citation_warning_text(unverified: list[str]) -> str:
    """Build the human-visible citation-compliance warning block."""
    if not unverified:
        return ""
    lines = [
        "",
        "⚠️ 引用合规警告：以下 URL 出现在答案中，但本次会话中工具**未访问过**它们，"
        "可能是模型编造或源自训练数据，建议自行核实：",
    ]
    for u in unverified:
        lines.append(f"  - {u}")
    return "\n" + "\n".join(lines)


def _annotate_final_message(
    message: dict[str, Any], trace: AgentTrace, citation_guard: bool = True
) -> None:
    """Post-process the assistant's final message: leak cleanup + citation audit.

    Idempotent — only mutates ``message['content']`` when something needs fixing.
    ``citation_guard=False`` 时跳过引用审计 —— 数据类请求(如 excel)没有 URL
    概念,审计会把模型为工具调用编造的占位 URL 当真、误报。
    """
    content = message.get("content")
    if not isinstance(content, str):
        return
    # 1) Leak cleanup (Phase 2 carry-over)
    cleaned, removed = _strip_tool_call_leaks(content)
    if removed:
        content = cleaned
        trace.tool_call_leaks_stripped += removed
    # 2) Citation audit (Hotfix P0-2)—— 仅 web 检索场景。
    if citation_guard:
        _audit_final_citations(content, trace)
        content = content + _citation_warning_text(trace.unverified_urls_in_answer)
    # 3) Empty-answer safety net — never ship an empty final answer (can happen
    # if the model emitted only leaked <tool_call> markup that got stripped).
    if not content.strip():
        content = "（抱歉，本轮未能整理出最终答案，检索过程可能未收敛。请重试或换一种问法。）"
        trace.stopped_reason = "answered_empty_fallback"
    message["content"] = content


# When the forced-answer iteration fails to produce real prose — the model
# emits only <tool_call> markup (stripped to nothing) or narrates its next
# action instead of answering — we fall back to a RAG-style synthesis call.
# It is a FRESH request that contains no tool-call scaffolding at all: just the
# conversation, a plain-text digest of everything the tools found, and an
# instruction to answer. With no <tool_call>/tool-result pairs in context the
# model has no pattern to imitate, so it reliably writes prose instead of
# trying to keep digging.
SYNTHESIS_SYSTEM_PROMPT = (
    "你是一个严谨的中文助理。下面会给你一段对话，以及为回答用户最后一个问题"
    "而检索到的【资料】。你已经没有任何工具可用，唯一的任务就是基于这些资料"
    "写出最终答案。\n"
    "要求：\n"
    "- 开门见山给出结论和关键数据，**禁止**写「我将」「我会」「接下来」「让我」"
    "「为了回答」之类描述下一步动作的话 —— 直接给答案本身。\n"
    "- 句末用 [1][2] 标注来源，编号按其在答案中首次出现的顺序从 [1] 起连续递增。\n"
    "- 答案末尾追加 'Sources:'，逐行列出每个编号对应的 URL，只能用【资料】中"
    "真实出现过的 URL。\n"
    "- 资料不足以给出精确答案时，明确说明你能确认什么、还缺什么，"
    "并给出资料里能查到的最接近的信息。\n"
    "- **绝对禁止**输出 <tool_call> 标签或任何 JSON 工具调用语法。"
)

# 数据类请求(如 excel)的合成提示词 —— 去掉 URL 引用要求,否则 synthesis 会
# 为 excel_query 结果编造 example.com 之类的占位 URL + Sources 章节。
EXCEL_SYNTHESIS_SYSTEM_PROMPT = (
    "你是一个严谨的数据分析助理。下面会给你一段对话,以及为回答用户最后一个"
    "问题而通过 excel_query 工具查到的【资料】。你已经没有任何工具可用,唯一"
    "的任务就是基于这些查询结果写出最终的分析结论。\n"
    "要求:\n"
    "- 开门见山给出结论和关键数据,**禁止**写「我将」「我会」「接下来」「让我」"
    "「为了回答」之类描述下一步动作的话 —— 直接给完整分析,把已查到的结果综合"
    "成结构化总结。\n"
    "- 数据来自 excel_query 工具(不是网页)—— **不要**编造 URL、**不要**追加"
    "「Sources」/「参考来源」章节、**不要**用 [1][2] 之类的引用编号。\n"
    "- 资料不足以给出精确答案时,明确说明你能确认什么、还缺什么。\n"
    "- **绝对禁止**输出 <tool_call> 标签或任何 JSON 工具调用语法。"
)

SYNTHESIS_USER_PREFIX = "【为回答上面最后一个问题，已检索到以下资料】\n\n"
SYNTHESIS_USER_SUFFIX = "\n\n请立即依据以上资料，用中文写出完整、直接的最终答案。"
SYNTHESIS_NO_EVIDENCE = "（本次没有成功检索到外部资料，请基于你已知的信息谨慎作答并说明未联网核实。）"

# Short, first-person phrases that signal the model is narrating its NEXT
# action instead of answering ("我将尝试访问…官网"). Treated as a leak — not a
# real answer — only when the whole message is short.
_INTENT_LEAD_PHRASES = (
    "我将", "我会去", "我会先", "我会尝试", "我现在", "我先", "我需要先",
    "我需要查", "我打算", "我准备", "我马上", "我去查", "我来查", "让我",
    "接下来我", "接下来，我", "下一步", "我接下来", "首先我", "我要先",
    "我应该先", "为了回答", "为了获取", "为了找到", "为了查",
)


def _looks_like_intent_not_answer(text: str) -> bool:
    """The model sometimes narrates its next tool action instead of answering
    ('我将尝试访问麦当劳的投资者关系页面…'). A short message that opens with a
    first-person intent phrase is such a leak, not a real final answer."""
    t = (text or "").strip()
    if not t or len(t) >= 160:
        return False
    head = t[:20]
    return any(p in head for p in _INTENT_LEAD_PHRASES)


# 强制收尾时,模型有时不给结论、而在结尾宣告「接下来我将查询X」这类下一步动作
# —— agent 迭代预算耗尽、答案被截在半路的信号。命中 → 走 synthesis 兜底,把
# 已查到的结果整理成完整结论。只认**第一人称**意图(「我将…查询」),不误伤
# 「建议下一步关注X」这类给用户的建议。
_DANGLING_INTENT_RE = re.compile(
    r"(我将|我会|我先|我来|我打算|我继续|我需要再?|让我|接下来我|下一步我)"
    r"[^。\n]{0,48}"
    r"(查询|查一下|查看|分析|分组|统计|计算|核对|检查|了解一下)"
)


def _ends_with_dangling_intent(text: str) -> bool:
    """长答案结尾若是『接下来我将查询…』这类宣告下一步、其后没有结果跟着 ——
    说明 agent 迭代预算耗尽、答案被截在半路,需走 synthesis 兜底重新作答。"""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_DANGLING_INTENT_RE.search(t[-150:]))


def _collect_tool_evidence(
    augmented: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Pull every tool result out of the loop conversation into a plain-text
    digest plus a list of image content-parts (from web_view / vision fallback).

    The digest deliberately drops all tool-call scaffolding so a fresh
    synthesis call sees only evidence — no <tool_call>/tool-result pattern to
    imitate.
    """
    text_chunks: list[str] = []
    image_parts: list[dict[str, Any]] = []
    idx = 0
    for m in augmented:
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if isinstance(content, list):
            texts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    texts.append(str(part["text"]))
                elif part.get("type") == "image_url":
                    image_parts.append(part)
            content = "\n".join(texts)
        if isinstance(content, str) and content.strip():
            idx += 1
            text_chunks.append(f"【资料{idx}】\n{content.strip()}")
    return "\n\n".join(text_chunks), image_parts


def _build_synthesis_messages(
    augmented: list[dict[str, Any]],
    citation_mode: bool = True,
) -> list[dict[str, Any]]:
    """Build a fresh, tool-free message list for the synthesis call: our own
    system prompt, the plain user/assistant turns (scaffolding stripped), and a
    final user turn carrying the evidence digest (plus any screenshots).

    ``citation_mode=False``(数据类请求,如 excel)用不带 URL 引用的合成提示词
    —— 否则 synthesis 会为 excel_query 结果编造占位 URL。"""
    sys_prompt = SYNTHESIS_SYSTEM_PROMPT if citation_mode else EXCEL_SYNTHESIS_SYSTEM_PROMPT
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": sys_prompt}
    ]
    for m in augmented:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        if role == "assistant" and m.get("tool_calls"):
            continue  # drop tool-call requests — keep only plain prose turns
        content = m.get("content")
        if role == "assistant" and isinstance(content, str):
            content, _ = _strip_tool_call_leaks(content)
        if content in (None, "") or (isinstance(content, str) and not content.strip()):
            continue
        msgs.append({"role": role, "content": content})
    digest, image_parts = _collect_tool_evidence(augmented)
    user_text = SYNTHESIS_USER_PREFIX + (digest or SYNTHESIS_NO_EVIDENCE) + SYNTHESIS_USER_SUFFIX
    if image_parts:
        msgs.append(
            {"role": "user", "content": [{"type": "text", "text": user_text}, *image_parts]}
        )
    else:
        msgs.append({"role": "user", "content": user_text})
    return msgs


def _synthesize_answer(
    cfg: AgentConfig,
    augmented: list[dict[str, Any]],
    extra_payload: Optional[dict[str, Any]],
    trace: AgentTrace,
) -> str:
    """RAG-style fallback: answer from a digest of tool results in a fresh,
    tool-free request. Reliable because the request carries no tool-call
    scaffolding for the model to imitate. Returns cleaned prose (empty only if
    the upstream call itself fails)."""
    # cfg.citation_guard 兼作 web/数据模式信号:数据类请求用不带引用的合成提示词。
    msgs = _build_synthesis_messages(augmented, cfg.citation_guard)
    t0 = time.time()
    try:
        resp = _call_upstream(cfg, msgs, [], extra_payload)
    except Exception:  # noqa: BLE001
        return ""
    trace.upstream_latencies_ms.append(int((time.time() - t0) * 1000))
    choices = resp.get("choices", [])
    if not choices:
        return ""
    content = (choices[0].get("message", {}) or {}).get("content") or ""
    cleaned, _ = _strip_tool_call_leaks(content)
    return cleaned.strip()


def _finalize_answer(
    msg: dict[str, Any],
    cfg: AgentConfig,
    augmented: list[dict[str, Any]],
    extra_payload: Optional[dict[str, Any]],
    trace: AgentTrace,
) -> None:
    """Ensure the final assistant message carries a real answer, then annotate.

    The forced-answer iteration sometimes comes back unusable — either empty
    (the model emitted only <tool_call> markup, stripped to nothing) or a
    leaked intent fragment ('我将尝试访问…'). In both cases we run ONE
    synthesis pass (RAG-style, tool-free) which reliably produces prose. If
    that upstream call also fails, the raw unusable content is dropped so the
    empty-answer safety net in _annotate_final_message takes over.
    """
    raw = msg.get("content") or ""
    stripped, _ = _strip_tool_call_leaks(raw)
    needs_synthesis = (
        (not stripped.strip())
        or _looks_like_intent_not_answer(stripped)
        or _ends_with_dangling_intent(stripped)
    )
    if needs_synthesis:
        synthesized = _synthesize_answer(cfg, augmented, extra_payload, trace)
        if synthesized:
            msg["content"] = synthesized
            trace.stopped_reason = "answered_synthesized"
        else:
            msg["content"] = ""  # let _annotate_final_message ship the safety-net text
    _annotate_final_message(msg, trace, cfg.citation_guard)


def _is_final_message(choice: dict[str, Any]) -> bool:
    """Determine whether this choice is the model's final answer (no further tool calls)."""
    msg = choice.get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    finish_reason = choice.get("finish_reason", "")
    return (not tool_calls) or (finish_reason != "tool_calls")


def run_agent(
    messages: list[dict[str, Any]],
    cfg: AgentConfig,
    registry: ToolRegistry,
    extra_payload: Optional[dict[str, Any]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> tuple[dict[str, Any], AgentTrace]:
    """Phase 2: N-iteration agent loop with budget control.

    Loop:
        for iteration in 1..max_iterations:
            call upstream (with tools, unless final iteration → tools=[])
            if no tool_calls → return as final answer
            dispatch tool_calls in parallel (with dedup + per-tool budget)
            append (assistant_message + tool_results) to conversation
        else (loop exhausted): force one more call with tools=[] to extract final answer

    Returns (final_response_dict, trace).
    """
    trace = AgentTrace()
    tools = registry.schemas()
    augmented = _ensure_system_prompt(_sanitize_history(messages), cfg.system_prompt)
    seen_signatures: set[str] = set()
    last_response: dict[str, Any] = {}

    for iteration in range(1, cfg.max_iterations + 1):
        is_last = iteration == cfg.max_iterations
        if is_last:
            # On the final iteration, hide the tools to force the model to answer.
            current_tools: list[dict[str, Any]] = []
            current_messages = augmented + [{"role": "system", "content": FORCE_ANSWER_SYSTEM_HINT}]
            _emit(progress_cb, "agent_force_answer", "最后一轮，强制模型作答", iteration=iteration)
        else:
            current_tools = tools
            current_messages = augmented
            _emit(progress_cb, "iteration_start", f"开始第 {iteration} 轮模型调用", iteration=iteration)

        t0 = time.time()
        resp = _call_upstream(cfg, current_messages, current_tools, extra_payload)
        trace.upstream_latencies_ms.append(int((time.time() - t0) * 1000))
        trace.iterations = iteration
        last_response = resp

        choices = resp.get("choices", [])
        if not choices:
            trace.stopped_reason = "no_choices"
            return resp, trace

        choice = choices[0]
        if _is_final_message(choice):
            msg = choice.get("message", {}) or {}
            # Loop-level "dig deeper" pushback: if the answer hedges on stale/
            # incomplete data and there is room left to both dig AND answer
            # (>= 2 iterations remaining), push for another round.
            if (
                iteration <= cfg.max_iterations - 2
                and trace.pushbacks_used < cfg.max_pushbacks
                and _answer_needs_more_digging(msg.get("content") or "", trace.searches_used)
            ):
                trace.pushbacks_used += 1
                _emit(
                    progress_cb,
                    "agent_dig_deeper",
                    f"答案疑似过时/不完整，要求继续深挖（第 {trace.pushbacks_used} 次）",
                    pushback=trace.pushbacks_used,
                )
                augmented = augmented + [msg, {"role": "system", "content": DIG_DEEPER_HINT}]
                continue
            trace.stopped_reason = "answered"
            trace.final_finish_reason = str(choice.get("finish_reason") or "")
            trace.answer_truncated = trace.final_finish_reason == "length"
            _finalize_answer(msg, cfg, augmented, extra_payload, trace)
            if trace.tool_call_leaks_stripped:
                _emit(
                    progress_cb,
                    "tool_call_leak_stripped",
                    f"清洗了 {trace.tool_call_leaks_stripped} 处泄漏的 <tool_call> 标签",
                    removed=trace.tool_call_leaks_stripped,
                )
            if trace.unverified_urls_in_answer:
                _emit(
                    progress_cb,
                    "citation_warn",
                    f"答案中发现 {len(trace.unverified_urls_in_answer)} 个未验证 URL",
                    unverified=trace.unverified_urls_in_answer,
                )
            return resp, trace

        # Tool calls present — dispatch and continue
        message = choice.get("message", {}) or {}
        tool_calls = message.get("tool_calls") or []
        _emit(
            progress_cb,
            "tools_dispatch",
            f"模型请求 {len(tool_calls)} 个工具调用",
            count=len(tool_calls),
            iteration=iteration,
        )
        tool_messages = _dispatch_tool_calls_parallel(
            tool_calls, registry, cfg, progress_cb, trace, seen_signatures
        )
        augmented = augmented + [message] + tool_messages

    # Should not reach here because the final iteration disables tools and returns;
    # keep as defensive fallback.
    trace.stopped_reason = "max_iterations"
    return last_response, trace


# =============================================================================
# Streaming variant — emits SSE-shaped events as the loop progresses
# =============================================================================


def _sse_progress_chunk(model: str, stage: str, message: str, **meta: Any) -> dict[str, Any]:
    """OpenAI-shaped progress chunk carrying our x_adapter_agent_progress extension.

    Compatible with vanilla OpenAI clients (they see an empty delta and
    ignore the unknown extension field).
    """
    return {
        "id": "agent-progress",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model or "adapter",
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "x_adapter_agent_progress": {"stage": stage, "message": message, **meta},
    }


def _sse_sources_chunk(
    model: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """OpenAI-shaped event carrying our ``x_adapter_sources`` extension —— 一次
    web_search 工具调用的结构化结果(按 BACKEND_TODO #1 协议:**扁平 array**,
    每条 ``n`` 字段为 session 级递增 stable id)。

    模型仍按系统提示词在正文里写 ``[N]`` 和 ``Sources:`` 段;本事件**只补**
    title / favicon / snippet 等元数据,前端用 url 去重 + 匹配正文 ``[N]``。
    query 信息走 ``x_adapter_agent_progress.tool_start.args.query``,这里不重复。
    """
    return {
        "id": "agent-sources",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model or "adapter",
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "x_adapter_sources": items,
    }


def _sse_trace_chunk(model: str, trace: AgentTrace) -> dict[str, Any]:
    return {
        "id": "agent-trace",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model or "adapter",
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "x_adapter_agent_trace": {
            "iterations": trace.iterations,
            "stopped_reason": trace.stopped_reason,
            "tool_calls": trace.tool_calls,
            "upstream_latencies_ms": trace.upstream_latencies_ms,
            "searches_used": trace.searches_used,
            "fetches_used": trace.fetches_used,
            "duplicate_calls_skipped": trace.duplicate_calls_skipped,
            "tool_call_leaks_stripped": trace.tool_call_leaks_stripped,
            "pushbacks_used": trace.pushbacks_used,
            "verified_urls": sorted(trace.verified_urls),
            "unverified_urls_in_answer": trace.unverified_urls_in_answer,
            "final_finish_reason": trace.final_finish_reason,
            "answer_truncated": trace.answer_truncated,
            "elapsed_total_ms": int((time.time() - trace.started_at) * 1000),
        },
    }


def _stream_final_answer(model: str, content: str, resp: dict[str, Any]):
    """Emit a finalized answer string as a canonical OpenAI streaming sequence:
    one role-only opening chunk, then small content chunks, then the stop chunk.

    Splitting role and content into *separate* chunks — and the content into
    small pieces — matches the token-by-token shape that proxies (e.g. LiteLLM)
    and chat UIs reliably render. A single combined {"role","content"} delta can
    have its content silently dropped by a proxy, surfacing as an empty reply.
    """
    chunk_id = resp.get("id") or "agent-final"
    created = resp.get("created") or int(time.time())
    base = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model}
    # 1) opening chunk carries only the role
    yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    # 2) the answer text, in small content-only chunks
    step = 64
    for i in range(0, len(content), step):
        yield {
            **base,
            "choices": [{"index": 0, "delta": {"content": content[i:i + step]}, "finish_reason": None}],
        }
    # 3) terminal stop chunk
    stop = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    if resp.get("usage"):
        stop["usage"] = resp["usage"]
    yield stop


def _speculative_iteration(
    cfg: AgentConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
):
    """Drive one upstream streaming call with speculative content/tool_calls/empty detection.

    Yields a stream of ``("chunk", dict)`` tuples for client-bound SSE chunks
    (these should be forwarded as-is to the wire). When the upstream ends,
    yields a single terminal ``("done", state)`` where ``state['path']`` is:
      - ``'content'``  → answer streamed live (state['content_buf'] has the text)
      - ``'tool_calls'`` → assistant requested tools; assembled fragments in state
      - ``'empty'``    → no usable output (often just reasoning_content)
      - ``'upstream_error'`` → HTTP/network exception (state['error'] populated)

    v0.2.14 关键改动:**``content_started`` 只在出现非空白 content 才承诺**。
    上游若先发一串 ``reasoning_content`` 再发 ``\\n\\n`` 然后结束(Qwen thinking
    模式空答案 bug,见 BACKEND_REQUESTS_v0.11),不会被误判为 'content' 路径 ——
    那些空白 chunk 留在 ``pre_decision_chunks`` 里,流结束后归入 'empty',
    供调用方决定 retry / reasoning fallback。
    """
    pre_decision_chunks: list[dict[str, Any]] = []
    content_started = False
    tool_calls_started = False
    content_buf = ""              # 累积所有 content delta(含空白,用于 empty 检测)
    reasoning_buf = ""            # 累积所有 reasoning_content delta
    tool_call_fragments: list = []
    finish_reason = ""

    try:
        for chunk in _stream_upstream(cfg, messages, tools, extra):
            choices = chunk.get("choices") or []
            if not choices:
                # usage-only / 心跳块:承诺后转发,未承诺前 buffer
                if content_started:
                    yield ("chunk", chunk)
                elif not tool_calls_started:
                    pre_decision_chunks.append(chunk)
                continue
            ch0 = choices[0] or {}
            delta = ch0.get("delta") or {}
            fr = ch0.get("finish_reason")
            if fr:
                finish_reason = str(fr)

            tc_delta = delta.get("tool_calls")
            content_delta = delta.get("content")
            reasoning_delta = delta.get("reasoning_content")

            # reasoning 累积(不影响路径承诺)
            if isinstance(reasoning_delta, str) and reasoning_delta:
                reasoning_buf += reasoning_delta

            # tool_calls delta:承诺 tool_calls 路径
            if tc_delta:
                if not content_started and not tool_calls_started:
                    tool_calls_started = True
                    pre_decision_chunks = []  # 工具调用轮不向前端发任何 chunk
                if tool_calls_started:
                    tool_call_fragments.append(tc_delta)
                continue

            # content delta:v0.2.14 只在非空白时承诺
            if isinstance(content_delta, str) and content_delta:
                content_buf += content_delta  # 始终累积,用于 empty 检测
                if not content_started and not tool_calls_started:
                    if content_delta.strip():
                        # 真正承诺 content 路径:flush pre_decision_chunks + 转发当前 chunk
                        content_started = True
                        for buffered in pre_decision_chunks:
                            yield ("chunk", buffered)
                        pre_decision_chunks = []
                        yield ("chunk", chunk)
                    else:
                        # 空白 content delta(如 "\\n\\n"):尚未承诺,buffer 起来
                        pre_decision_chunks.append(chunk)
                elif content_started:
                    yield ("chunk", chunk)
                # tool_calls_started + 后续 content delta:静默丢弃
                continue

            # role-only / finish-only chunk
            if content_started:
                yield ("chunk", chunk)
            elif not tool_calls_started:
                pre_decision_chunks.append(chunk)
            # tool_calls_started 但本 chunk 没 tc_delta:静默丢弃
    except Exception as exc:  # noqa: BLE001 — 报告给调用方处理
        yield ("done", {
            "path": "upstream_error",
            "error": str(exc),
            "content_buf": content_buf,
            "reasoning_buf": reasoning_buf,
            "tool_call_fragments": tool_call_fragments,
            "finish_reason": finish_reason,
        })
        return

    # 决定最终 path
    assembled: list[dict[str, Any]] = []
    dropped_invalid = 0
    if content_started:
        path = "content"
    elif tool_calls_started:
        # 拼装 + v0.2.15 过滤 args 不合法的"半成品" tool_call。全没了归 empty,
        # 让调用方触发 retry / reasoning 兜底,而不是 dispatch 一堆空 args 调用
        # 然后把 history 喂坏 EAS 触发 400。
        assembled = _assemble_tool_calls_from_deltas(tool_call_fragments)
        assembled, dropped_invalid = _filter_valid_tool_calls(assembled)
        path = "tool_calls" if assembled else "empty"
    else:
        # 既没真 content,也没 tool_calls。可能只有 whitespace content + reasoning。
        path = "empty"

    yield ("done", {
        "path": path,
        "content_buf": content_buf,
        "reasoning_buf": reasoning_buf,
        "tool_call_fragments": tool_call_fragments,
        "assembled_tool_calls": assembled,        # v0.2.15: 已过滤的 tool_calls
        "dropped_invalid_tool_calls": dropped_invalid,  # 供主循环发 progress
        "finish_reason": finish_reason,
    })


def _build_no_thinking_extra(
    extra: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Patch ``extra`` so upstream skips thinking mode on the retry.

    Qwen 各版本认的 key 不一样:新版走 ``chat_template_kwargs.enable_thinking``,
    旧版直接吃 top-level ``enable_thinking``。两边都塞,谁认谁用。
    """
    out = dict(extra or {})
    ct = dict(out.get("chat_template_kwargs") or {})
    ct["enable_thinking"] = False
    out["chat_template_kwargs"] = ct
    out["enable_thinking"] = False
    return out


def _build_iteration_extra(
    cfg: AgentConfig,
    is_last: bool,
    base_extra: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """v0.2.17: 按 cfg 的 thinking 策略,给每轮上游调用注入 enable_thinking 开关。

    intermediate 默认 ``enable_thinking=False``、force_answer 默认开 —— 在每轮
    `_stream_upstream` 之前调,跟 ``_build_no_thinking_extra`` 共享底层 key 形态。
    """
    enabled = (
        cfg.force_answer_thinking_enabled if is_last else cfg.intermediate_thinking_enabled
    )
    if enabled:
        # 上游默认就是 thinking on,直接返回原 extra 即可
        return base_extra
    return _build_no_thinking_extra(base_extra)


def run_agent_stream(
    messages: list[dict[str, Any]],
    cfg: AgentConfig,
    registry: ToolRegistry,
    extra_payload: Optional[dict[str, Any]] = None,
):
    """Generator yielding dict events (each meant to become one SSE 'data:' line).

    v0.2.13 投机式真流式:每轮都用 ``_stream_upstream`` 取上游 SSE chunk,**先**
    buffer 头几块,直到第一个**非空白** content 或 tool_calls 才决定:

    - 看到非空白 content → flush buffer + 持续转发(真流式,前端逐字看到)
    - 看到 tool_calls → 丢弃 buffer,本轮静默拼工具调用,继续 agent 循环
    - 都没等到(只有 reasoning / 只有空白 content)→ **empty 兜底**(v0.2.14)

    v0.2.14 空响应兜底(变种 1/2 from BACKEND_REQUESTS_v0.11):
    - 上游 Qwen thinking 模式有时只输出 reasoning_content,content 为空或仅 ``\\n\\n``
    - 检测到 empty 时,**自动重试一次**带 ``enable_thinking=false`` 的请求
    - 重试也失败 → 用 ``reasoning_buf`` 当兜底答案,加 prefix 提示用户重试

    审计在 v0.2.13 被**弱化**(BACKEND_REQUESTS_v0.11 "真流式 + 弱化审计"):
    - DROP "dig deeper" pushback —— 已流出去的字没法收回
    - DROP ``<tool_call>`` 泄漏剥除 —— 同上
    - KEEP citation 审计,但**只发 ``citation_warn`` progress 事件**,不再追加警告到正文

    强制作答轮(最后一轮)若模型仍然请求工具(tools=[] + 系统提示词通常拦得住),
    走 ``_synthesize_answer`` 非流式合成兜底 —— 这是唯一保留的退化路径。

    事件顺序:
      1. progress / sources 事件(随时穿插,sources 每条带 session 级 ``n``)
      2. 真流式 content delta(role 块 + 多个 content 块 + finish_reason 块)
      3. citation_warn / empty_answer_recovered 事件(如有)
      4. x_adapter_agent_trace 终结块
      5. (调用方负责发 ``[DONE]`` 哨兵)
    """
    trace = AgentTrace()
    tools = registry.schemas()
    augmented = _ensure_system_prompt(_sanitize_history(messages), cfg.system_prompt)
    seen_signatures: set[str] = set()
    model = cfg.model

    # session 级 source 编号:跨多轮 web_search 递增,前端用 `n` 匹配正文 [N]。
    # 用 dict 包裹以便嵌套闭包内 mutate(nonlocal 在闭包链里有时报 SyntaxError)。
    source_n_counter = {"v": 0}
    empty_retries_used = 0

    # Collect progress/sources events via a callback. We can't `yield` from inside
    # a callback (it's not a generator), so we buffer events in a queue and drain
    # at safe boundaries.
    progress_queue: list[dict[str, Any]] = []

    def progress_cb(stage: str, message: str, meta: dict[str, Any]) -> None:
        progress_queue.append(_sse_progress_chunk(model, stage, message, **meta))

    def sources_cb(query: str, items: list[dict[str, Any]]) -> None:
        # 给每条结果分配 session 级递增 n(BACKEND_TODO #1 协议:扁平 array + n 字段),
        # 前端用 url 去重 + 用 n 匹配正文 [N] 引用。query 走 progress.tool_start,这里不重复。
        enriched: list[dict[str, Any]] = []
        for it in items or []:
            source_n_counter["v"] += 1
            enriched.append({**it, "n": source_n_counter["v"]})
        if enriched:
            progress_queue.append(_sse_sources_chunk(model, enriched))

    def _drain_queue():
        while progress_queue:
            yield progress_queue.pop(0)

    def _run_attempt(msgs, current_tools, extra):
        """Forward chunks from one _speculative_iteration to client; capture state.

        Returns the final state dict (path/content_buf/reasoning_buf/...) via
        the special ('state', dict) sentinel. Caller iterates and forwards.
        """
        for kind, payload in _speculative_iteration(cfg, msgs, current_tools, extra):
            if kind == "chunk":
                yield ("chunk", payload)
            elif kind == "done":
                yield ("state", payload)
                return

    for iteration in range(1, cfg.max_iterations + 1):
        is_last = iteration == cfg.max_iterations
        if is_last:
            current_tools: list[dict[str, Any]] = []
            current_messages = augmented + [{"role": "system", "content": FORCE_ANSWER_SYSTEM_HINT}]
            progress_cb("agent_force_answer", "最后一轮，强制模型作答", {"iteration": iteration})
        else:
            current_tools = tools
            current_messages = augmented
            progress_cb("iteration_start", f"开始第 {iteration} 轮模型调用", {"iteration": iteration})

        yield from _drain_queue()

        # Context 预算裁剪:超预算时丢早期 role=tool 消息,避免 force_answer 把
        # 整段 history 喂上游撑爆 EAS 输入上限(实测远低于纸面 256K context)。
        # v0.2.16: force_answer 单独用更紧 budget(默认 max_context * 0.6),给输出
        # 留更多空间 —— 这是最容易撞 400 的轮次。
        budget = cfg.max_context_chars
        if is_last and cfg.force_answer_max_context_chars:
            budget = cfg.force_answer_max_context_chars
        before_chars = _estimate_messages_size(current_messages)
        current_messages = _truncate_messages_for_budget(current_messages, budget)
        after_chars = _estimate_messages_size(current_messages)
        if before_chars != after_chars:
            progress_cb(
                "agent_context_trimmed",
                f"上下文超预算,裁剪早期工具结果({before_chars} → {after_chars} char)",
                {
                    "before": before_chars,
                    "after": after_chars,
                    "budget": budget,
                    "iteration": iteration,
                    "force_answer": is_last,
                },
            )
            yield from _drain_queue()

        # ── 主尝试:投机式真流式 ───────────────────────────────────────
        # v0.2.17: 按 cfg.intermediate_thinking_enabled / force_answer_thinking_enabled
        # 注入 enable_thinking 开关 —— 默认中间迭代关、force_answer 开。
        iter_extra = _build_iteration_extra(cfg, is_last, extra_payload)
        t0 = time.time()
        state: dict[str, Any] = {}
        for kind, payload in _run_attempt(current_messages, current_tools, iter_extra):
            if kind == "chunk":
                yield payload
            else:
                state = payload
        trace.upstream_latencies_ms.append(int((time.time() - t0) * 1000))
        trace.iterations = iteration

        # upstream_error 短路
        if state.get("path") == "upstream_error":
            yield _sse_progress_chunk(
                model, "agent_error",
                f"upstream error: {state.get('error', '')}",
                iteration=iteration,
            )
            trace.stopped_reason = "upstream_error"
            yield _sse_trace_chunk(model, trace)
            return

        # v0.2.15: 提前发 filter progress 事件 —— 不管最终 path 是 tool_calls
        # 还是 empty(全过滤光),前端都该知道"有一些 tool_call 被丢了"
        if state.get("dropped_invalid_tool_calls", 0) > 0:
            progress_cb(
                "agent_tool_calls_filtered",
                f"丢弃 {state['dropped_invalid_tool_calls']} 个 args 不合法的工具调用(上游真流式拼装失败)",
                {
                    "dropped": state["dropped_invalid_tool_calls"],
                    "remaining": len(state.get("assembled_tool_calls") or []),
                    "iteration": iteration,
                },
            )
            yield from _drain_queue()

        # ── v0.2.14 空响应识别 + 禁 thinking 重试 ──────────────────────
        # Qwen thinking 模式有时只发 reasoning,content 全空或仅 \n\n(BACKEND_REQUESTS
        # variant 1)。或 finish_reason=tool_calls 但 fragments 拼不出工具调用
        # (variant 2)。两者都归入 path='empty',这里做一次 enable_thinking=false 的
        # 重试 —— 大多数情况下能拿回真 content 或真 tool_calls。
        if state.get("path") == "empty" and empty_retries_used < cfg.max_empty_retries:
            empty_retries_used += 1
            progress_cb(
                "agent_empty_answer_retry",
                "上游本轮仅输出思考过程,以禁用 thinking 重试",
                {
                    "iteration": iteration,
                    "reasoning_chars": len(state.get("reasoning_buf", "")),
                    "finish_reason": state.get("finish_reason", ""),
                },
            )
            yield from _drain_queue()
            retry_extra = _build_no_thinking_extra(extra_payload)
            t_retry = time.time()
            for kind, payload in _run_attempt(current_messages, current_tools, retry_extra):
                if kind == "chunk":
                    yield payload
                else:
                    # 保留 reasoning_buf:retry 也可能仅给 reasoning,合并最新的
                    prev_reasoning = state.get("reasoning_buf", "")
                    state = payload
                    if not state.get("reasoning_buf"):
                        state["reasoning_buf"] = prev_reasoning
            trace.upstream_latencies_ms.append(int((time.time() - t_retry) * 1000))

            if state.get("path") == "upstream_error":
                yield _sse_progress_chunk(
                    model, "agent_error",
                    f"empty-answer retry upstream error: {state.get('error', '')}",
                    iteration=iteration,
                )
                trace.stopped_reason = "upstream_error"
                yield _sse_trace_chunk(model, trace)
                return

        path = state.get("path")

        # ── 真流式正文路径:弱化审计 + 终结 ─────────────────────────────
        if path == "content":
            content_buf = state.get("content_buf", "")
            finish_reason = state.get("finish_reason", "")
            trace.final_finish_reason = finish_reason or "stop"
            trace.answer_truncated = finish_reason == "length"
            trace.stopped_reason = trace.stopped_reason or (
                "answered_forced" if is_last else "answered_streamed"
            )
            # KEEP citation audit —— 只发警告事件,不改正文(正文已流出)
            if cfg.citation_guard and content_buf:
                _audit_final_citations(content_buf, trace)
                if trace.unverified_urls_in_answer:
                    yield _sse_progress_chunk(
                        model,
                        "citation_warn",
                        "检测到正文 URL 未被工具访问过(可能编造)",
                        unverified=trace.unverified_urls_in_answer,
                    )
            yield _sse_trace_chunk(model, trace)
            return

        # ── tool_calls 路径:dispatch + 进入下一轮 ────────────────────────
        if path == "tool_calls":
            # v0.2.15: _speculative_iteration 已经过滤过 args 不合法的工具调用,
            # 这里直接用 state["assembled_tool_calls"]。
            assembled = state.get("assembled_tool_calls") or []
            # 双保险:retry path 合并状态时可能丢失 assembled,fallback 拼一次
            if not assembled:
                assembled = _assemble_tool_calls_from_deltas(
                    state.get("tool_call_fragments", [])
                )
                assembled, _ = _filter_valid_tool_calls(assembled)
            if not assembled:
                path = "empty"

            elif is_last:
                # 强制作答轮模型仍请求工具:走合成兜底
                progress_cb(
                    "agent_force_answer_fallback",
                    "强制作答轮仍请求工具,退化到合成兜底",
                    {"iteration": iteration},
                )
                yield from _drain_queue()
                synthesized = _synthesize_answer(cfg, augmented, extra_payload, trace)
                if not synthesized:
                    synthesized = (
                        "（抱歉，本轮未能整理出最终答案，检索过程可能未收敛。请重试或换一种问法。）"
                    )
                    trace.stopped_reason = "answered_empty_fallback"
                else:
                    trace.stopped_reason = "answered_synthesized"
                trace.final_finish_reason = "stop"
                fake_resp = {"id": "agent-final-synth", "created": int(time.time())}
                yield from _stream_final_answer(model, synthesized, fake_resp)
                yield _sse_trace_chunk(model, trace)
                return

            else:
                # 正常 dispatch
                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": assembled,
                }
                progress_cb(
                    "tools_dispatch",
                    f"模型请求 {len(assembled)} 个工具调用",
                    {"count": len(assembled), "iteration": iteration},
                )
                yield from _drain_queue()
                tool_messages = _dispatch_tool_calls_parallel(
                    assembled, registry, cfg, progress_cb, trace, seen_signatures,
                    sources_cb=sources_cb,
                )
                yield from _drain_queue()
                augmented = augmented + [assistant_message] + tool_messages
                continue

        # ── 空响应兜底:重试已用完或仍空 → reasoning 当答案 ───────────────
        if path == "empty":
            reasoning_buf = state.get("reasoning_buf", "") or ""
            finish_reason = state.get("finish_reason", "")
            # is_last 时优先走合成兜底(已有 system prompt + 工具结果摘要,更可能产出正经答案)
            if is_last:
                progress_cb(
                    "agent_force_answer_fallback",
                    f"强制作答轮上游空响应(finish={finish_reason or 'unknown'}),走合成兜底",
                    {
                        "iteration": iteration,
                        "reasoning_chars": len(reasoning_buf),
                    },
                )
                yield from _drain_queue()
                synthesized = _synthesize_answer(cfg, augmented, extra_payload, trace)
                if synthesized:
                    trace.stopped_reason = "answered_synthesized"
                    payload_text = synthesized
                elif reasoning_buf.strip():
                    trace.stopped_reason = "answered_from_reasoning"
                    payload_text = (
                        "（以下是模型的思考过程作为兜底答案，可能不是最终结论；"
                        "建议点「重新生成」再试一次。）\n\n"
                        + reasoning_buf.strip()
                    )
                else:
                    trace.stopped_reason = "answered_empty_fallback"
                    payload_text = (
                        "（抱歉，本轮模型未输出有效内容，请重新生成或换一种问法。）"
                    )
                trace.final_finish_reason = "stop"
                fake_resp = {"id": "agent-final-empty", "created": int(time.time())}
                yield from _stream_final_answer(model, payload_text, fake_resp)
                yield _sse_trace_chunk(model, trace)
                return

            # 非最后一轮的 empty:reasoning 兜底 + 结束(不强行进入下一轮 —— 模型已经
            # 表态没东西可输出,继续循环只会重复同样的空响应)
            if reasoning_buf.strip():
                progress_cb(
                    "agent_empty_answer_recovered",
                    "上游本轮 content 为空,展示思考过程作为兜底答案",
                    {
                        "iteration": iteration,
                        "reasoning_chars": len(reasoning_buf),
                        "finish_reason": finish_reason,
                    },
                )
                yield from _drain_queue()
                trace.stopped_reason = "answered_from_reasoning"
                trace.final_finish_reason = "stop"
                payload_text = (
                    "（以下是模型的思考过程作为兜底答案，可能不是最终结论；"
                    "建议点「重新生成」再试一次。）\n\n"
                    + reasoning_buf.strip()
                )
                fake_resp = {"id": "agent-final-reasoning", "created": int(time.time())}
                yield from _stream_final_answer(model, payload_text, fake_resp)
                yield _sse_trace_chunk(model, trace)
                return

            # 连 reasoning 都没有 —— 最终兜底
            progress_cb(
                "agent_empty_answer_recovered",
                f"上游本轮完全空响应(finish={finish_reason or 'unknown'}),使用固定兜底文案",
                {"iteration": iteration, "finish_reason": finish_reason},
            )
            yield from _drain_queue()
            trace.stopped_reason = "answered_empty_fallback"
            trace.final_finish_reason = "stop"
            fake_resp = {"id": "agent-final-empty", "created": int(time.time())}
            yield from _stream_final_answer(
                model,
                "（抱歉，本轮模型未输出有效内容，请重新生成或换一种问法。）",
                fake_resp,
            )
            yield _sse_trace_chunk(model, trace)
            return

        # 防御性:未知 path
        trace.stopped_reason = "no_choices"
        yield _sse_progress_chunk(model, "agent_warn", f"unexpected path: {path}")
        yield _sse_trace_chunk(model, trace)
        return


# Note: v0.2.14 在 v0.2.13 投机式真流式基础上加了空响应识别 + 兜底 ——
# 1. content_started 只在非空白 content delta 时才承诺(避免 \n\n 把路径锁死)
# 2. 流结束后若仍未承诺,标记 path=empty,自动重试一次禁 thinking 的请求
# 3. 重试也失败就用 reasoning_buf 当兜底答案,加 prefix 提示用户重试
# 4. 强制作答轮的 empty 优先走 _synthesize_answer 合成,失败才退到 reasoning
