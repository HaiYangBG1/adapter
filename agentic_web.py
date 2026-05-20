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


# =============================================================================
# System prompt — proven necessary by Phase 0
# =============================================================================

DEFAULT_SYSTEM_PROMPT_TEMPLATE = (
    "你是一个具备联网检索 + 视觉读图能力的助手。\n"
    "\n"
    "【当前时间】{current_date}（{current_weekday}）。\n"
    "你的训练语料截止日期早于这一天，因此对**任何涉及具体日期、版本号、"
    "公司动态、产品发布、官方信息**的问题，你的训练记忆很可能已经过时。\n"
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
    max_iterations: int = 5          # hard cap on agent turns
    max_fetches: int = 8             # cap on web_fetch + web_view calls per session
    max_searches: int = 8            # cap on web_search calls per session


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
    # Hotfix P0-2: citation guard
    verified_urls: set[str] = field(default_factory=set)  # URLs actually touched by tools this session
    unverified_urls_in_answer: list[str] = field(default_factory=list)  # URLs in final content that weren't touched
    # Phase 4 P2: observability
    final_finish_reason: str = ""  # upstream finish_reason of the final answer ("stop" / "length" / ...)
    answer_truncated: bool = False  # True when final_finish_reason == "length"


ProgressCallback = Callable[[str, str, dict[str, Any]], None]
# stage, message, meta


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
            if key in {"messages", "tools", "tool_choice", "stream", "model"}:
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


def _annotate_final_message(message: dict[str, Any], trace: AgentTrace) -> None:
    """Post-process the assistant's final message: leak cleanup + citation audit.

    The two passes are idempotent and only mutate ``message['content']`` when
    something needs fixing.
    """
    content = message.get("content")
    if not isinstance(content, str):
        return
    # 1) Leak cleanup (Phase 2 carry-over)
    cleaned, removed = _strip_tool_call_leaks(content)
    if removed:
        content = cleaned
        trace.tool_call_leaks_stripped += removed
    # 2) Citation audit (Hotfix P0-2)
    _audit_final_citations(content, trace)
    content = content + _citation_warning_text(trace.unverified_urls_in_answer)
    message["content"] = content


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
            trace.stopped_reason = "answered"
            trace.final_finish_reason = str(choice.get("finish_reason") or "")
            trace.answer_truncated = trace.final_finish_reason == "length"
            msg = choice.get("message", {}) or {}
            _annotate_final_message(msg, trace)
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
            "verified_urls": sorted(trace.verified_urls),
            "unverified_urls_in_answer": trace.unverified_urls_in_answer,
            "final_finish_reason": trace.final_finish_reason,
            "answer_truncated": trace.answer_truncated,
            "elapsed_total_ms": int((time.time() - trace.started_at) * 1000),
        },
    }


def run_agent_stream(
    messages: list[dict[str, Any]],
    cfg: AgentConfig,
    registry: ToolRegistry,
    extra_payload: Optional[dict[str, Any]] = None,
):
    """Generator yielding dict events (each meant to become one SSE 'data:' line).

    Event ordering:
      1. Zero or more progress chunks (x_adapter_agent_progress) per iteration
      2. The actual streamed completion chunks from the FINAL upstream call
         (these have a populated choices[0].delta with text)
      3. A final x_adapter_agent_trace chunk
      4. (Caller is responsible for emitting the terminal "[DONE]" sentinel)

    The caller (HTTP handler) is responsible for writing each event as
    'data: <json>\\n\\n' to the wire.
    """
    trace = AgentTrace()
    tools = registry.schemas()
    augmented = _ensure_system_prompt(_sanitize_history(messages), cfg.system_prompt)
    seen_signatures: set[str] = set()
    model = cfg.model

    # Collect progress events via a callback that yields back through this generator.
    # We can't `yield` from inside a callback, so we buffer events.
    progress_queue: list[dict[str, Any]] = []

    def progress_cb(stage: str, message: str, meta: dict[str, Any]) -> None:
        progress_queue.append(_sse_progress_chunk(model, stage, message, **meta))

    def _drain_queue():
        while progress_queue:
            yield progress_queue.pop(0)

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

        # On the final (forced) iteration the model can leak <tool_call> markup
        # as raw text because the parser is bypassed. To handle it safely we
        # do a non-streaming call here, sanitize, and emit the cleaned content
        # as a single chunk. Streaming UX is only sacrificed on this fallback
        # path; the common "model answered earlier" path still streams (see the
        # _is_final_message branch below).
        if is_last:
            t_last = time.time()
            try:
                resp = _call_upstream(cfg, current_messages, current_tools, extra_payload)
            except Exception as exc:  # noqa: BLE001
                yield _sse_progress_chunk(model, "agent_error", f"upstream error: {exc}", iteration=iteration)
                trace.stopped_reason = "upstream_error"
                yield _sse_trace_chunk(model, trace)
                return
            trace.upstream_latencies_ms.append(int((time.time() - t_last) * 1000))
            trace.iterations = iteration
            choices = resp.get("choices", [])
            if not choices:
                trace.stopped_reason = "no_choices"
                yield _sse_trace_chunk(model, trace)
                return
            choice = choices[0]
            trace.final_finish_reason = str(choice.get("finish_reason") or "")
            trace.answer_truncated = trace.final_finish_reason == "length"
            msg = choice.get("message", {}) or {}
            _annotate_final_message(msg, trace)
            cleaned = msg.get("content") or ""
            trace.stopped_reason = trace.stopped_reason or "answered_forced"
            yield {
                "id": resp.get("id", "agent-final"),
                "object": "chat.completion.chunk",
                "created": resp.get("created", int(time.time())),
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": cleaned}, "finish_reason": None}
                ],
            }
            yield {
                "id": resp.get("id", "agent-final"),
                "object": "chat.completion.chunk",
                "created": resp.get("created", int(time.time())),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": resp.get("usage"),
            }
            yield _sse_trace_chunk(model, trace)
            return

        # Intermediate iterations: stream the upstream call so the model's
        # answer (when it answers) reaches the client token-by-token. When the
        # turn is a tool-call turn instead, no content is produced (Qwen3-VL
        # emits either content OR tool_calls, never both), so forwarding
        # content deltas live is safe.
        t0 = time.time()
        streamed_content = ""
        tool_calls: list[dict[str, Any]] = []
        finish_reason = ""
        try:
            for item in _stream_upstream_iteration(
                cfg, current_messages, current_tools, extra_payload
            ):
                if item[0] == "content":
                    yield item[1]  # forward token delta to client immediately
                else:  # ("final", content, tool_calls, finish_reason)
                    streamed_content, tool_calls, finish_reason = item[1], item[2], item[3]
        except Exception as exc:  # noqa: BLE001
            yield _sse_progress_chunk(model, "agent_error", f"upstream error: {exc}", iteration=iteration)
            trace.stopped_reason = "upstream_error"
            yield _sse_trace_chunk(model, trace)
            return
        trace.upstream_latencies_ms.append(int((time.time() - t0) * 1000))
        trace.iterations = iteration

        if finish_reason != "tool_calls" or not tool_calls:
            # Model answered directly — content already streamed live above.
            trace.stopped_reason = "answered"
            trace.final_finish_reason = finish_reason
            trace.answer_truncated = finish_reason == "length"
            # Citation audit: content was already sent, so we can't edit it —
            # surface any warning as a trailing content chunk instead.
            _audit_final_citations(streamed_content, trace)
            warning = _citation_warning_text(trace.unverified_urls_in_answer)
            if warning:
                yield {
                    "id": "agent-citation-warn",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": warning}, "finish_reason": None}],
                }
            yield {
                "id": "agent-final",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield _sse_trace_chunk(model, trace)
            return

        # Tool calls present — dispatch and continue
        message = {
            "role": "assistant",
            "content": streamed_content or None,
            "tool_calls": tool_calls,
        }
        progress_cb(
            "tools_dispatch",
            f"模型请求 {len(tool_calls)} 个工具调用",
            {"count": len(tool_calls), "iteration": iteration},
        )
        yield from _drain_queue()
        tool_messages = _dispatch_tool_calls_parallel(
            tool_calls, registry, cfg, progress_cb, trace, seen_signatures
        )
        yield from _drain_queue()
        augmented = augmented + [message] + tool_messages


def _stream_upstream_iteration(
    cfg: AgentConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    extra: Optional[dict[str, Any]],
):
    """Generator over one streaming upstream call (Phase 4 P3 — true streaming).

    Yields:
      ("content", <openai chunk dict>)  — for each delta carrying text content
      ("final", content_str, tool_calls, finish_reason)  — once, at the end

    Streaming tool_calls arrive fragmented across chunks (the ``arguments``
    string is split); we accumulate them by ``index`` and reassemble.
    """
    req = _build_upstream_request(cfg, messages, tools, extra, stream=True)
    content_parts: list[str] = []
    tc_acc: dict[int, dict[str, Any]] = {}
    finish_reason = ""
    with urllib.request.urlopen(req, timeout=cfg.request_timeout) as resp:
        for raw_line in resp:
            line = raw_line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                chunk = json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]
            delta = ch.get("delta") or {}
            text = delta.get("content")
            if text:
                content_parts.append(text)
                yield ("content", chunk)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tc_acc.setdefault(
                    idx,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
    tool_calls = [tc_acc[i] for i in sorted(tc_acc)]
    yield ("final", "".join(content_parts), tool_calls, finish_reason)
