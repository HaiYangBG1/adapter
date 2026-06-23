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
import uuid  # v0.5.0 B:artifact id 生成(generating→ready 同 id 三态)
import queue  # v0.4.0 D 重构:plan 执行 generator 用 Queue 收集 worker 完成事件
import threading  # v0.4.0 D 重构:plan 执行 worker 用 daemon thread,主 generator 退出后自然 GC
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
# v0.3.0 Plan-and-Execute(D 方案 Phase 1):带数据集的 Excel agent 第一轮
# 强制 emit 这个工具,LLM 一次性提交完整执行计划,adapter 按 depends_on 拓扑
# 排序 + 分批并行 dispatch excel_query,最后一轮综合作答。
# 详见 lxj-adapter-deploy/design/2026-05-28-D-plan-and-execute-architecture.md
SUBMIT_ANALYSIS_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_analysis_plan",
        "description": (
            "提交本次分析的**完整**执行计划。把回答用户问题所需的所有 excel_query "
            "步骤一次性列出;系统会按 depends_on 拓扑排序、并行 dispatch,然后让你"
            "基于全部结果综合作答。\n"
            "\n"
            "**关键约束**:\n"
            "- 本轮**只有这一个工具可用**,你必须 emit 这个 tool_call,不能直接调 excel_query。\n"
            "- plan 提交后**没有机会追加 step**(本轮预算用完了)—— 一次性把要查的都列上,"
            "拿不准的边缘指标也可以列上。\n"
            "- 大多数 step 的 `depends_on` 应该是**空数组**(并行最快);"
            "只有'B 需要看 A 的结果才能写出 question'时才填依赖。\n"
            "- 单个 step 的 `question` 内部可以含多个相关指标(CTE / 多 JOIN / window 一段 SQL "
            "算完);**不同维度 / 不同实体粒度** 才拆 step。\n"
            "- step 上限 12 —— 超了说明拆得太碎,合并相关 step。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "执行步骤列表(1-12 个)。",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": (
                                    "步骤编号,供 depends_on 引用。"
                                    "推荐:step_1 / q_revenue / by_region 之类。"
                                ),
                            },
                            "question": {
                                "type": "string",
                                "description": (
                                    "传给 excel_query 的自然语言指令(同你以前调 "
                                    "excel_query 的 question 参数)。后端会自动写 SQL "
                                    "执行。例:「按区域分组,统计总销售额+总门店数+单店均值,"
                                    "按单店均值降序排前 10」是**一个** question。"
                                ),
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "依赖的前置步骤 id 列表(填其他 step 的 id)。"
                                    "本步会等所有 depends_on 完成后再执行。"
                                    "**绝大多数情况下应该是空数组**(并行执行)。"
                                ),
                            },
                            "rationale": {
                                "type": "string",
                                "description": (
                                    "(可选)为什么需要这一步、它回答用户问题的哪部分。"
                                    "便于日志和回溯,不影响执行。"
                                ),
                            },
                        },
                        "required": ["id", "question"],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "(可选)对整个 plan 的一句话总结,便于前端 UI 展示。"
                        "例:'按门店类型对比 5 个核心经营指标(共 4 步并行)'。"
                    ),
                },
            },
            "required": ["steps"],
        },
    },
}

# v0.5.0 B(文件生成 MVP):PPTX 确定性生成工具。enable_file_gen=True 时由
# _build_agent_registry 用 register_schema_only 挂上(inline 拦截,不走 dispatch),
# 显式 PPTX 模式下首轮 force_first_tool_name 强制模型 emit 完整大纲。模型**只产出
# 结构化大纲数据**(A 铁律:不写渲染代码),adapter 注入的 file_renderer 用写死的
# python-pptx 模板把大纲 → 真 .pptx → 落对象存储 → emit x_adapter_artifact。
GENERATE_PPTX_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_pptx",
        "description": (
            "把一份**完整的演示文稿大纲**提交给确定性渲染器,生成可下载的 .pptx 文件。\n"
            "\n"
            "**关键约束**:\n"
            "- 本轮**只有这一个工具可用**,你必须 emit 这个 tool_call,不能用自然语言描述大纲。\n"
            "- 一次性把**所有**幻灯片列全(标题页之后的每一页);提交后没有追加机会。\n"
            "- 每页 `bullets` 写**要点短句**(不是整段文字),3-6 条为宜;一页一个主题。\n"
            "- 你只负责**内容大纲**;版式 / 配色 / 字体由渲染器统一控制,不要在文本里写样式。\n"
            "- 建议 6-15 页(含封面);超过 30 页会被截断。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "演示文稿主标题(出现在封面页),如「2024 Q4 营收分析」。",
                },
                "subtitle": {
                    "type": "string",
                    "description": "(可选)副标题 / 作者 / 日期,出现在封面标题下方。",
                },
                "slides": {
                    "type": "array",
                    "description": "正文幻灯片列表(封面之后;建议 5-14 页)。",
                    "minItems": 1,
                    "maxItems": 30,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "本页标题(一行短语)。",
                            },
                            "bullets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "本页要点列表(3-6 条短句)。每条是一个独立要点,"
                                    "不要写成整段;不含编号 / 项目符号(渲染器自动加)。"
                                ),
                            },
                            "notes": {
                                "type": "string",
                                "description": "(可选)演讲者备注,放进 pptx 备注栏,不显示在幻灯片上。",
                            },
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["title", "slides"],
        },
    },
}


# v0.6.0 B+(多类型扩展):Excel 工作簿确定性生成工具。模型只产出**结构化表格数据**
# (A 铁律:不写渲染代码),adapter 注入的 file_renderer 用写死的 openpyxl 模板渲染。
GENERATE_XLSX_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_xlsx",
        "description": (
            "把**结构化的表格数据**提交给确定性渲染器,生成可下载的 Excel (.xlsx) 文件。\n"
            "\n"
            "**关键约束**:\n"
            "- 你只负责**数据本身**(列名 + 每行的值);样式 / 配色 / 列宽由渲染器统一控制。\n"
            "- `rows` 里的数字写成**数值**(如 1240.5、47),不要加千分位逗号或单位符号 ——"
            "把单位写进列名(如「销售额(万)」)。这样表格才能排序 / 求和。\n"
            "- 可以多个工作表(`sheets`),每个表是一组 `columns` + `rows`。\n"
            "- 不要在单元格里写公式或 markdown;就是纯数据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "工作簿主题(用作下载文件名),如「2024 区域经营」。",
                },
                "sheets": {
                    "type": "array",
                    "description": "工作表列表(至少 1 个;单表场景给 1 个即可)。",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "工作表标签名(简短),如「区域汇总」。"},
                            "columns": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "表头列名(一行)。单位写进列名,如「销售额(万)」。",
                            },
                            "rows": {
                                "type": "array",
                                "description": "数据行;每行是一个与 columns 等长的数组,数字写成数值。",
                                "items": {"type": "array", "items": {}},
                            },
                        },
                        "required": ["columns", "rows"],
                    },
                },
            },
            "required": ["title", "sheets"],
        },
    },
}


# v0.6.0 B+:CSV 表格确定性生成工具(单表纯数据,UTF-8 BOM 便于 Excel 打开)。
GENERATE_CSV_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_csv",
        "description": (
            "把**一张表格的数据**提交给渲染器,生成可下载的 CSV (.csv) 文件。\n"
            "适合简单的单表数据导出;需要多表 / 带格式时用 generate_xlsx。\n"
            "数字写成数值;单位写进列名;不要 markdown。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "表格主题(用作文件名),如「Q4 区域销售」。"},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "表头列名(一行)。",
                },
                "rows": {
                    "type": "array",
                    "description": "数据行;每行是一个与 columns 等长的数组。",
                    "items": {"type": "array", "items": {}},
                },
            },
            "required": ["title", "columns", "rows"],
        },
    },
}


# v0.6.0 B+:Word 文档确定性生成工具。模型出**结构化文档大纲**(标题 + 小节),
# 渲染由写死的 python-docx 模板完成(A 铁律:模型不写排版代码)。
GENERATE_DOCX_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_docx",
        "description": (
            "把一份**结构化的文档**提交给确定性渲染器,生成可下载的 Word (.docx) 文件。\n"
            "\n"
            "**关键约束**:\n"
            "- 你负责**内容结构**:文档标题、若干小节(每节一个 `heading` + 正文 `paragraphs` 和/或要点 `bullets`)。\n"
            "- 排版 / 字体 / 标题样式 / 项目符号由渲染器统一控制 —— 不要在文本里写 markdown(#、*、- 等)。\n"
            "- `paragraphs` 写成段(完整句子);`bullets` 写要点短句。两者按需,可只用其一。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "文档主标题,如「Q4 营收复盘报告」。"},
                "subtitle": {"type": "string", "description": "(可选)副标题 / 作者 / 日期。"},
                "sections": {
                    "type": "array",
                    "description": "文档小节列表。",
                    "minItems": 1,
                    "maxItems": 60,
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string", "description": "小节标题(一行短语)。"},
                            "paragraphs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "(可选)本节正文段落,每条是完整一段。",
                            },
                            "bullets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "(可选)本节要点列表,每条一行短句。",
                            },
                        },
                        "required": ["heading"],
                    },
                },
            },
            "required": ["title", "sections"],
        },
    },
}


# v0.6.0 B+ / v0.6.3(2026-06-23 PM 拍「方案2 自由写 HTML + 卡片一致」):HTML 网页生成工具。
# 🔑 **模型只填短 brief(需求描述)**,**不在工具参数里写整段 HTML** —— 实测模型在长 string
# 工具参数里严重偷懒(写空壳),而填短结构化参数 + 自由写正文都正常。adapter 拦截到本工具后
# **单独发一个自由生成调用**(_call_upstream_html_builder)让模型自由写出完整含 chart.js 的
# HTML(自由写=模型强项),再走 PPT 同款 OSS+文件卡流程。**允许 <script>/图表库**(下载件
# file:// 隔离 origin,见 html_generator 模块注释 + decisions 2026-06-23 爆炸半径界定)。
GENERATE_HTML_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_html",
        "description": (
            "生成一个可下载的网页 (.html)。用户要「网页 / 可视化 / 看板 / dashboard / 图表展示」时用。\n"
            "\n"
            "你**只需在 `brief` 里用一段话讲清楚要生成什么**(主题、要展示的内容/数据、想要的图表类型),"
            "系统会据此生成完整的、带交互图表(chart.js)的网页文件。\n"
            "**不要在这里写 HTML 代码** —— 只写需求描述;把用户给的具体数据写进 brief。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "网页标题(用作下载文件名)。"},
                "brief": {
                    "type": "string",
                    "description": (
                        "网页需求的**一段话描述**:主题、要展示的具体内容 / 数据、想要的图表类型"
                        "(柱状图 / 折线图 / 饼图等)。把用户给的数据原样写进来。例:「展示某班语文88、"
                        "数学95、英语76 三科平均分,用柱状图对比,配一段简短点评」。**不要写 HTML 代码。**"
                    ),
                },
            },
            "required": ["title", "brief"],
        },
    },
}


# v0.6.0 B+:file_gen 自动模式下挂的全部生成工具(模型自决类型,tool_choice=auto)。
ALL_FILE_GEN_TOOLS: list[dict[str, Any]] = [
    GENERATE_PPTX_TOOL,
    GENERATE_XLSX_TOOL,
    GENERATE_DOCX_TOOL,
    GENERATE_CSV_TOOL,
    GENERATE_HTML_TOOL,
]

# tool name → artifact 元信息。拦截分支据此在渲染前 emit「生成中」占位卡(kind/mime/
# previewKind 在渲染前就要知道);渲染器返回的 mime/name/size 为最终权威值。保持
# generic / open-source safe(只有标准 MIME / 中性默认名,无内部标识)。
_OOXML = "application/vnd.openxmlformats-officedocument"
FILE_GEN_TOOL_META: dict[str, dict[str, str]] = {
    "generate_pptx": {"kind": "pptx", "ext": "pptx", "mime": f"{_OOXML}.presentationml.presentation", "default_name": "演示文稿", "preview": "office"},
    "generate_xlsx": {"kind": "xlsx", "ext": "xlsx", "mime": f"{_OOXML}.spreadsheetml.sheet", "default_name": "工作簿", "preview": "office"},
    "generate_docx": {"kind": "docx", "ext": "docx", "mime": f"{_OOXML}.wordprocessingml.document", "default_name": "文档", "preview": "office"},
    "generate_csv": {"kind": "csv", "ext": "csv", "mime": "text/csv; charset=utf-8", "default_name": "数据表", "preview": "none"},
    "generate_html": {"kind": "html", "ext": "html", "mime": "text/html; charset=utf-8", "default_name": "网页", "preview": "none"},
}
FILE_GEN_TOOL_NAMES: frozenset[str] = frozenset(FILE_GEN_TOOL_META)


EXCEL_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "excel_query",
        "description": (
            "对用户已上传的表格数据集做精确计算与统计 —— 求和、计数、分组、"
            "排名、占比、多表关联对比等。传入一个用自然语言描述的、要计算的"
            "具体子问题;工具内部会写 DuckDB SQL 在沙箱中执行,返回真实的计算"
            "结果(含所用 SQL)。\n"
            "\n"
            "**重要 — 工具能力比你想的更强**:每次 question 在内部生成的 SQL "
            "**可以含多个 CTE、跨多表 JOIN、window function、子查询等**,所以"
            "**一个 question 可以一次性算出多个相关维度**,不需要每个维度发"
            "一次 question。例:「按区域分组,统计总销售额、总门店数、单店"
            "均值,并按单店均值降序排列前 10」就是**一个 question**(内部一段 "
            "SQL 一次算完),不是三个。\n"
            "\n"
            "**支持并行调用**:本轮如果要算多个**互不依赖**的维度(比如"
            "「按区域分组」和「按教练分组」是两个独立分析),应当在**同一轮"
            "内并行 emit 多个 excel_query 调用**(parallel_tool_calls 已开启),"
            "而不是一次只发一个、看完结果再发下一个。"
            "\n"
            "凡涉及具体数字、统计口径、排名或对比,**必须**调用本工具取真实"
            "结果,不要凭记忆、看图估算或心算。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "要计算的一个完整子问题,自然语言描述。可以含多个相关"
                        "口径(只要它们能用一段 SQL / 多个 CTE 一起算)。"
                        "例:「按城市分组,统计每个城市的门店数量、平均日均"
                        "销售额、销售额标准差,按门店数量降序排列前 20」—— "
                        "这是**一个** question(三个相关指标 + 一个排序,一段"
                        "SQL 算完),不要拆三次发。"
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
2. 面对宽泛的请求(如「帮我详细分析一下」「列出所有 X 并对比优劣」)或概念/定义类问题(如「X 指标的口径规则是什么」「Y 字段什么意思」),**第一步永远是 emit excel_query 调用**去探查表结构 —— 数据集里可能有「指标说明」「字段定义」「口径」等元数据 sheet,或者列名/列样本本身就能回答口径问题。**严禁**在没调工具的情况下直接说「需要业务文档」「需要看具体定义」「这通常是某某业务系统的自定义指标」之类的"客气拒答"——数据集就在工具后面,先去查。

   **探完表的下一个 message:立即在同一 message 内并行 emit 多个 excel_query。绝对禁止只说计划不 emit。**(关键 —— v0.2.29 修过的严重 bug):
   - **要做的 action**:消息的 assistant.tool_calls 数组里**同时塞多个** excel_query 调用对象(adapter 已开 parallel_tool_calls),例如:
     `tool_calls: [{name:"excel_query", args:{question:"按区域分组算总销售额+门店数+单店均值排序前10"}}, {name:"excel_query", args:{question:"按教练分组算人均产出+门店数+排序前20"}}, ...]`
   - **绝对禁止的 action**:在 content 文本里写「现在我将并行查询...」「我打算分析以下 N 个维度...」「接下来我会做...」**然后就停了什么都不做**。这是最严重的失败模式 —— 你只讲计划没干活,等于浪费用户一轮等待。
   - 哪怕你脑子里有规划,**思路放进 reasoning_content / thinking block**,正文(content)严格只能是两种之一:① 空字符串 + 多个 tool_calls(消息只为发工具调用);② 给最终答案(所有 query 跑完后)。不允许第三种:content 里说计划 + 没 tool_calls。

   **错误 vs 正确示范**:
     错(纯串行 / 只说不做,浪费 5 轮 budget):
       第 1 轮: 探表 → 第 2 轮: 「现在我将并行查询...」(无 tool_calls,任务断)
     对(并行执行,2-3 轮完成):
       第 1 轮: 探表(单 query)
       第 2 轮: **同 message 内** emit 4-6 个独立 excel_query(content="",tool_calls=[...])
       第 3 轮: 拿到所有结果 → 综合作答(content=结论,tool_calls=null)

   **判断标准**:几个分析维度如果**互不依赖**(B 不需要 A 的结果),就**必须并行发**;只有"B 需要 A 的结果才能继续"才串行。

   单个 excel_query 内部的 SQL 可以含多个 CTE / 多 JOIN / window function,**一个 question 能算多个相关指标**(详见 excel_query 工具描述的"重要"段)。例:「按区域算总销售额 + 总门店数 + 单店均值 + 排序前 10」是**一个 question**,不是 3 个。

   查询要抓重点:优先分析最关键的几个维度,不要把每一列都查一遍。给最终结论时,综合已查到的全部结果作结构化总结,**禁止**在结尾宣告「接下来我将查询…」这类未完成的动作 —— 要么把它查了,要么就此收尾。

3. 单个 excel_query question 写得**尽量"宽" —— 把可以一段 SQL 一起算的多个相关指标合到一个 question 里**(见上)。但**不同维度 / 不同实体粒度 / 不同数据子集**之间不要硬塞,该并行多发就并行多发。
4. 禁止声称调用了某工具而实际没有调用 —— 回答只能如实反映你真正执行过的操作。
5. 若 excel_query 的结果表明数据集中没有所需的列 / 维度 / 口径,如实说明「数据集中没有这项数据,无法据此分析」并指出需补充什么,不得用其他列硬凑。
6. 本次分析的数据来自 excel_query 工具(不是网页)—— 回答中**不要**编造 URL、**不要**追加「Sources」/「参考来源」章节、**不要**用 [1][2] 之类的引用编号。需要说明出处时,直接写「据 excel_query 查询」即可。
7. **工具调用必须用结构化协议,不要用自然语言描述。** 需要调用工具时,直接 emit 标准的 tool_calls JSON 结构;**严禁**把「让我调用 excel_query…」「我将查询…」「请允许我执行…」「下面我来调用工具」这类**叙述句**当作回答内容输出。这种叙述对用户无意义、又不会真的触发工具调用,会让用户看到"思考中"卡住几十秒后无果。要么直接 emit tool_calls,要么直接给最终答案,二者必居其一,**不要写过渡叙述**。"""


# v0.3.0 D 方案 Phase 1:Plan-and-Execute 模式的 Excel agent system prompt。
# 跟 EXCEL_AGENT_SYSTEM_PROMPT(旧 iterative loop 模式)互斥,通过 cfg.system_prompt
# 切换。设计目标:消除 LLM 多轮决策的 brittle 累积问题(单轮 80% × 5 轮 = 33%
# → 1 plan + 1 综合 = 64-80%)。
# 第一轮 LLM 看到的工具只有 submit_analysis_plan(由 _build_agent_registry 在
# cfg.enable_plan_and_execute=True 时控制),且 tool_choice 协议层强制必 emit。
# 详见 lxj-adapter-deploy/design/2026-05-28-D-plan-and-execute-architecture.md
EXCEL_AGENT_PLAN_PROMPT = """你是一个数据分析助手。本次对话用户提供了一个表格数据集(Excel)。

你**必须**做一件事:把回答用户问题所需的**所有**查询步骤,一次性提交到 `submit_analysis_plan` 工具。

【工作模式】
1. 仔细读用户问题,在脑子里(reasoning 里)拆解:要回答这个问题,需要哪些独立的统计/查询?
2. 把每个独立的查询写成一个 step,塞进 `submit_analysis_plan.steps` 数组,然后 emit 这一个 tool_call。
3. 系统会按 `depends_on` 拓扑排序、自动并行执行所有 step,把结果汇总后再给你。**你不需要也不能自己迭代调用 excel_query**。
4. 等系统汇总后,你基于全部真实数据综合作答。

【关于 step 的写法】
- `question` 字段是给后端 excel_query 的自然语言指令(同你以前调 excel_query 的 question 参数);后端自动写 DuckDB SQL 执行,你**无需**也**不要**写 SQL。
- 一个 step 内部可以包含多个相关指标 —— 「按区域分组算总营业额、总门店数、单店均值,排前 10」 是**一个** step(一段 SQL 含 CTE 就能算完),不要拆成 3 步。
- **不同维度 / 不同实体粒度 / 不同数据子集** 才需要拆成不同 step。例:「按区域分组」和「按教练分组」是两个独立 step;「全年合计」和「分月合计」也是两个 step。

【关于 depends_on】
- **绝大多数业务查询互相独立**,`depends_on` 填空数组 `[]` 并行最快。
- 错误示例:「① 算全国总营业额,② 算各区域占比」—— 可并行,② 不需要等 ①(② 本身就能算"区域营业额 / SUM(全国)")。
- 🔴 **唯一必须用 depends_on 的情况:数据集表很多 / 结构不确定,你不知道指标在哪张表、列名叫什么时**。这时**先列一个「查看表结构」step 放在 plan 第一个,再给所有"需要这些表名/列名才能写出来"的查询 step 填上 `depends_on: ["该结构 step 的 id"]`**——否则查询 step 在不知道结构时盲猜表名列名,在多表数据集里会反复试错、撞超时失败(实测教训:十几张利润表的数据集,查询 step 不依赖结构 step → 全部超时)。表结构清楚时(单表 / 列名明确)则不必,直接并行。

【关于 step 数量】
- 简单问题:1 步(如"全年总营业额是多少")
- 中等问题:2-4 步(如"对比直营/加盟的关键经营指标")
- 复杂问题:4-8 步(如"详细分析这个 Excel 的核心经营情况")
- **上限 12 步**,超了说明拆得太碎,merge 相关 step。

【关于 plan 完整性】
- plan 提交后,你**没有机会再追加 step**(本轮预算用完了)。**一次性把要查的都列出**。
- 拿不准的边缘指标也可以列上,系统按 plan 跑完后,你看实际结果再决定哪些进答案。
- 用户问题没明说但你判断需要的"上下文性 step"(如先看表结构、口径定义),也直接列进去;**若后续查询 step 要用到它的结果(表名/列名/口径),务必给那些 step 填 `depends_on`**,别让它们在不知道结构时盲查。

【严格禁止】
- 禁止在 content 里写"我将调用 excel_query 查询..." 这类计划叙述 —— 你的 plan **必须**通过 `submit_analysis_plan` tool 提交,不是写在 content 里。
- 禁止试图直接调 excel_query —— 本轮只有 `submit_analysis_plan` 这一个工具可用,你看不到 excel_query 工具。
- 禁止凭记忆 / 常识回答 —— 数据集内容**不在**对话上下文里,必须先 plan + 执行。
- 禁止把多个独立指标硬塞进一个 step 的 question 里(后端 SQL 一行写不完),也禁止把能合并的相关指标硬拆成多个 step(浪费 plan 预算)。

【数据集结构】
{schema}
"""


# v0.5.0 B(文件生成 MVP):PPTX 生成模式系统提示词。显式 PPTX 模式(gen_pptx chip)整体
# 替换 cfg.system_prompt;第一轮 force_first_tool_name 强制模型 emit generate_pptx。
# (自动多类型模式 gen_file 用 FILE_GEN_PROMPT,见下,不强制。)
# 模型只产出**结构化大纲**(A 铁律),渲染由 adapter 写死的 python-pptx 模板完成。
PPTX_GEN_PROMPT = """你是一个演示文稿(PPT)内容策划助手。用户希望你把某个主题整理成一份结构清晰的演示文稿。

你**必须**做一件事:把演示文稿的**完整大纲**一次性提交到 `generate_pptx` 工具。系统会用统一模板把它渲染成真正的 .pptx 文件,供用户下载。

【工作模式】
1. 读懂用户想要的主题、受众、篇幅;在 reasoning 里规划好幻灯片结构(封面 → 若干正文页 → 收尾)。
2. 把规划好的内容填进 `generate_pptx`:`title`(主标题)、可选 `subtitle`、`slides`(正文每页一个对象,含 `title` 和 `bullets`)。
3. emit 这**一个** tool_call。系统渲染完成后会给你回执,你再用一两句话告知用户文件已生成。

【内容质量要求】
- **一页一个主题**:每页 `title` 是该页的核心论点,`bullets` 是支撑它的 3-6 条要点短句。
- **要点写短句不写长段**:每条 bullet 一行能说完;要展开的细节放进该页 `notes`(演讲者备注)。
- **有逻辑流**:正文页之间有递进/并列关系(如 现状 → 问题 → 方案 → 计划),不要堆砌零散事实。
- **篇幅**:除非用户指定,正文 5-12 页比较合适(加封面);宁可精炼,不要注水。
- 若用户给了具体素材/数据,**忠实使用**,不要编造数字;信息不足时给出合理的框架性大纲并在收尾页注明"待补充数据"。

【关于样式】
- 你**只负责文字内容**。版式、配色、字体、项目符号、页码全部由渲染器统一控制 —— **不要**在文本里写 markdown 符号、"•"、"第 X 页"、颜色或排版指令。

【严格禁止】
- 禁止用自然语言把大纲写在 content 里 —— 大纲**必须**通过 `generate_pptx` 工具提交。
- 禁止本轮直接给最终答复(本轮只有 `generate_pptx` 一个工具,且被强制调用);渲染完成后下一轮再收尾。
- 禁止输出 <tool_call>、{"name":...} 这类原始工具调用语法 —— 直接 emit 标准 tool_calls 结构。
"""


# v0.6.0 B+(多类型 + 自动识别):file_gen 模式系统提示词。与 PPTX_GEN_PROMPT 不同 ——
# **不强制**首轮调工具(tool_choice=auto):模型自己判断「用户是否真要生成文件、要哪种」。
# 若只是普通聊天 / 分析问题(如"帮我分析这份 PPT"),正常文字作答即可,**不要硬调工具**。
FILE_GEN_PROMPT = """你是一个能把内容整理成**可下载文件**的助手。系统给了你一组文件生成工具,你要先判断:用户这次到底想不想要一个文件?想要哪种?

【先判断:要不要生成文件】
- 用户明确想"做 / 生成 / 导出 / 整理成"某种文件 → 选最合适的工具生成。
- 用户只是提问、闲聊、或想让你**分析/解读**某个已有文件(如"看看这份报表说明什么")→ **不要**调任何工具,直接用文字正常回答。
- 拿不准时,优先按用户字面意思:说"做个表"就生成表,问"这个表怎么看"就解读。

【选哪种文件】
- **演示文稿 / PPT / 幻灯片** → `generate_pptx`(出大纲:每页标题 + 要点)。
- **Excel / 表格 / 多维数据 / 需要多个工作表或后续计算** → `generate_xlsx`(列名 + 数据行,数字写成数值)。
- **简单单表导出 / 纯数据列表** → `generate_csv`(轻量,单表)。
- **文档 / 报告 / 说明 / 方案 / 信函 / Word** → `generate_docx`(标题 + 小节,每节正文段落和/或要点)。
- **网页 / HTML / 单页展示 / 可视化看板 / dashboard** → `generate_html`(只填 `brief` 一段话讲清需求+数据+想要的图表,系统会生成带交互图表的完整网页;**别自己写 HTML 代码**)。

【铁律(所有生成工具通用)】
- 所有生成工具:你**只填工具参数**(结构化数据 / 或网页 brief),**绝不写**渲染/排版代码;版式与最终文件由系统生成。
- generate_html:只在 `brief` 里**用一段话讲清需求 + 数据 + 想要的图表**(系统会据此自由生成完整含 chart.js 的网页);**别自己写 HTML/JS 代码**——把数据和意图说清即可。
- 选定一种工具,**一次性把内容填全**,emit 这**一个** tool_call(同一轮不要同时调多个生成工具;一次请求出一份文件)。
- 工具调用成功后系统会把文件以"文件卡"展示在对话下方供用户下载;你随后用一两句话告知用户即可,不要把全部内容再复述一遍。
- 信息不足就给合理的框架性内容,不要编造具体数字。

【严禁】
- 禁止用自然语言把文件内容写在 content 里冒充"已生成"——真要生成就 emit 工具调用。
- 禁止输出 <tool_call>、{"name":...} 这类原始语法 —— 直接 emit 标准 tool_calls。
"""


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
    "\n"
    "工具调用协议（极其重要）：\n"
    "- 需要调用工具时，**直接 emit 标准的 tool_calls JSON 结构**，"
    "**严禁**把「让我搜一下…」「我将调用 web_search…」「请允许我查询…」"
    "「下面我来抓取…」这类**叙述句**当作回答内容输出。\n"
    "- 这种叙述对用户无意义、又不会真的触发工具调用，会让用户卡在"
    "「正在思考」几十秒后无果。要么 emit tool_calls，要么直接给最终答案，"
    "二者必居其一，**不要写过渡叙述**。\n"
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

    def register_schema_only(self, schema: dict[str, Any]) -> None:
        """v0.4.0 D 重构:挂 schema 让 LLM 看到,但不挂 impl。
        用于 inline-handled tool(如 submit_analysis_plan)—— 由 run_agent /
        run_agent_stream 在 plan 分支直接拦截执行,不走 dispatch 路径。
        漏拦截时 dispatch 会返回 "unknown tool" error,且
        _dispatch_tool_calls_parallel 会 emit `agent_plan_intercept_missed`
        progress(开发者明显告警,防 ToolRegistry 异常吞掉静默)。"""
        name = schema["function"]["name"]
        self._schemas[name] = schema
        # 不写 self._impls[name] — dispatch 时 lookup 不到自然走 unknown-tool 路径

    def has_impl(self, name: str) -> bool:
        """是否注册了 impl(用于区分 inline-handled tool / 正常 tool)。"""
        return name in self._impls

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
    # v0.2.17 / v0.2.20 / v0.2.22 thinking 策略:agentic 循环每轮单独决定。
    # - intermediate(前 N-1 轮,决策调哪个工具):默认**关**,模型有 tool_call
    #   这个外置思考机制,内置 chain-of-thought 在工具决策场景是浪费 + 易出
    #   v0.2.14 类 bug。这个是硬约束,client 也覆盖不了(对应前端无 chip)。
    # - force_answer(最后一轮综合所有工具结果作答):**passthrough**(v0.2.22)。
    #   v0.2.17 默认 True、v0.2.20 默认 False 都过于一刀切。前端有「深度思考」chip,
    #   通过 extra_body.chat_template_kwargs.enable_thinking 表达用户意图,adapter
    #   应该尊重:client 给 True 就让 EAS 跑 thinking、reasoning_content delta
    #   照常透传给前端(前端会渲染思考过程面板);未指定/给 False 就跟中间轮一样
    #   注入 enable_thinking=false。
    #
    #   ``force_answer_thinking_enabled`` 三态:
    #     None         = passthrough(默认,跟前端 chip 走)
    #     True/False   = 硬覆盖(测试 / 临时压问题用)
    intermediate_thinking_enabled: bool = False
    force_answer_thinking_enabled: Optional[bool] = None
    # v0.2.24 plan B(Anthropic 模式):force_answer 之前把历史里的 assistant
    # tool_calls + tool 对**重写**成 assistant narration("我已查到这些资料: ..."),
    # 消除模型自己 emit 过的 tool_call JSON 痕迹。原因:Qwen3.5 在 force_answer
    # 看到前 N 轮 tool_call history 时容易被 prime 走 tool_call mimicking path,
    # 即使 enable_thinking=true 也常不激活 thinking(实测 chip ON 激活率 ~10%)。
    # Anthropic Claude 4 模型对自己历史 robust 不需要这步;Qwen3.5 不行,得 adapter
    # 层来扛。True = 重写(更好的 chip ON 激活率,但 KV cache miss 多 1-2s 延迟)。
    # v0.2.24 默认 True,实验后若激活率提升不达预期可回切。
    force_answer_rewrite_history: bool = True

    # v0.2.27 thinking 死循环防护:default sampling penalty。详见 adapter.py
    # ADAPTER_DEFAULT_FREQUENCY_PENALTY / ADAPTER_DEFAULT_PRESENCE_PENALTY 注释。
    # 0 表示不注入(让 SGLang 用 0 默认)。
    default_frequency_penalty: float = 0.0
    default_presence_penalty: float = 0.0
    # reasoning_content 累积字符数硬上限。> 此值时主动 abort 防 thinking 死循环。
    # 0 表示不限制。
    max_reasoning_chars: int = 0

    # v0.2.30 streaming path intent-leak 续轮兜底:Qwen3.5 在多轮 agent 偶发
    # "流式 content 全是'我将调用 excel_query ...'这类计划叙述、tool_calls 全空"
    # 的 silent failure(_speculative_iteration 判 path='content' 直接收尾)。
    # EXCEL_AGENT_SYSTEM_PROMPT v0.2.29 已经在 prompt 层严禁(行 314-318),
    # 实测仍偶发 → 架构层兜底。命中 _looks_like_intent_not_answer 或
    # _ends_with_dangling_intent + 有迭代预算 → 流分隔符 + 把 leak content
    # append 进 history + 加 system 纠正 hint + 进下一轮(下一轮也 force
    # tool_choice 若 force_first_tool_name 配置)。
    # 0 = 关闭(等同 < v0.2.30 行为);1 = 推荐默认。
    max_intent_leak_retries: int = 0

    # v0.3.0 D 方案:Plan-and-Execute 模式。把"LLM 每轮决定下一步"(brittle
    # iterative loop,单轮 80% × 5 轮 = 33%)改造为"LLM 一次性规划 → adapter
    # 按 plan 调度 → LLM 综合作答"(deterministic plan-and-execute,目标 80-90%)。
    # 仅 Excel agent 适合(结构化任务);Web agent 继续走旧 loop(开放式探索)。
    # True 时:
    #   - _build_agent_registry 只挂 SUBMIT_ANALYSIS_PLAN_TOOL(隐藏 excel_query)
    #   - cfg.system_prompt 用 EXCEL_AGENT_PLAN_PROMPT
    #   - cfg.force_first_tool_name = "submit_analysis_plan"(替代 "excel_query")
    #   - run_agent_stream 走 plan-and-execute 分支(Phase 2 实现)
    # 详见 lxj-adapter-deploy/design/2026-05-28-D-plan-and-execute-architecture.md
    enable_plan_and_execute: bool = False

    # v0.4.0 D 重构:plan 执行注入的"step 执行器"。由 adapter.py 在 handler 路径
    # 通过 _make_excel_run_step(dataset_id) 注入一个绑定 dataset_id 的 callable。
    # 签名:(question: str, timeout_s: int) -> Any —— 返回 dict 含 answer / sql / error 等。
    # agentic_web.py 不知道 Excel 后端存在,守 AGENTS.md generic/open-source safe 边界。
    # 仅当 enable_plan_and_execute=True 且 plan_step_runner 非 None 时,plan 分支才触发。
    plan_step_runner: Optional[Callable[[str, int], Any]] = None

    # v0.4.0 D 重构:plan 执行的并发上限。同一 batch 内最多起 N 个 worker thread,
    # 超出的进 pending 等前完成。由 adapter.py 从 ADAPTER_AGENT_PLAN_PARALLELISM env 注入。
    agent_plan_parallelism: int = 4

    # v0.4.0 D 重构:单个 step(run_step 调用)的超时(秒)。
    # 由 adapter.py 从 ADAPTER_AGENT_PLAN_STEP_TIMEOUT env 注入。
    agent_plan_step_timeout: int = 60

    # v0.4.0 D 重构:plan 整体超时(秒) —— 兜底防 worker 跑飞(如 run_step hang)。
    # 触发后标剩余 step error,正常 emit plan_step_end + plan_complete,正常 yield result。
    # 由 adapter.py 从 ADAPTER_AGENT_PLAN_TOTAL_TIMEOUT env 注入。
    agent_plan_total_timeout: int = 480

    # v0.4.2:单个 plan step 失败后的最大 retry 次数。
    # 由 adapter.py 从 ADAPTER_AGENT_PLAN_STEP_MAX_RETRIES env 注入。
    # 默认 0 = 不 retry,失败立即标 ok=false(向后兼容)。
    # 触发条件:run_step 返回 dict 含 error 字段 / worker 抛异常。
    # 不触发:plan_total_timeout 路径(它是 plan-level 兜底,不是 step-level 失败)。
    # 重试时:重新起 worker 跑同 step,emit plan_step_retrying 事件给前端。
    # plan_step_end.elapsed_ms 是从第一次 start 到最终完成的累计时间(含 retry)。
    # plan_step_end.attempts 字段记录实际尝试次数(1 = 没 retry,2 = retry 1 次)。
    # 修 excel-poc 偶发返 HTTP 500 的影响(实测:同 prompt 一次失败一次全成,
    # 加 retry 1 次能把"偶发失败"兜住大半)。
    agent_plan_step_max_retries: int = 0

    # v0.2.25 L1:带数据集(Excel)的请求,**第一轮**强制 tool_choice 指向唯一
    # 的查询工具(force_first_tool_name),协议层保证模型不能直接给文本叙述。
    # 修的是 Qwen3.5 在 Excel 大表 + 多工具场景偶发的"describe but don't call"
    # 退化 —— 模型说"我需要先查询一下数据集..."然后没真 emit tool_call,
    # 用户看到一段空话,要点重新生成。
    #
    # 用 ``{"type":"function","function":{"name": X}}`` 而非 ``"required"`` ——
    # SGLang 0.5.9 对 ``"required"`` 的兼容性不如指定工具名稳定,而 Excel 模式
    # 只有 excel_query 一个工具,两者语义等价。
    #
    # 仅作用于 iteration == 1:后续轮模型已有工具结果,正常 ``"auto"`` 允许
    # 进入 final answer 文本路径。force_answer 轮 tools=[] → tool_choice 不生效。
    #
    # None 表示关闭(默认,web 模式)。adapter.py 在 excel_dataset_id 不空时
    # 设为 "excel_query"。
    force_first_tool_name: Optional[str] = None

    # v0.5.0 B(文件生成 MVP):PPTX 确定性生成模式。True 时:
    #   - 显式 PPTX(gen_pptx chip):_build_agent_registry 只挂 GENERATE_PPTX_TOOL +
    #     force_first_tool_name="generate_pptx"(协议层强制首轮 emit 大纲)
    #   - 自动多类型(gen_file,v0.6.0 B+):挂 ALL_FILE_GEN_TOOLS,tool_choice=auto,
    #     模型自决类型 / 是否生成;system_prompt = FILE_GEN_PROMPT,不设 force_first
    #   两条都 register_schema_only(inline 拦截,不走 dispatch),run_agent_stream 的
    #   file_gen 拦截分支按 tool name 路由渲染→上传→emit x_adapter_artifact。
    # 模型只产出**结构化内容数据**(A 铁律),渲染是 adapter 注入的写死代码。
    enable_file_gen: bool = False

    # v0.6.0 B+:注入的文件渲染器,签名 (tool_name: str, args: dict, artifact_id: str) -> dict
    # (v0.5.0 是 PPTX 专用 (outline_args, id);B+ 加 tool_name 首参支持多类型路由)。
    #   成功:{"ok": True, "name": str, "mime": str, "size": int,
    #          "download_url": str, "object_key": str}
    #   失败:{"ok": False, "error": str, "name": str}
    # adapter.py 在 handler 路径通过 _make_file_renderer() 注入;agentic_web.py 不
    # 知道 python-pptx / openpyxl / OSS 细节(同 plan_step_runner 依赖反转,守
    # AGENTS.md generic / open-source safe 边界)。仅 enable_file_gen=True 且非 None 时触发。
    file_renderer: Optional[Callable[[str, dict, str], dict]] = None


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


# v0.4.0 D 重构:删除 ACTIVE_PROGRESS_CB ContextVar(原 v0.3.0 Phase 3 引入)。
# 重构后 plan 执行升级为 agent loop 一等公民(`_execute_plan_streaming`),通过
# generator 直接 yield SSE chunk,**不再依赖 ContextVar 把 progress_cb 跨线程透传**。
# ToolRegistry 接口零改动,web 工具继续走 _dispatch_tool_calls_parallel(纯 sync,
# 无 ContextVar 需求)。


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
    tool_choice: Any = None,
) -> urllib.request.Request:
    """Build the upstream POST request (shared by streaming + non-streaming).

    ``tool_choice`` overrides the default ``"auto"``. v0.2.25 主 loop 用它在
    Excel 第一轮传 ``{"type":"function","function":{"name":"excel_query"}}``
    强制模型必须 emit tool_call,不能给文本叙述。None = 用默认 "auto"。
    """
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        payload["parallel_tool_calls"] = True
    if extra:
        for key, value in extra.items():
            # `stream`/`stream_options` excluded: internal agentic calls are non-streaming.
            if key in {"messages", "tools", "tool_choice", "stream", "stream_options", "model"}:
                continue
            payload[key] = value

    # v0.2.27 thinking 死循环防护 —— agent loop 路径同步注入 sampling penalty。
    # cfg.default_frequency_penalty / default_presence_penalty 从 adapter.py 顶层 env
    # 透传过来。client 显式覆盖时(通过 extra)按 client 走,这里只填默认。
    # 🔴 v0.4.3:带 tools / tool_choice≠none 时跳过 penalty(同 adapter.py _transform_payload)
    #    —— agent loop 本质是工具调用,长 SQL / 结构化 tool_call arguments 会被 penalty
    #    压成词链死循环。工具轮跳过、最终总结轮(不带 tools)仍注入防死循环。
    _is_tool_call = bool(payload.get("tools")) or (payload.get("tool_choice") not in (None, "none"))
    if not _is_tool_call:
        if cfg.default_frequency_penalty > 0 and "frequency_penalty" not in payload:
            payload["frequency_penalty"] = cfg.default_frequency_penalty
        if cfg.default_presence_penalty > 0 and "presence_penalty" not in payload:
            payload["presence_penalty"] = cfg.default_presence_penalty
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
    tool_choice: Any = None,
) -> dict[str, Any]:
    """Non-streaming POST. Returns the full parsed response dict (with tool_calls
    intact when finish_reason == 'tool_calls')."""
    req = _build_upstream_request(
        cfg, messages, tools, extra, stream=False, tool_choice=tool_choice,
    )
    with urllib.request.urlopen(req, timeout=cfg.request_timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _stream_upstream(
    cfg: AgentConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
    tool_choice: Any = None,
):
    """Streaming variant of ``_call_upstream``:逐 chunk yield 上游 SSE 块解出的
    OpenAI 标准 dict。``data: [DONE]`` 触发正常退出。

    调用方负责处理 ``urllib.error.HTTPError`` / 连接异常(包在 try/except)。

    v0.2.16: HTTPError 抛出前,**把 response body 头 500 char 拼到异常 message**,
    上层 ``agent_error`` 事件就能看到 EAS 的实际报错(context too long / 鉴权失败
    等),不再像之前那样只看到一句"HTTP Error 400: Bad Request"。
    """
    req = _build_upstream_request(
        cfg, messages, tools, extra, stream=True, tool_choice=tool_choice,
    )
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


def _tool_args_preview(tc: dict[str, Any]) -> str:
    """v0.2.20: 给 ``tools_dispatch`` 事件用,提取工具调用最关键的可读字段。

    web_search → query;web_fetch/web_view → url;excel_query → question;
    其他 → 整段 arguments JSON 截 60 char。前端拿来在 pending 步骤上预显示
    "搜索: <query>" / "抓取: <url>" / "查询: <question>"。
    """
    fn = tc.get("function") or {}
    name = fn.get("name") or ""
    raw = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return str(raw)[:60]
    if not isinstance(args, dict):
        return str(args)[:60]
    if name == "web_search":
        return str(args.get("query") or "")[:120]
    if name in ("web_fetch", "web_view"):
        return str(args.get("url") or "")[:160]
    if name == "excel_query":
        return str(args.get("question") or "")[:160]
    if name == "submit_analysis_plan":
        # v0.4.0:plan 特化 —— 让 args_preview 显示有意义的摘要,而不是被截断的
        # raw JSON。正常路径下 plan 走 _execute_plan_streaming 不经过 dispatch,
        # 这个 preview 用在 stream_agent plan 拦截分支 emit 的兼容性 tools_dispatch
        # 事件里(必做兼容事件 #1)。
        steps = args.get("steps") if isinstance(args.get("steps"), list) else []
        summary = str(args.get("summary") or "").strip()
        if summary:
            return f"{summary}(共 {len(steps)} 步)"[:200]
        ids = [
            str(s.get("id", "?"))
            for s in steps[:5]
            if isinstance(s, dict)
        ]
        suffix = "" if len(steps) <= 5 else f" 等 {len(steps)} 步"
        return f"plan: {', '.join(ids)}{suffix}"[:200]
    # 通用兜底
    return json.dumps(args, ensure_ascii=False)[:80]


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
# 真实问题:多轮 web_search + web_fetch + web_view 累积后,force_answer 把整段
# history 喂给上游,容易撞 EAS 输入上限。早期版本按 image_url 占 2000 char 估算,
# 但 base64 image data 的 JSON 表示通常 100KB+,严重低估 —— 含图历史的真实 body
# 可能 300KB+ 而 estimator 只算 30K,导致 truncate 不触发。
# v0.2.23 修正:直接对每条 message 做 ``json.dumps`` 取其序列化长度。准确反映
# 即将送上游的实际字节数,代价是每次裁剪检查多一次 dump(可接受)。


def _estimate_messages_size(messages: list) -> int:
    """估算 messages 总 char 数 —— 用真实 JSON 序列化长度。

    含 multimodal content (image_url base64) 的消息按真实 base64 体积算,不再
    低估。失败的消息按 200 char 兜底。
    """
    total = 0
    for m in messages:
        try:
            total += len(json.dumps(m, ensure_ascii=False))
        except (TypeError, ValueError):
            total += 200
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


def _extract_tool_result_parts(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """从 tool message 的 content 抽出 (纯文本摘要, 多模态 image_url 部分列表)。

    支持 OpenAI 多模态 content list:``[{"type":"text",...}, {"type":"image_url",...}]``
    (web_view 截图就是这种形态)。返回 (文本, 图片 parts),用于 rewrite history。
    """
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        text_parts: list[str] = []
        image_parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text_parts.append(str(part.get("text") or ""))
            elif ptype == "image_url":
                image_parts.append(part)
        return "\n".join(p for p in text_parts if p), image_parts
    return str(content) if content is not None else "", []


_REWRITE_TOOL_RESULT_PREVIEW_CHARS = 1500  # 每条 tool 结果在 narration 中保留长度


def _rewrite_history_for_force_answer(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """v0.2.24 plan B(Anthropic 模式):把 history 里的 assistant tool_calls 和
    tool result 对**重写**成普通 assistant narration,消除 tool_call JSON 痕迹。

    输入(典型 force_answer 之前的 messages):
        [
          {role:system, content:"<merged hint>"},
          {role:user,   content:"<原问题>"},
          {role:assistant, tool_calls:[ws,ws], content:""},
          {role:tool,    tool_call_id, content:"..."},
          {role:tool,    tool_call_id, content:"..."},
          {role:assistant, tool_calls:[wf,wv], ...},
          {role:tool,    tool_call_id, content:"..."},
          {role:tool,    tool_call_id, content:[multimodal img]},
          ... 5+ 轮
        ]

    输出:
        [
          {role:system, content:"<merged hint>"},
          {role:user,   content:"<原问题>"},
          {role:assistant, content:"以下是我已经收集到的资料:\\n1. 【web_search】query → ...\\n2. ..."},
          {role:user,   content:[{type:"text","text":"以下是截图供参考:"}, image, image]}  # 仅在有截图时
        ]

    为什么这样:Qwen3.5 看到自己历史里的 tool_call JSON 会被 prime 走 mimicking
    path,即使 enable_thinking=true 也常不激活 thinking。Anthropic Claude 在多轮
    extended-thinking + tool use 时服务端自动 strip thinking blocks;Qwen3.5 模型
    本身不像 Claude 4 那样 robust,我们需要更激进的 history cleanup。
    """
    if not messages:
        return messages

    out: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}  # tool_call_id -> {name, args_preview}
    narration_entries: list[dict[str, Any]] = []
    multimodal_parts: list[dict[str, Any]] = []

    def _flush() -> None:
        """把累积的 narration 输出成 assistant message + 可选的 user image message。"""
        if not narration_entries and not multimodal_parts:
            return
        if narration_entries:
            lines = ["以下是我已经通过工具收集到的资料(按调用顺序):", ""]
            for i, entry in enumerate(narration_entries, 1):
                args = entry["args_preview"]
                args_str = f" {args}" if args else ""
                ok_mark = "" if entry["ok"] else "  ⚠️[调用失败]"
                lines.append(f"{i}. 【{entry['name']}】{args_str}{ok_mark}")
                if entry["result_preview"]:
                    # 缩进显示结果摘要
                    result_lines = entry["result_preview"].splitlines()
                    for rl in result_lines[:30]:  # 单条最多 30 行
                        lines.append(f"   {rl}")
                lines.append("")
            out.append({
                "role": "assistant",
                "content": "\n".join(lines).rstrip(),
            })
        if multimodal_parts:
            # OpenAI 协议:multimodal content 只允许在 user/tool message,assistant
            # 必须是 string。所以截图单独放一条 user message。
            out.append({
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": "(以下是上述工具调用过程中截取的页面截图,供综合作答参考)"},
                    *multimodal_parts,
                ],
            })
        narration_entries.clear()
        multimodal_parts.clear()
        pending.clear()

    for m in messages:
        role = m.get("role")
        if role == "system":
            out.append(m)
            continue
        if role == "user":
            # 用户消息前先 flush 之前累积的工具叙事
            _flush()
            out.append(m)
            continue
        if role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                # 记录这些 tool_calls,等对应 tool result 来匹配
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    tcid = tc.get("id") or ""
                    fn = tc.get("function") or {}
                    pending[tcid] = {
                        "name": str(fn.get("name") or ""),
                        "args_preview": _tool_args_preview(tc),
                    }
                # assistant tool_call message 本身**不进 out**,等被 narrate
                continue
            # 普通 assistant content(罕见,agent loop 中间通常不会有)
            if m.get("content"):
                _flush()
                out.append(m)
            continue
        if role == "tool":
            tcid = m.get("tool_call_id") or ""
            meta = pending.pop(tcid, None)
            if meta is None:
                # 找不到对应 assistant tool_call:可能历史结构异常,直接跳过
                continue
            text, images = _extract_tool_result_parts(m.get("content"))
            ok = True
            # 简单失败检测:tool 结果以 "{"error" 开头 / 含 "blocked:"
            t_strip = text.lstrip()
            if t_strip.startswith('{"error"') or t_strip.startswith("[blocked"):
                ok = False
            # 截到预览长度
            preview = text
            if len(preview) > _REWRITE_TOOL_RESULT_PREVIEW_CHARS:
                preview = (
                    preview[:_REWRITE_TOOL_RESULT_PREVIEW_CHARS]
                    + f"\n... [截断, 原文还有 {len(text) - _REWRITE_TOOL_RESULT_PREVIEW_CHARS} 字符]"
                )
            narration_entries.append({
                "name": meta["name"],
                "args_preview": meta["args_preview"],
                "result_preview": preview,
                "ok": ok,
            })
            multimodal_parts.extend(images)
            continue
        # 其他 role 原样保留
        out.append(m)

    # 收尾 flush
    _flush()
    return out


def _inject_force_answer_hint(
    messages: list[dict[str, Any]],
    hint: str,
) -> list[dict[str, Any]]:
    """合并 force_answer hint 进**头部** system 消息,而不是在末尾另开一条。

    v0.2.19 修复:EAS upstream 严校验「System message must be at the beginning」,
    之前 ``augmented + [{role:system, ...}]`` 把第二条 system 追加在末尾会触发
    HTTP 400(只在 force_answer 那一轮、且历史很长时才容易撞 —— 短会话碰巧没
    被严校验拦下来)。

    合并策略:把 hint 拼到原 system 末尾(分隔双换行),其余消息原样保留。
    上下文末尾顺势是 user/tool 消息,符合 EAS 期望的"system 在前,对话在后"形态。
    若历史里没有 system(理论上 _ensure_system_prompt 保证有),退化为开头插一条。
    """
    if not hint:
        return list(messages)
    out = list(messages)
    if out and out[0].get("role") == "system":
        existing = str(out[0].get("content") or "")
        merged = existing + ("\n\n" if existing else "") + hint
        out[0] = {**out[0], "content": merged}
        return out
    return [{"role": "system", "content": hint}] + out


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

        # v0.4.0:漏拦截兜底告警 —— plan tool 正常路径应该被 stream_agent /
        # run_agent 的 plan 分支直接 yield generator 拦截,不应该进 dispatch。
        # 走到这里说明:(1) cfg.enable_plan_and_execute=False(env 关闭场景);
        # (2) plan_step_runner 未注入(adapter.py handler 漏配);(3) LLM 在
        # 多 tool 场景下混合 emit 了 plan(理论上 register_schema_only 已防,
        # 但 LLM 可能直接构造 tool_call 而无视 schema)。三种都是 bug,
        # 给开发者明显信号(progress event 走 Langfuse + 前端 + 日志)。
        if name == "submit_analysis_plan" and not registry.has_impl(name):
            _emit(
                progress_cb, "agent_plan_intercept_missed",
                "submit_analysis_plan 落到 dispatch(预期应被 plan 分支拦截)—— "
                "请检查 cfg.enable_plan_and_execute / cfg.plan_step_runner 是否正确注入",
                name=name,
                args_preview=str(args)[:200],
                iteration_hint="plan 分支应在 _execute_plan_streaming 处理,不应到这里",
            )
            # 继续走 unknown-tool 路径(registry.dispatch 会返 "unknown tool" error)
            # —— LLM 看到 error 后会知道 plan 不可用,fallback 到旧 iterative loop 行为

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
        # v0.2.20: 带 tool_call_id —— 前端用它把 tool_start / tool_end
        # 精确配对(并发场景下两个 tool_start 紧挨着,如果不带 id 没法知道
        # 后续的 tool_end 对应哪一个,会让"检索过程"步骤列表 active/done 乱掉)
        tc_id = tc.get("id", "") or f"tc_{idx}"
        _emit(progress_cb, "tool_start", f"调用 {name}",
              tool_call_id=tc_id, name=name, args=args)
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
            tool_call_id=tc_id,    # v0.2.20: 跟 tool_start 配对
            name=name,
            elapsed_ms=elapsed_ms,
            ok=ok,                  # v0.2.20: 失败时前端可标 step 为 error 态
            modality="image" if is_image else "text",
        )
        indexed[idx] = _make_tool_message(tc, result, cfg.max_tool_result_chars)

    if fresh:
        # v0.4.0:还原为最初的 pool.submit(_run_one, ...) 直接派发。
        # 不再用 copy_context().run —— plan 执行已升级为一等公民 generator,
        # 不依赖 ContextVar 跨线程透传 progress_cb。
        with ThreadPoolExecutor(max_workers=max(cfg.parallel_dispatch_workers, 1)) as pool:
            futures = [
                pool.submit(_run_one, idx, tc, name, args)
                for idx, tc, name, args in fresh
            ]
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

# v0.3.3 D Phase 4:plan-and-execute 模式下,plan tool 跑完后强制综合作答的
# system hint(替代 FORCE_ANSWER_SYSTEM_HINT,语义不同 —— 这里不是因为预算
# 用光,而是 plan 设计上 dispatcher 完一次后 LLM 必须综合作答)。
# 配合 stream_agent 在 plan_dispatch_done 后置 tools=[]。
PLAN_SYNTHESIS_HINT = (
    "【plan 已执行完毕】你提交的 submit_analysis_plan 已经被 adapter 完整执行,"
    "所有 step 的查询结果都在上一条 tool 消息(plan_executed=true)的 results 数组里。\n"
    "\n"
    "本轮你必须**立即用自然语言**综合作答,直接给用户最终的分析报告。\n"
    "\n"
    "严格禁止:\n"
    "- 禁止再 emit 任何 tool_calls —— 本轮已禁用工具(架构上不可能再调一次 plan)\n"
    "- 禁止写'我将查询...''让我再提交一个 plan...''接下来调用...'这类叙述\n"
    "- 禁止凭记忆 / 推测补充数据 —— **所有数字必须逐字来自 plan results**\n"
    "- 禁止输出 <tool_call>、{\"name\":...} 这类工具调用语法\n"
    "\n"
    "答案格式建议:用 Markdown 表格按 step 组织数据,附关键发现/对比/洞察。\n"
    "若 plan 中某 step 失败(result.error 非空),如实告知用户该维度未拿到数据,"
    "**基于剩余成功 step 综合作答**,不要因为部分失败而拒答。"
)

# v0.5.0 B / v0.6.0 B+:文件生成完毕后强制收尾的 system hint(配合 stream_agent 在
# file_gen_dispatch_done 后置 tools=[])。文件已 emit 给前端(下方文件卡),本轮只需
# 一句话告知用户,不要重复文件全文。措辞对所有文件类型通用(PPT/Excel/Word/CSV/网页)。
FILE_GEN_SYNTHESIS_HINT = (
    "【文件已生成完毕】你提交的内容已被渲染成可下载文件,"
    "文件卡已显示在对话下方(用户可直接下载)。\n"
    "\n"
    "本轮你必须**用一两句自然语言**收尾即可,例如:已为你生成《<标题>》,"
    "可点击下方卡片下载;如需调整可告诉我。\n"
    "\n"
    "严格禁止:\n"
    "- 禁止再 emit 任何 tool_calls —— 本轮已禁用工具\n"
    "- 禁止把文件内容(大纲 / 表格数据 / 文档正文)重新抄一遍(文件里已有,啰嗦)\n"
    "- 禁止输出 <tool_call>、{\"name\":...} 这类工具调用语法\n"
    "若上一条 tool 消息显示生成失败(ok=false),如实把失败原因告诉用户,并建议重试或换个表述。"
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
    # v0.2.25:补 QA-2026-05-26 实测到的"请允许我先..."句式 ——
    # Qwen3.5 在数据查询场景偏好这种"客气请示"叙述,而不是真 emit tool_call。
    "请允许我", "允许我先", "我需要先了解", "我需要了解",
)

# v0.2.25 L3:模型未真调工具时,常见的"叙述意图 + 描述查询动作"两段组合 ——
# 例:"...请允许我先查询一下数据集的概况"。这种叙述完整、可超 160 字,但语义
# 上仍是"宣告下一步动作 + 没有具体数据",必须当 intent leak 处理。
_INTENT_NARRATE_RE = re.compile(
    r"(请允许我|允许我|让我|我需要|我应该|我会|我将|我可以|我打算|我先)"
    r"[^。\n]{0,30}"
    r"(查询|查一下|查看|了解一下|了解|分析|调用|执行|获取|看看)"
)


def _looks_like_intent_not_answer(text: str) -> bool:
    """The model sometimes narrates its next tool action instead of answering
    ('我将尝试访问麦当劳的投资者关系页面…'). Two leak shapes:

    1. **Short message opens with intent phrase** — classic case
       (e.g. "我将先访问官网。"),长度 < 160 char。
    2. **Long narrative without any data + ends/contains intent-action pair**
       (v0.2.25 — QA 2026-05-26 实测) — e.g. "由于您提到的'X'通常是...为了准确
       回答您,我需要先了解数据集的结构。请允许我先查询一下数据集的概况..."
       这种 200 字左右,完整段落,但**实际没有任何数字/工具结果**,仅在描述
       接下来要做的查询动作。仍是 intent leak。
    """
    t = (text or "").strip()
    if not t:
        return False
    head = t[:20]
    if any(p in head for p in _INTENT_LEAD_PHRASES):
        return True
    # 长叙述场景:整段文本含 intent + 动作 组合,且**无数字/无引用编号**
    # ([1] / [2] 等),才判为 leak。有数字说明可能是真的分析答案,不该误伤。
    if len(t) <= 600 and _INTENT_NARRATE_RE.search(t):
        has_numbers = bool(re.search(r"\d", t))
        has_citation = bool(re.search(r"\[\d+\]", t))
        if not has_numbers and not has_citation:
            return True
    return False


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
    # v0.4.0:删除 ACTIVE_PROGRESS_CB.set —— plan 执行已升级为 generator,直接
    # 通过 yield 传 chunk 给上层,不需要 ContextVar 跨线程透传。
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
            # v0.2.19: 合并 hint 进头部 system,不在末尾另开一条 ——
            # EAS upstream 严校验「System message must be at the beginning」。
            current_messages = _inject_force_answer_hint(augmented, FORCE_ANSWER_SYSTEM_HINT)
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

        # ── v0.4.0 D 重构:非流式路径对称加 plan 拦截分支 ──
        is_plan_call = (
            cfg.enable_plan_and_execute
            and cfg.plan_step_runner is not None
            and len(tool_calls) == 1
            and (tool_calls[0].get("function") or {}).get("name")
                == "submit_analysis_plan"
        )
        if is_plan_call:
            plan_tc = tool_calls[0]
            plan_tc_id = plan_tc.get("id", "") or "tc_plan"
            plan_args_raw = (plan_tc.get("function") or {}).get("arguments") or "{}"
            try:
                plan_args = (
                    json.loads(plan_args_raw)
                    if isinstance(plan_args_raw, str)
                    else (plan_args_raw or {})
                )
                plan_parse_err: Optional[str] = None
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                plan_args = {}
                plan_parse_err = f"plan args 非合法 JSON:{type(exc).__name__}: {exc}"

            _emit(
                progress_cb, "tools_dispatch", "模型请求 1 个工具调用",
                count=1, iteration=iteration,
            )

            plan_result: Optional[dict[str, Any]] = None
            plan_t0 = time.time()
            if plan_parse_err:
                plan_result = {
                    "plan_executed": False, "error": plan_parse_err,
                }
                trace.tool_calls.append({
                    "name": "submit_analysis_plan", "ok": False,
                    "modality": "text",
                    "elapsed_ms": int((time.time() - plan_t0) * 1000),
                    "error": plan_parse_err, "step_count": 0,
                })
            else:
                for kind, payload in _execute_plan_streaming(
                    plan_args, cfg.plan_step_runner, cfg, cfg.model, trace,
                ):
                    # 非流式路径丢弃 chunk(没 SSE client),只取 result
                    if kind == "result":
                        plan_result = payload
                if plan_result is None:
                    plan_result = {
                        "plan_executed": False,
                        "error": "_execute_plan_streaming 未 yield result",
                    }

            tool_message = {
                "role": "tool",
                "tool_call_id": plan_tc_id,
                "content": json.dumps(plan_result, ensure_ascii=False),
            }
            augmented = augmented + [message, tool_message]
            continue

        # ── 非 plan tool_call:走原 _dispatch_tool_calls_parallel ──
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


def _sse_artifact_chunk(model: str, artifact: dict[str, Any]) -> dict[str, Any]:
    """OpenAI-shaped event carrying our ``x_adapter_artifact`` extension (五期 B).

    ``artifact`` 是单个文件产物信封(契约见 docs/BACKEND_REQUESTS_artifact_5期.md):
    ``{id, type:"file", kind, name, mime, size?, status, downloadUrl?, previewKind, error?}``。
    前端 chat-client 按顶层键 ``x_adapter_artifact`` 解析,按 ``id`` 累积到
    ``message.artifacts[]``(同 id 三态覆盖:generating → ready → error)。
    """
    _aid = str(artifact.get("id") or "")
    return {
        "id": f"agent-artifact-{_aid}" if _aid else "agent-artifact",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model or "adapter",
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "x_adapter_artifact": artifact,
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


# =============================================================================
# v0.4.0 D 重构:Plan 升级为 agent loop 一等公民(first-class phase)
# 不再套 ToolRegistry 接口,plan 执行是 generator,直接 yield SSE chunk。
# 详见 lxj-adapter-deploy/design/2026-05-28-D-refactor-plan-as-first-class-phase.md
# =============================================================================


def _validate_plan_steps(steps: Any) -> Optional[str]:
    """校验 LLM 提交的 plan.steps 结构。返回错误信息字符串,None 表示通过。
    容错原则:能跑就跑(如 depends_on 引用不存在的 id 当作无依赖处理 —— 在
    _topological_sort_plan_steps 里处理),只对结构性问题报错。
    v0.4.0:从 adapter.py 移过来,作为 agent loop 内部 helper。
    """
    if not isinstance(steps, list) or not steps:
        return "steps 必须是非空数组"
    if len(steps) > 12:
        return f"steps 上限 12,收到 {len(steps)}(请合并相关 step)"
    seen_ids: set[str] = set()
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return f"steps[{i}] 不是对象"
        sid = s.get("id")
        if not isinstance(sid, str) or not sid.strip():
            return f"steps[{i}].id 缺失或不是字符串"
        sid = sid.strip()
        if sid in seen_ids:
            return f"steps[{i}].id='{sid}' 重复"
        seen_ids.add(sid)
        q = s.get("question")
        if not isinstance(q, str) or not q.strip():
            return f"steps[{i}](id={sid}).question 缺失或为空"
        deps = s.get("depends_on")
        if deps is not None and not isinstance(deps, list):
            return f"steps[{i}](id={sid}).depends_on 必须是数组(可省略表示无依赖)"
    return None


def _topological_sort_plan_steps(
    steps: list[dict[str, Any]],
) -> Optional[list[list[dict[str, Any]]]]:
    """拓扑排序 plan.steps,返回 batches —— 同一 batch 内 step 互不依赖、可并行执行;
    batch 之间串行(前一 batch 全完成才开下一 batch)。检测到循环依赖时返回 None。
    depends_on 引用不存在的 id 当无依赖容错跳过。
    v0.4.0:从 adapter.py 移过来。
    """
    step_by_id = {s["id"].strip(): s for s in steps}
    in_degree: dict[str, int] = {sid: 0 for sid in step_by_id}
    dependents: dict[str, list[str]] = {sid: [] for sid in step_by_id}
    for s in steps:
        sid = s["id"].strip()
        for dep in (s.get("depends_on") or []):
            dep = dep.strip() if isinstance(dep, str) else ""
            if dep and dep in step_by_id:
                in_degree[sid] += 1
                dependents[dep].append(sid)
            # else: 引用不存在的 step id,容错跳过(不增 in_degree)
    batches: list[list[dict[str, Any]]] = []
    remaining = set(step_by_id.keys())
    while remaining:
        current_ids = [sid for sid in remaining if in_degree[sid] == 0]
        if not current_ids:
            return None  # 循环依赖
        current_batch = [step_by_id[sid] for sid in current_ids]
        batches.append(current_batch)
        for sid in current_ids:
            remaining.remove(sid)
            for dependent in dependents[sid]:
                if dependent in remaining:
                    in_degree[dependent] -= 1
    return batches


# v0.4.0:run_step 是 agentic_web.py 不知道 Excel 等具体后端时的"step 执行器"抽象。
# adapter.py 在 handler 路径通过 cfg.plan_step_runner 注入一个绑定 dataset_id 的
# callable,签名:(question: str, timeout_s: int) -> Any。
# agentic_web.py 完全 Excel-agnostic,守 AGENTS.md generic/open-source safe 边界。
PlanStepRunner = Callable[[str, int], Any]


def _execute_plan_streaming(
    plan_args: dict[str, Any],
    run_step: PlanStepRunner,
    cfg: "AgentConfig",
    model: str,
    trace: "AgentTrace",
):
    """v0.4.0 D 重构核心:plan 执行的 generator-based 一等公民实现。

    yield ("chunk", sse_dict)  ── 实时进度,直接给上层 generator forward 到 SSE client
    yield ("result", dict)     ── 最后一次,给上层组 tool_message

    设计要点(均见 design doc §4.2):
    - 不依赖 ContextVar / 不嵌套 ThreadPoolExecutor —— 纯 generator 单向数据流
    - plan_deadline 兜底:cfg.agent_plan_total_timeout 触发后,标剩余 step 为 error,
      正常 emit plan_step_end + plan_complete,正常 yield result(不抛异常)
    - 单 step 超时由 run_step 内部 deadline 保证(cfg.agent_plan_step_timeout)
    - worker daemon thread:主 generator 退出后自然 GC,不阻塞
    - 并发上限:cfg.agent_plan_parallelism 限制每批 inflight worker 数
    - trace.tool_calls 必写入:替代原 _dispatch_tool_calls_parallel 的 _run_one 写入,
      保证 Langfuse 上报完整(reviewer 反馈,见 design doc §7.5)
    """
    plan_t0 = time.time()
    plan_deadline = plan_t0 + cfg.agent_plan_total_timeout

    def _trace_write(ok: bool, *, error: Optional[str] = None, **extra: Any) -> None:
        """统一 trace.tool_calls append 入口 —— 三个 return 路径都调一次。"""
        rec: dict[str, Any] = {
            "name": "submit_analysis_plan",
            "ok": ok,
            "modality": "text",
            "elapsed_ms": int((time.time() - plan_t0) * 1000),
            "args": {
                "summary": (plan_args.get("summary") or "")[:200],
            },
        }
        if error is not None:
            rec["error"] = error
        rec.update(extra)
        trace.tool_calls.append(rec)

    # ── 1. 校验 plan 结构 ───────────────────────────────────────────
    steps_raw = plan_args.get("steps")
    err = _validate_plan_steps(steps_raw)
    if err:
        yield ("chunk", _sse_progress_chunk(
            model, "plan_validation_failed",
            f"plan 校验失败:{err}",
        ))
        _trace_write(False, error=err, step_count=0)
        yield ("result", {
            "plan_executed": False,
            "error": f"plan 校验失败:{err}",
        })
        return

    steps: list[dict[str, Any]] = [
        {**s, "id": s["id"].strip(), "question": s["question"].strip()}
        for s in steps_raw  # type: ignore[union-attr]
    ]

    # ── 2. 拓扑排序 ─────────────────────────────────────────────────
    batches = _topological_sort_plan_steps(steps)
    if batches is None:
        err_msg = "plan 含循环依赖,无法拓扑排序"
        yield ("chunk", _sse_progress_chunk(
            model, "plan_validation_failed", err_msg,
            step_ids=[s["id"] for s in steps],
        ))
        _trace_write(False, error=err_msg, step_count=len(steps))
        yield ("result", {
            "plan_executed": False,
            "error": err_msg + "。请检查 depends_on 字段。",
            "step_ids": [s["id"] for s in steps],
        })
        return

    # ── 3. emit plan_submitted ──────────────────────────────────────
    yield ("chunk", _sse_progress_chunk(
        model, "plan_submitted",
        f"已规划 {len(steps)} 步({len(batches)} 个并行批次)",
        step_count=len(steps),
        batch_count=len(batches),
        summary=plan_args.get("summary") or "",
        steps=[
            {
                "id": s["id"],
                "question": s["question"][:200],
                "depends_on": s.get("depends_on") or [],
                "rationale": (s.get("rationale") or "")[:200],
            }
            for s in steps
        ],
    ))

    # ── 4. 分批执行 ─────────────────────────────────────────────────
    results: dict[str, Any] = {}
    elapsed_per_step: dict[str, int] = {}
    aborted_by_deadline = False
    parallelism = max(1, cfg.agent_plan_parallelism)
    step_timeout = cfg.agent_plan_step_timeout

    def _worker(step: dict[str, Any], q: "queue.Queue") -> None:
        """worker:跑 run_step,完成或异常后 put 完整 result 到 queue。"""
        t0 = time.time()
        sid = step["id"]
        question = step["question"]
        try:
            r = run_step(question, step_timeout)
            ok = not (isinstance(r, dict) and r.get("error"))
            err = (r.get("error") if isinstance(r, dict) and not ok else None)
            q.put((sid, r, int((time.time() - t0) * 1000), ok, err))
        except Exception as exc:  # noqa: BLE001
            q.put((
                sid,
                {"error": f"{type(exc).__name__}: {exc}"},
                int((time.time() - t0) * 1000),
                False,
                f"{type(exc).__name__}: {exc}",
            ))

    for batch_idx, batch in enumerate(batches):
        if aborted_by_deadline:
            break

        # 4.1 emit plan_step_start —— 批量同时发 N 个,前端可立即展示 N 个 ⏳
        for s in batch:
            yield ("chunk", _sse_progress_chunk(
                model, "plan_step_start",
                f"执行 step {s['id']}",
                step_id=s["id"],
                question=s["question"][:200],
                batch_index=batch_idx,
            ))

        # 4.2 起 worker(并发上限 = parallelism;超出的进 pending 等前完成再起)
        # v0.4.2:加 step_attempts 跟踪 retry 次数 + step_t0_map 记录第一次 start
        # 时间(让 plan_step_end.elapsed_ms 包含 retry 累计时间)
        completed_q: "queue.Queue" = queue.Queue()
        in_flight = 0
        pending = list(batch)
        step_attempts: dict[str, int] = {s["id"]: 0 for s in batch}
        step_t0_map: dict[str, float] = {}
        max_step_retries = max(0, cfg.agent_plan_step_max_retries)

        while pending and in_flight < parallelism:
            s = pending.pop(0)
            step_t0_map[s["id"]] = time.time()  # v0.4.2:记 first-start 时间
            threading.Thread(
                target=_worker, args=(s, completed_q), daemon=True,
            ).start()
            in_flight += 1

        # 4.3 主 generator 边等边 yield —— plan_deadline 兜底
        finished = 0
        target = len(batch)
        while finished < target:
            remaining = plan_deadline - time.time()
            if remaining <= 0:
                # plan-level 总超时:标剩余 step 为 error,emit plan_step_end
                missing_in_batch = [s for s in batch if s["id"] not in results]
                for s in missing_in_batch:
                    sid = s["id"]
                    err_msg = f"plan 总超时 ({cfg.agent_plan_total_timeout}s)"
                    results[sid] = {"error": err_msg}
                    elapsed_per_step[sid] = int((time.time() - plan_t0) * 1000)
                    yield ("chunk", _sse_progress_chunk(
                        model, "plan_step_end",
                        f"step {sid} 因 plan 总超时未执行",
                        step_id=sid,
                        elapsed_ms=elapsed_per_step[sid],
                        ok=False,
                        error="plan_total_timeout",
                        attempts=step_attempts.get(sid, 0) + 1,
                    ))
                # 同样标 pending(后续 batch 不会跑)
                aborted_by_deadline = True
                break

            try:
                sid, r, elapsed_ms, ok, err_msg = completed_q.get(
                    timeout=min(remaining, 1.0)
                )
            except queue.Empty:
                continue  # 还没完成,回到 while 顶检查 deadline

            # ── v0.4.2 step retry:失败且还有配额 → resubmit 同 step ──
            # 不触发:ok=True 或 retry 配额用完(走到下方正常完成路径)
            # v0.4.5:timeout 类失败**不 retry** —— retry 同样会撞 EXCEL_QUERY_TIMEOUT,
            # 只把 240s 白翻成 480s(截图实测:利润查询 step 超时后 retry 又超时)。
            # 只对 excel-poc 瞬时错误(HTTP 5xx 等非 timeout)retry。
            # 只认 Python socket 超时的固定串 "timed out"(TimeoutError/socket.timeout);
            # 不用宽泛 "timeout" 子串——否则 excel-poc 返回的 504 body(如 "Gateway Timeout")
            # 会被误判为超时跳过 retry,而那本是该 retry 的瞬时错误(reviewer P1)。
            _is_timeout = bool(err_msg) and "timed out" in str(err_msg).lower()
            if not ok and not _is_timeout and step_attempts[sid] < max_step_retries:
                step_attempts[sid] += 1
                yield ("chunk", _sse_progress_chunk(
                    model, "plan_step_retrying",
                    f"step {sid} 失败(第 {step_attempts[sid]} 次尝试),自动 retry",
                    step_id=sid,
                    attempt=step_attempts[sid] + 1,  # 即将开始的尝试编号(2-based)
                    max_attempts=max_step_retries + 1,
                    previous_error=str(err_msg)[:200] if err_msg else None,
                    previous_elapsed_ms=elapsed_ms,
                ))
                # resubmit 同 step,in_flight 不变(worker 资源还在用)
                # 不更新 step_t0_map[sid]:elapsed_ms 累计第一次 start 到最终完成
                same_step = next(x for x in batch if x["id"] == sid)
                threading.Thread(
                    target=_worker, args=(same_step, completed_q), daemon=True,
                ).start()
                continue  # 不算 finished,继续 while

            # 正常完成(ok=True)或 retry 配额用完(ok=False)
            # v0.4.2:elapsed_ms 从 step_t0_map 算累计(含 retry 等待时间)
            total_elapsed_ms = int(
                (time.time() - step_t0_map[sid]) * 1000
            ) if sid in step_t0_map else elapsed_ms
            results[sid] = r
            elapsed_per_step[sid] = total_elapsed_ms
            attempt_count = step_attempts[sid] + 1  # 1-based 总尝试次数
            yield ("chunk", _sse_progress_chunk(
                model, "plan_step_end",
                f"step {sid} {'完成' if ok else '失败'} "
                f"({total_elapsed_ms}ms"
                + (f",尝试 {attempt_count} 次" if attempt_count > 1 else "")
                + ")",
                step_id=sid,
                elapsed_ms=total_elapsed_ms,
                ok=ok,
                error=err_msg,
                attempts=attempt_count,
            ))
            finished += 1
            in_flight -= 1

            # 释放并发,起下一个 pending(若有)
            if pending and in_flight < parallelism:
                s = pending.pop(0)
                step_t0_map[s["id"]] = time.time()  # v0.4.2:记 first-start
                threading.Thread(
                    target=_worker, args=(s, completed_q), daemon=True,
                ).start()
                in_flight += 1

    # ── 5. plan_complete + trace 写入 + 返回 result ─────────────────
    total_ms = int((time.time() - plan_t0) * 1000)
    failed_step_ids = [
        sid for sid, r in results.items()
        if isinstance(r, dict) and r.get("error")
    ]
    plan_ok = (len(failed_step_ids) == 0) and (not aborted_by_deadline)

    yield ("chunk", _sse_progress_chunk(
        model, "plan_complete",
        f"plan 完成 ({total_ms}ms,{len(steps)} 步,{len(failed_step_ids)} 失败"
        + (",总超时中止" if aborted_by_deadline else "") + ")",
        total_elapsed_ms=total_ms,
        step_count=len(steps),
        failed_count=len(failed_step_ids),
        failed_step_ids=failed_step_ids,
        aborted=aborted_by_deadline,
    ))

    _trace_write(
        plan_ok,
        step_count=len(steps),
        batch_count=len(batches),
        failed_step_count=len(failed_step_ids),
        failed_step_ids=failed_step_ids,
        aborted_by_total_timeout=aborted_by_deadline,
    )

    yield ("result", {
        "plan_executed": not aborted_by_deadline,
        "step_count": len(steps),
        "batch_count": len(batches),
        "total_elapsed_ms": total_ms,
        "results": [
            {
                "step_id": s["id"],
                "question": s["question"],
                "rationale": s.get("rationale", ""),
                "depends_on": s.get("depends_on") or [],
                "elapsed_ms": elapsed_per_step.get(s["id"], 0),
                "result": results.get(s["id"], {"error": "未执行"}),
            }
            for s in steps
        ],
        "failed_step_count": len(failed_step_ids),
        "failed_step_ids": failed_step_ids,
        "aborted_by_total_timeout": aborted_by_deadline,
    })


def _speculative_iteration(
    cfg: AgentConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
    tool_choice: Any = None,
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
        for chunk in _stream_upstream(cfg, messages, tools, extra, tool_choice=tool_choice):
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
                # v0.2.27 thinking 死循环防护:reasoning_buf 累积超 max_reasoning_chars
                # 主动 abort。Qwen3 thinking + Int8 量化在简单问题上易陷入 "Final →
                # Wait → keep → Final" 自我质疑循环;normal 推理 < 4k token (16k char)
                # 够用,> 此值大概率已死循环。abort 后通过 "empty" path 走 reasoning
                # 兜底,前端看到"思考过程过长,请换个简单点的问法"。
                if (
                    cfg.max_reasoning_chars > 0
                    and len(reasoning_buf) > cfg.max_reasoning_chars
                    and not content_started
                    and not tool_calls_started
                ):
                    finish_reason = "reasoning_too_long"
                    break  # 跳出 stream loop,后续按 path=empty 路径走 synthesis 兜底

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
    ct["thinking"] = False  # 🔴 K2.6/vLLM0.18 用 thinking;enable_thinking 是 Qwen 旧名,对 K2.6 静默失效(已实测)——只塞 enable_thinking 时 adapter「关 thinking」一直没生效
    out["chat_template_kwargs"] = ct
    out["enable_thinking"] = False
    out["thinking"] = False
    return out


def _client_wants_thinking(extra: Optional[dict[str, Any]]) -> bool:
    """v0.2.22:看 client 在 extra payload 里有没有显式要求 thinking on。

    前端「深度思考」chip 通过 ``extra_body.chat_template_kwargs.enable_thinking``
    或顶层 ``enable_thinking`` 表达意图(双 key 兼容老/新 Qwen 模板);两者任一
    为 True 视为「开」。其他情况(False / 未设置 / 类型不对)视为「关」。
    """
    if not extra:
        return False
    # 🔴 前端 v0.9.0/0.16.x 起发的是 K2.6 字段 `thinking`(不再是 `enable_thinking`);
    #    两个 key 都认,否则用户开了深度思考 chip 这里检测不到、被静默关掉。
    ct = extra.get("chat_template_kwargs")
    if isinstance(ct, dict) and (ct.get("enable_thinking") is True or ct.get("thinking") is True):
        return True
    return extra.get("enable_thinking") is True or extra.get("thinking") is True


def _build_iteration_extra(
    cfg: AgentConfig,
    is_last: bool,
    base_extra: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """v0.2.17 / v0.2.22:按 cfg 的 thinking 策略,给每轮上游调用注入开关。

    - intermediate: 始终注入 ``enable_thinking=False``(``cfg.intermediate_thinking_enabled``
      可改 True,但 99% 场景应该保持 False)。
    - force_answer: 看 ``cfg.force_answer_thinking_enabled``:
        - None(default,passthrough):**尊重 client** —— 前端 chip 开 → 保留 True、
          关/未指定 → 注入 False
        - True/False:硬覆盖,忽略 client 意图
    """
    if is_last:
        mode = cfg.force_answer_thinking_enabled
        if mode is None:
            keep = _client_wants_thinking(base_extra)
        else:
            keep = mode
    else:
        keep = cfg.intermediate_thinking_enabled

    if keep:
        # 保留 thinking on:确保 ``enable_thinking=True`` 双 key 都设上,client 没
        # 设过但 cfg 硬开的场景(罕见)也能跑通。已设过的会被覆盖为 True(等价)。
        out = dict(base_extra or {})
        ct = dict(out.get("chat_template_kwargs") or {})
        ct["enable_thinking"] = True
        ct["thinking"] = True  # K2.6/vLLM0.18 字段(enable_thinking 对 K2.6 失效)
        out["chat_template_kwargs"] = ct
        out["enable_thinking"] = True
        out["thinking"] = True
        return out
    # 关 thinking:用现有的 _build_no_thinking_extra(确保双 key=False)
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
    # v0.2.30 streaming path intent-leak 续轮兜底状态(per-session):
    # - intent_leak_retries_used: 已续轮次,封顶 cfg.max_intent_leak_retries
    # - force_tool_choice_next: 上一轮命中 intent leak → 下一轮强制 tool_choice
    #   (一次性,触发后立即清,防模型连续两轮都"只说不做")
    intent_leak_retries_used = 0
    force_tool_choice_next = False

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

    # v0.4.0:删除 ACTIVE_PROGRESS_CB.set —— plan 执行 generator 直接 yield 传 chunk。

    # v0.3.3 D Phase 4:plan-and-execute 模式 per-session flag。
    # 第一次 dispatch 完 submit_analysis_plan 后置 True;之后所有 iteration
    # 都强制 tools=[] + PLAN_SYNTHESIS_HINT,LLM 看不到 submit_analysis_plan
    # 这个 tool,架构上不可能再调一次 plan(修 v0.3.2 实测复现的 "iteration 2
    # LLM 又 emit plan" silent loop)。
    plan_dispatch_done = False
    # v0.5.0 B / v0.6.0 B+:文件生成同款 per-session flag。第一次拦截执行完任一
    # generate_* 后置 True;之后所有 iteration 强制 tools=[] + FILE_GEN_SYNTHESIS_HINT,
    # 模型看不到生成工具,架构上不可能再生成一次,只能用一句话收尾(一次请求出一份文件)。
    file_gen_dispatch_done = False

    def _run_attempt(msgs, current_tools, extra, tool_choice=None):
        """Forward chunks from one _speculative_iteration to client; capture state.

        Returns the final state dict (path/content_buf/reasoning_buf/...) via
        the special ('state', dict) sentinel. Caller iterates and forwards.

        ``tool_choice`` 透到 upstream payload。None = 默认 "auto"。
        v0.2.25:Excel 第一轮传 ``{"type":"function","function":{"name":"excel_query"}}``。
        """
        for kind, payload in _speculative_iteration(
            cfg, msgs, current_tools, extra, tool_choice=tool_choice,
        ):
            if kind == "chunk":
                yield ("chunk", payload)
            elif kind == "done":
                yield ("state", payload)
                return

    for iteration in range(1, cfg.max_iterations + 1):
        is_last = iteration == cfg.max_iterations
        # v0.3.3 D Phase 4:plan 模式下 dispatch 完成后,后续轮强制综合作答
        # (tools=[] + PLAN_SYNTHESIS_HINT),架构上消除 "LLM 再调一次 plan"
        # 的 silent loop。检测条件:enable_plan_and_execute + plan_dispatch_done
        # + 不是 force_answer 轮(后者本来就 tools=[],PLAN hint 也适用)。
        plan_synthesis_now = cfg.enable_plan_and_execute and plan_dispatch_done
        # v0.5.0 B / v0.6.0 B+:文件生成完成后同样强制收尾(tools=[] + FILE_GEN hint)。
        file_gen_synthesis_now = cfg.enable_file_gen and file_gen_dispatch_done
        if is_last or plan_synthesis_now or file_gen_synthesis_now:
            current_tools: list[dict[str, Any]] = []
            # v0.2.19: 合并 hint 进头部 system,不在末尾另开一条 ——
            # EAS upstream 严校验「System message must be at the beginning」。
            if file_gen_synthesis_now:
                hint_text = FILE_GEN_SYNTHESIS_HINT
            elif plan_synthesis_now:
                hint_text = PLAN_SYNTHESIS_HINT
            else:
                hint_text = FORCE_ANSWER_SYSTEM_HINT
            current_messages = _inject_force_answer_hint(augmented, hint_text)
            # v0.2.24 plan B(Anthropic 模式):rewrite history 消除 tool_call JSON 痕迹,
            # 让 Qwen3.5 在 force_answer 时更可能激活 thinking(尤其 chip ON 场景)。
            # plan / file_gen synthesis 路径不走 rewrite —— 结果已经在 tool message 里
            # 结构化清晰,改写反而丢信息。
            rewrote_history = False
            if (
                is_last
                and cfg.force_answer_rewrite_history
                and not plan_synthesis_now
                and not file_gen_synthesis_now
            ):
                before_count = len(current_messages)
                current_messages = _rewrite_history_for_force_answer(current_messages)
                rewrote_history = len(current_messages) != before_count
            if file_gen_synthesis_now:
                progress_cb(
                    "agent_file_gen_synthesis",
                    f"文件已生成,本轮强制一句话收尾(iter {iteration})",
                    {"iteration": iteration},
                )
            elif plan_synthesis_now:
                progress_cb(
                    "agent_plan_synthesis",
                    f"plan 已 dispatch,本轮强制基于 plan results 综合作答(iter {iteration})",
                    {"iteration": iteration},
                )
            else:
                progress_cb(
                    "agent_force_answer",
                    "最后一轮，强制模型作答",
                    {"iteration": iteration, "history_rewritten": rewrote_history},
                )
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
        # v0.2.25 L1:Excel 模式第一轮强制 tool_choice 指向 excel_query,堵住
        # Qwen3.5 偶发的"describe but don't call"退化路径。后续轮回 "auto"
        # 让模型能从工具结果走 final answer 文本路径。force_answer(is_last)
        # tools=[] 时 tool_choice 不生效,无需特判。
        iter_tool_choice: Any = None
        # v0.2.30:除首轮外,intent-leak 续轮也强制 tool_choice —— 防模型连续两轮
        # 都"只说不做"(实测:第 1 轮探表 → 第 2 轮 leak → 第 3 轮若不 force 又
        # 是 leak)。force_tool_choice_next 是 path=="content" intent leak 分支
        # 置位的一次性 flag,触发后立即清。
        _force_this_turn = iteration == 1 or force_tool_choice_next
        if (
            _force_this_turn
            and not is_last
            and cfg.force_first_tool_name
            and current_tools
            and any(
                (t.get("function") or {}).get("name") == cfg.force_first_tool_name
                for t in current_tools
            )
        ):
            iter_tool_choice = {
                "type": "function",
                "function": {"name": cfg.force_first_tool_name},
            }
            if force_tool_choice_next:
                _force_msg = (
                    f"intent-leak 续轮:强制调用 {cfg.force_first_tool_name}"
                    f"(已续 {intent_leak_retries_used} 次)"
                )
                force_tool_choice_next = False  # 一次性,用完立即清
            else:
                _force_msg = (
                    f"首轮强制调用 {cfg.force_first_tool_name}(防模型仅叙述不调工具)"
                )
            progress_cb(
                "agent_first_turn_tool_forced",
                _force_msg,
                {"iteration": iteration, "tool_name": cfg.force_first_tool_name},
            )
            yield from _drain_queue()
        t0 = time.time()
        state: dict[str, Any] = {}
        for kind, payload in _run_attempt(
            current_messages, current_tools, iter_extra, tool_choice=iter_tool_choice,
        ):
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
            # v0.2.25:retry 沿用主尝试的 tool_choice —— 若第一轮强制了 excel_query,
            # retry 也强制(否则 retry 又退回 auto,L1 的 enforcement 被绕过)。
            for kind, payload in _run_attempt(
                current_messages, current_tools, retry_extra, tool_choice=iter_tool_choice,
            ):
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

            # ── v0.2.30 intent-leak 续轮兜底 ───────────────────────────────
            # Qwen3.5 偶发"流出 content 全是'我将调用 excel_query...'计划叙述、
            # tool_calls 全空"的 silent failure。EXCEL_AGENT_SYSTEM_PROMPT
            # v0.2.29 已经在 prompt 层严禁,实测仍 4 次 2 次复现 → 架构兜底。
            # 复用 final-message 兜底已用的 _looks_like_intent_not_answer /
            # _ends_with_dangling_intent 检测器(行 1481 / 1520),命中 + 有
            # 迭代预算时 emit 分隔符 + 续一轮(下一轮也 force tool_choice)。
            # 已流给前端的字撤不回,但用户能看到真答案接在分隔符后。
            looks_intent = (
                _looks_like_intent_not_answer(content_buf)
                or _ends_with_dangling_intent(content_buf)
            )
            can_intent_retry = (
                looks_intent
                and not is_last
                and intent_leak_retries_used < cfg.max_intent_leak_retries
                and iteration < cfg.max_iterations - 1  # 留至少 1 轮给真 answer
                # v0.3.3 D Phase 4 修 false-positive:plan dispatch 完后,LLM 在
                # tools=[] + PLAN_SYNTHESIS_HINT 路径下输出综合作答的开头
                # ("我将分析以下数据...")会被 _looks_like_intent_not_answer
                # 误判为 intent leak。但此时本来就该返回 content,无 tool 可调,
                # 续轮没意义且浪费 LLM 调用。强制跳过 intent guard。
                and not plan_dispatch_done
            )
            if can_intent_retry:
                intent_leak_retries_used += 1
                force_tool_choice_next = True  # 下一轮强制 tool_choice(若配置了)
                progress_cb(
                    "agent_intent_leak_recovering",
                    f"检测到上一段只是计划叙述、未触发工具调用,续一轮强制执行(第 {intent_leak_retries_used} 次)",
                    {
                        "iteration": iteration,
                        "content_chars": len(content_buf),
                        "retries_used": intent_leak_retries_used,
                    },
                )
                yield from _drain_queue()
                # 流一个可视分隔符:让用户知道上文是计划、下文才是真答案
                divider = (
                    "\n\n---\n\n_（上文仅为模型的计划描述,未触发实际查询;"
                    "正在继续执行真正的工具调用…）_\n\n"
                )
                yield {
                    "id": "agent-intent-leak-divider",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": divider},
                        "finish_reason": None,
                    }],
                }
                # 把 leak content 作为 assistant message append,加 system 纠正 hint。
                # v0.3.2 修 bug:hint 是 system message,不能加在末尾(EAS 严校验
                # "System message must be at the beginning",撞 HTTP 400)。复用
                # _inject_force_answer_hint 把 hint 拼到头部 system,避开 EAS 校验。
                # v0.2.30 原写法 `augmented + [leak_assistant_msg, leak_recover_hint]`
                # 在续轮触发时会必然 400,实测 v0.3.1 Phase 3 trace 复现。
                leak_assistant_msg = {"role": "assistant", "content": content_buf}
                leak_recover_hint_text = (
                    "【纠正 — v0.2.30 intent-leak guard】你上一条 assistant 消息"
                    "只用自然语言宣告了下一步动作,**没有真正 emit tool_calls** ——"
                    "这正是系统提示词反复禁止的最严重 bug,等于浪费一轮 budget。\n"
                    "本轮你必须立刻 emit 真正的 tool_calls(在 assistant.tool_calls "
                    "数组里),不要再写'我将...''接下来...''下面我来...'之类的"
                    "叙述。content 必须是空字符串,工具调用必须用结构化协议发出。"
                )
                augmented = _inject_force_answer_hint(
                    augmented + [leak_assistant_msg], leak_recover_hint_text,
                )
                continue

            # ── v0.4.1 修问题 3:intent-leak retry 配额用完仍 leak → 合成兜底 ─
            # 前情:v0.2.30 intent-leak guard 续轮 1 次后,如果模型再次输出
            # intent narrative(没真 emit tool_calls),原代码直接 fall through
            # 到 line 3158 把"第二次 leak content"当 final answer 返回,
            # 用户看到的就是"分隔符之后还是计划描述,没有真答案" —— 协议描述
            # 「真答案以 content delta 接续流出」承诺不兑现。
            #
            # 修法:复用现有 _synthesize_answer 走合成兜底(参考 line 3146
            # 的 force_answer_fallback 模式)。把已有工具结果交给 LLM 在
            # tool-free 环境下硬作答,产出真正的结论 prose。
            #
            # 已经流出去的两次 leak content 撤不回,但在它们后面接一个分隔符
            # 告知用户"模型仍未直接作答,已强制合成"+ synthesis 真答案。
            # 用户看到:[leak 1] [续轮分隔符] [leak 2] [合成提示分隔符] [真答案]
            #
            # 协议事件用现有 `agent_force_answer_fallback`(前端已 handle),
            # 区分用 message 文本,不引入新 stage。
            elif (
                looks_intent
                and not is_last
                and intent_leak_retries_used >= cfg.max_intent_leak_retries
                # 跟 can_intent_retry 同样排除 plan_dispatch_done 的 false-positive:
                # plan synthesis 路径下 LLM 输出综合作答开头会被误判为 intent,
                # 不该走兜底(直接 return 即可,内容本身就是答案)
                and not plan_dispatch_done
            ):
                progress_cb(
                    "agent_force_answer_fallback",
                    "intent-leak retry 配额已用完,模型仍未给出真答案,走合成兜底",
                    {
                        "iteration": iteration,
                        "retries_used": intent_leak_retries_used,
                        "content_chars": len(content_buf),
                        "reason": "intent_leak_exhausted",  # 给 Langfuse / 前端区分
                    },
                )
                yield from _drain_queue()
                # 流一个分隔符:把"上面是 leak / 下面是合成真答案"视觉区分
                synth_notice = (
                    "\n\n---\n\n_（模型多轮仍未直接作答,adapter 已基于已有工具"
                    "结果强制合成最终结论:）_\n\n"
                )
                yield {
                    "id": "agent-intent-leak-synth-notice",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": synth_notice},
                        "finish_reason": None,
                    }],
                }
                # 调合成(非流式,LLM 在 tool-free 环境下基于工具结果硬作答)
                synthesized = _synthesize_answer(cfg, augmented, extra_payload, trace)
                if not synthesized:
                    synthesized = (
                        "（抱歉,模型在续轮后仍未给出有效答案,合成兜底也失败。"
                        "请重试或换一种问法。）"
                    )
                    trace.stopped_reason = "answered_empty_fallback"
                else:
                    trace.stopped_reason = "answered_synthesized"
                trace.final_finish_reason = "stop"
                fake_resp = {"id": "agent-intent-leak-synth", "created": int(time.time())}
                yield from _stream_final_answer(model, synthesized, fake_resp)
                yield _sse_trace_chunk(model, trace)
                return

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
                # ── v0.4.0 D 重构:plan tool_call 走 _execute_plan_streaming
                #    一等公民 generator,不再套 _dispatch_tool_calls_parallel ──
                is_plan_call = (
                    cfg.enable_plan_and_execute
                    and cfg.plan_step_runner is not None
                    and len(assembled) == 1
                    and (assembled[0].get("function") or {}).get("name")
                        == "submit_analysis_plan"
                )
                if is_plan_call:
                    plan_tc = assembled[0]
                    plan_tc_id = plan_tc.get("id", "") or "tc_plan"
                    plan_fn = plan_tc.get("function") or {}
                    plan_args_raw = plan_fn.get("arguments") or "{}"
                    try:
                        plan_args = (
                            json.loads(plan_args_raw)
                            if isinstance(plan_args_raw, str)
                            else (plan_args_raw or {})
                        )
                        plan_parse_err: Optional[str] = None
                    except (ValueError, TypeError, json.JSONDecodeError) as exc:
                        plan_args = {}
                        plan_parse_err = (
                            f"plan args 非合法 JSON:{type(exc).__name__}: {exc}"
                        )

                    # ── 必做兼容事件 #1:tools_dispatch + tool_start ──
                    # 前端"执行步骤"UI 依赖工具行视觉锚点(reviewer 反馈,设计 §7.3)
                    plan_steps_count = (
                        len(plan_args.get("steps") or [])
                        if isinstance(plan_args, dict) else 0
                    )
                    plan_summary = (
                        plan_args.get("summary") or ""
                        if isinstance(plan_args, dict) else ""
                    )
                    args_preview = (
                        plan_summary
                        if plan_summary
                        else f"已规划 {plan_steps_count} 步"
                    )[:200]
                    progress_cb(
                        "tools_dispatch",
                        f"模型请求 1 个工具调用",
                        {
                            "count": 1,
                            "iteration": iteration,
                            "tool_calls": [{
                                "tool_call_id": plan_tc_id,
                                "name": "submit_analysis_plan",
                                "args_preview": args_preview,
                            }],
                        },
                    )
                    progress_cb(
                        "tool_start",
                        "调用 submit_analysis_plan",
                        {
                            "tool_call_id": plan_tc_id,
                            "name": "submit_analysis_plan",
                            "args": {
                                "summary": plan_summary[:200],
                                "step_count": plan_steps_count,
                            },
                        },
                    )
                    yield from _drain_queue()

                    # ── 直接消费 generator,chunk 实时 yield 给 client ──
                    plan_t0 = time.time()
                    plan_result: Optional[dict[str, Any]] = None
                    if plan_parse_err:
                        # JSON 解析失败:不调 _execute_plan_streaming(它的校验是
                        # 结构层,JSON 解析是更外层),直接 emit validation failed
                        # 并组 error result
                        progress_cb(
                            "plan_validation_failed", plan_parse_err,
                            {"phase": "json_parse"},
                        )
                        yield from _drain_queue()
                        plan_result = {
                            "plan_executed": False,
                            "error": plan_parse_err,
                        }
                        trace.tool_calls.append({
                            "name": "submit_analysis_plan", "ok": False,
                            "modality": "text",
                            "elapsed_ms": int((time.time() - plan_t0) * 1000),
                            "error": plan_parse_err, "step_count": 0,
                        })
                    else:
                        for kind, payload in _execute_plan_streaming(
                            plan_args, cfg.plan_step_runner, cfg, model, trace,
                        ):
                            if kind == "chunk":
                                yield payload  # ← 实时流给 SSE client
                            elif kind == "result":
                                plan_result = payload
                        if plan_result is None:
                            plan_result = {
                                "plan_executed": False,
                                "error": "_execute_plan_streaming 未 yield result",
                            }

                    # ── 必做兼容事件 #2:tool_end ─────────────────────
                    plan_elapsed = int((time.time() - plan_t0) * 1000)
                    plan_ok = bool(plan_result and plan_result.get("plan_executed"))
                    progress_cb(
                        "tool_end",
                        f"submit_analysis_plan 完成 ({plan_elapsed}ms)",
                        {
                            "tool_call_id": plan_tc_id,
                            "name": "submit_analysis_plan",
                            "elapsed_ms": plan_elapsed,
                            "ok": plan_ok,
                            "modality": "text",
                        },
                    )
                    yield from _drain_queue()

                    # plan tool_call + tool_message 进 history,进下一轮 synthesis
                    assistant_message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [plan_tc],
                    }
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": plan_tc_id,
                        "content": json.dumps(plan_result, ensure_ascii=False),
                    }
                    augmented = augmented + [assistant_message, tool_message]
                    plan_dispatch_done = True
                    continue

                # ── v0.5.0 B / v0.6.0 B+:file_gen 拦截 → 渲染→上传→emit x_adapter_artifact ──
                # 同 plan 架构:inline 拦截(register_schema_only,不走 dispatch)。模型只
                # 产出结构化内容(A 铁律),cfg.file_renderer 按 tool name 路由到写死的渲染/
                # 存储。取 assembled 里**第一个** generate_*(模型偶发并发 emit 多个时,渲染
                # 第一个、忽略其余 —— 一次请求出一份文件;register_schema_only 下其余也无
                # impl 可跑)。tool name → kind/ext/mime/preview 由 FILE_GEN_TOOL_META 定。
                _file_tc_found = (
                    next(
                        (tc for tc in assembled
                         if (tc.get("function") or {}).get("name") in FILE_GEN_TOOL_NAMES),
                        None,
                    )
                    if (cfg.enable_file_gen and cfg.file_renderer is not None)
                    else None
                )
                if _file_tc_found is not None:
                    file_tc = _file_tc_found
                    file_tc_id = file_tc.get("id", "") or "tc_file"
                    file_fn = file_tc.get("function") or {}
                    file_tool_name = file_fn.get("name") or ""
                    fmeta = FILE_GEN_TOOL_META.get(file_tool_name, {})
                    f_kind = fmeta.get("kind", "other")
                    f_ext = fmeta.get("ext", "bin")
                    f_mime = fmeta.get("mime", "application/octet-stream")
                    f_default_name = fmeta.get("default_name", "文件")
                    f_preview = fmeta.get("preview", "none")
                    file_args_raw = file_fn.get("arguments") or "{}"
                    try:
                        file_args = (
                            json.loads(file_args_raw)
                            if isinstance(file_args_raw, str)
                            else (file_args_raw or {})
                        )
                        if not isinstance(file_args, dict):
                            file_args = {}
                        file_parse_err: Optional[str] = None
                    except (ValueError, TypeError, json.JSONDecodeError) as exc:
                        file_args = {}
                        file_parse_err = f"内容 args 非合法 JSON:{type(exc).__name__}: {exc}"

                    artifact_id = uuid.uuid4().hex
                    prov_title = (
                        file_args["title"].strip()
                        if isinstance(file_args.get("title"), str) else ""
                    )
                    prov_name = ((prov_title or f_default_name)[:60]) + "." + f_ext
                    # 内容条目数(progress + 收尾措辞用);各类型取最有意义的列表长度。
                    count_hint = 0
                    for _ck in ("slides", "rows", "sections", "sheets"):
                        _cv = file_args.get(_ck)
                        if isinstance(_cv, list):
                            count_hint = len(_cv)
                            break

                    # 兼容事件:tools_dispatch + tool_start(前端"执行步骤"UI 锚点)
                    progress_cb(
                        "tools_dispatch",
                        "模型请求 1 个工具调用",
                        {
                            "count": 1,
                            "iteration": iteration,
                            "tool_calls": [{
                                "tool_call_id": file_tc_id,
                                "name": file_tool_name,
                                "args_preview": (prov_title or f_default_name)[:200],
                            }],
                        },
                    )
                    progress_cb(
                        "tool_start",
                        f"调用 {file_tool_name}",
                        {
                            "tool_call_id": file_tc_id,
                            "name": file_tool_name,
                            "args": {"title": prov_title[:120], "items": count_hint},
                        },
                    )
                    yield from _drain_queue()

                    # 1) 先 emit generating 占位卡(渲染/上传可能耗时数秒)
                    yield _sse_artifact_chunk(model, {
                        "id": artifact_id,
                        "type": "file",
                        "kind": f_kind,
                        "name": prov_name,
                        "mime": f_mime,
                        "status": "generating",
                        "previewKind": f_preview,
                    })

                    # 2) 渲染 + 上传(注入的 file_renderer;agentic_web 不知渲染/OSS 细节)
                    file_t0 = time.time()
                    if file_parse_err:
                        render_result = {"ok": False, "error": file_parse_err, "name": prov_name}
                    else:
                        try:
                            render_result = cfg.file_renderer(file_tool_name, file_args, artifact_id)
                            if not isinstance(render_result, dict):
                                render_result = {"ok": False, "error": "renderer 返回非 dict", "name": prov_name}
                        except Exception as exc:  # noqa: BLE001 — 渲染失败不能崩 loop
                            render_result = {
                                "ok": False,
                                "error": f"{type(exc).__name__}: {exc}",
                                "name": prov_name,
                            }
                    file_elapsed = int((time.time() - file_t0) * 1000)
                    ok = bool(render_result.get("ok"))

                    # 3) emit ready / error(同 id 覆盖)
                    if ok:
                        final_name = render_result.get("name") or prov_name
                        artifact_ready: dict[str, Any] = {
                            "id": artifact_id,
                            "type": "file",
                            "kind": f_kind,
                            "name": final_name,
                            "mime": render_result.get("mime") or f_mime,
                            "status": "ready",
                            "downloadUrl": render_result.get("download_url"),
                            "previewKind": f_preview,
                        }
                        if render_result.get("size") is not None:
                            artifact_ready["size"] = render_result["size"]
                        yield _sse_artifact_chunk(model, artifact_ready)
                        tool_result: dict[str, Any] = {
                            "ok": True,
                            "name": final_name,
                            "items": count_hint,
                            "note": "文件已生成并展示在对话下方文件卡,用户可直接下载。",
                        }
                    else:
                        err_msg = str(render_result.get("error") or "生成失败")
                        yield _sse_artifact_chunk(model, {
                            "id": artifact_id,
                            "type": "file",
                            "kind": f_kind,
                            "name": render_result.get("name") or prov_name,
                            "mime": f_mime,
                            "status": "error",
                            "previewKind": f_preview,
                            "error": err_msg,
                        })
                        tool_result = {"ok": False, "error": err_msg}

                    trace.tool_calls.append({
                        "name": file_tool_name,
                        "ok": ok,
                        "modality": "file",
                        "elapsed_ms": file_elapsed,
                    })
                    progress_cb(
                        "tool_end",
                        f"{file_tool_name} 完成 ({file_elapsed}ms)",
                        {
                            "tool_call_id": file_tc_id,
                            "name": file_tool_name,
                            "elapsed_ms": file_elapsed,
                            "ok": ok,
                            "modality": "file",
                        },
                    )
                    yield from _drain_queue()

                    # tool_call + tool_message 进 history;下一轮 file_gen_synthesis_now
                    # 强制 tools=[] + FILE_GEN_SYNTHESIS_HINT,模型一句话收尾。
                    assistant_message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [file_tc],
                    }
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": file_tc_id,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                    augmented = augmented + [assistant_message, tool_message]
                    file_gen_dispatch_done = True
                    continue

                # ── 非 plan tool_call:走原 _dispatch_tool_calls_parallel ──
                assistant_message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": assembled,
                }
                # v0.2.20: 带每个工具的 id + name + 摘要参数,让前端在 dispatch
                # 那一刻就能预渲染 N 个 pending step 占位(状态条),后续的
                # tool_start / tool_end(也带 tool_call_id)按 id 配对推进状态。
                tc_meta = [
                    {
                        "tool_call_id": tc.get("id", "") or f"tc_{i}",
                        "name": (tc.get("function") or {}).get("name", ""),
                        "args_preview": _tool_args_preview(tc),
                    }
                    for i, tc in enumerate(assembled)
                ]
                progress_cb(
                    "tools_dispatch",
                    f"模型请求 {len(assembled)} 个工具调用",
                    {
                        "count": len(assembled),
                        "iteration": iteration,
                        "tool_calls": tc_meta,
                    },
                )
                yield from _drain_queue()
                tool_messages = _dispatch_tool_calls_parallel(
                    assembled, registry, cfg, progress_cb, trace, seen_signatures,
                    sources_cb=sources_cb,
                )
                yield from _drain_queue()
                augmented = augmented + [assistant_message] + tool_messages
                # v0.3.3 → v0.4.0:plan 模式下,若 LLM 通过非 plan 路径
                # 误调 submit_analysis_plan(理论上 register_schema_only 已防,
                # 但兜底),仍标 plan_dispatch_done 防止 silent loop。
                if (
                    cfg.enable_plan_and_execute
                    and any(
                        (tc.get("function") or {}).get("name") == "submit_analysis_plan"
                        for tc in assembled
                    )
                ):
                    plan_dispatch_done = True
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
