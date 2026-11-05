"""上下文压缩与历史检索模块。

职责：
1. 估算会话上下文 Token 用量（不引入 tiktoken / 向量库，用简单的启发式估算）；
2. 把原始上下文归档保存为 .agents/context/ 下的 JSONL 文件（每条一行 JSON）；
3. 提供 recallContext 的关键词检索：在归档里按文本关键词查找，返回片段、编号与位置；
4. 提供上下文压缩专用 Prompt，供 /compact 流程调用 LLM 生成摘要。

设计原则：尽量简单、可读，方便学生理解与维护。
"""

from __future__ import annotations

import json
import os
import re
import time

# 与 skills.py 一致：以程序启动时的工作目录作为项目根目录
PROJECT_ROOT = os.path.abspath(os.getcwd())
CONTEXT_DIR = os.path.join(PROJECT_ROOT, ".agents", "context")

# 默认上下文窗口：128k = 128 * 1024 = 131072 tokens
DEFAULT_CONTEXT_WINDOW = 128 * 1024

# 已知模型的上下文窗口（tokens）。不在表内的一律回退到默认 128k，并提示「默认」。
KNOWN_MODEL_WINDOWS = {
    "deepseek-chat": 128 * 1024,      # DeepSeek-V3
    "deepseek-reasoner": 128 * 1024,  # DeepSeek-R1
}

# 触发压缩警告的上下文占用比例
COMPACT_THRESHOLD = 0.70

# recallContext 检索结果的长度控制
RECALL_SNIPPET_CHARS = 600    # 单个片段最长字符数
RECALL_MAX_TOTAL_CHARS = 4000  # 一次检索返回的总长度上限

# 中日韩字符（按 1 字符 ≈ 1 token 估算），其余按单词 ≈ 1.3 token 估算
_CJK_PATTERN = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿"
    r"豈-﫿가-힯＀-￯]"
)


# ─── Token 估算 ───


def estimate_tokens(text) -> int:
    """粗略估算一段文本的 token 数。

    中文等 CJK 字符约 1 token/字，英文等按单词约 1.3 token/词。
    这不是精确值，仅用于监控上下文占用，够用且无需额外依赖。
    """
    if not text:
        return 0
    text = str(text)
    cjk_chars = len(_CJK_PATTERN.findall(text))
    non_cjk = _CJK_PATTERN.sub(" ", text)
    words = len(non_cjk.split())
    return cjk_chars + int(words * 1.3)


def format_k(n: int) -> str:
    """把 token 数格式化成「k」表示：131072 -> 128k，12345 -> 12.1k。"""
    if n >= 1024:
        val = n / 1024
        s = f"{val:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return s + "k"
    return str(n)


def _format_message(msg: dict, max_len: int | None = None) -> str:
    """把一条 OpenAI 消息转成可读文本（保留角色、内容、工具调用与工具结果）。"""
    role = msg.get("role", "")
    content = msg.get("content")
    if content is None:
        content = ""
    parts = [f"[{role}]"]
    if isinstance(content, str) and content.strip():
        parts.append(content)
    for tc in msg.get("tool_calls") or []:
        fn = (tc or {}).get("function", {}) or {}
        parts.append(f"[工具调用 {fn.get('name', '')}] {fn.get('arguments', '')}")
    if msg.get("tool_call_id"):
        parts.append(f"[tool_call_id={msg['tool_call_id']}]")
    text = "\n".join(parts)
    if max_len is not None and len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def count_history_tokens(history) -> int:
    """估算整个消息历史的 token 数（含 System、User、Assistant、Tool 消息）。"""
    total = 0
    for msg in history:
        total += estimate_tokens(_format_message(msg))
    return total


def get_context_window(model: str) -> tuple[int, bool]:
    """返回 (上下文窗口 token 数, 是否使用默认值)。"""
    window = KNOWN_MODEL_WINDOWS.get(model)
    if window is None:
        return DEFAULT_CONTEXT_WINDOW, True
    return window, False


# ─── 原始上下文归档 ───


def ensure_context_dir() -> str:
    os.makedirs(CONTEXT_DIR, exist_ok=True)
    return CONTEXT_DIR


def save_archive(history) -> tuple[str, str]:
    """把完整历史保存为 JSONL 归档，返回 (归档编号, 文件路径)。

    归档编号形如 20261022-153000；同秒重复时自动加 -2、-3 后缀。
    每条消息一行 JSON，保留角色、内容、工具调用参数、工具结果与顺序。
    """
    ensure_context_dir()
    base = time.strftime("%Y%m%d-%H%M%S")
    archive_id = base
    path = os.path.join(CONTEXT_DIR, f"{archive_id}.jsonl")
    i = 2
    while os.path.exists(path):
        archive_id = f"{base}-{i}"
        path = os.path.join(CONTEXT_DIR, f"{archive_id}.jsonl")
        i += 1
    with open(path, "w", encoding="utf-8") as f:
        for msg in history:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return archive_id, path


def _list_archives() -> list[str]:
    """列出所有归档文件名（按名称排序）。"""
    if not os.path.isdir(CONTEXT_DIR):
        return []
    return sorted(f for f in os.listdir(CONTEXT_DIR) if f.endswith(".jsonl"))


# ─── recallContext：关键词检索 ───


def recall_context(id=None, text=None) -> str:
    """recallContext 工具：在原始上下文归档中按关键词检索。

    id 可选，指定归档编号；text 必填，为要查找的关键词。
    返回匹配到的片段、归档编号与所在行号，并限制总长度避免再次占满上下文。
    """
    keyword = (text or "").strip()
    if not keyword:
        return "错误：recallContext 需要提供 text 参数（要查找的关键词）。"

    raw_id = (id or "").strip()
    if raw_id:
        archive_id = raw_id if raw_id.endswith(".jsonl") else raw_id + ".jsonl"
        path = os.path.join(CONTEXT_DIR, archive_id)
        if not os.path.isfile(path):
            return f"错误：未找到编号为 {raw_id!r} 的归档文件（在 {CONTEXT_DIR} 下）。"
        targets = [path]
    else:
        targets = [os.path.join(CONTEXT_DIR, f) for f in _list_archives()]

    if not targets:
        return "未找到任何上下文归档，可能尚未执行过 /compact。"

    matches: list[tuple[str, int, str]] = []  # (归档编号, 行号, 片段)
    for path in targets:
        aid = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    readable = _format_message(msg, max_len=RECALL_SNIPPET_CHARS)
                    if keyword.lower() in readable.lower():
                        matches.append((aid, lineno, readable))
        except OSError:
            continue

    if not matches:
        return f"在已保存的上下文归档中未找到与 {keyword!r} 相关的记录。"

    lines = [f"找到 {len(matches)} 条相关记录（关键词 {keyword!r}）："]
    total = 0
    shown = 0
    for aid, lineno, snippet in matches:
        entry = f"\n[{aid} · 第{lineno}行]\n{snippet}"
        if total + len(entry) > RECALL_MAX_TOTAL_CHARS:
            lines.append(f"\n…（其余 {len(matches) - shown} 条结果已省略）")
            break
        lines.append(entry)
        total += len(entry)
        shown += 1
    return "".join(lines)


# ─── 上下文压缩 ───

# 上下文压缩专用 Prompt（用于 /compact 流程）
COMPACT_PROMPT = (
    "你是一名 AI Agent 的上下文管理助手。当前会话即将达到上下文窗口限制，需要将历史对话压缩为一份高质量的工作摘要，"
    "以便 Agent 在新的上下文中继续执行任务。\n\n"
    "请认真阅读提供的历史上下文，提取对后续工作仍然有价值的信息，并生成一份约 5,000 Tokens 的 Markdown 摘要。\n\n"
    "摘要应重点保留以下内容：\n"
    "1. **用户目标与要求**：用户希望完成什么任务？有哪些必须遵守的约束、偏好和验收条件？\n"
    "2. **已经完成的工作**：哪些任务已经完成？实现了哪些功能？获得了哪些已经验证的结果？\n"
    "3. **当前工作状态**：Agent 正在处理什么任务？当前进展到哪一步？是否存在尚未解决的问题？\n"
    "4. **关键技术信息**：保留重要的文件路径、函数名称、接口定义、配置项、执行命令、错误信息和必要的代码片段。\n"
    "5. **关键决策与原因**：记录用户和 Agent 已经确定的技术方案，以及重要的设计取舍。\n"
    "6. **下一步任务**：明确接下来应该执行的操作，以及需要继续验证的事项。\n\n"
    "请遵守以下要求：\n"
    "- 优先保留对后续任务有直接影响的信息，删除寒暄、重复讨论、过时的尝试和无关内容。\n"
    "- 对已经验证的事实与尚未验证的推测作出明确区分。\n"
    "- 不要将计划完成的任务描述为已经完成。\n"
    "- 重要的文件路径、函数名称、配置名称和命令应保留准确原文。\n"
    "- 对失败的尝试，只保留有助于避免重复错误的信息。\n"
    "- 如果信息存在冲突，应优先保留用户最新确认的要求，并简要说明仍然存在的冲突。\n"
    "- 如果某些历史细节无法在摘要中完整保留，可简要说明它们已保存到原始上下文归档中，"
    "后续可以通过 recallContext 工具重新检索。\n"
    "- 使用清晰的 Markdown 结构输出摘要，不要附加解释或无关内容。\n\n"
    "**最终目标：使一个从未阅读过原始对话的 Agent，仅凭这份摘要，就能够理解当前任务、继承关键决策，并继续开展工作。**\n\n"
    "现在请直接输出这份 Markdown 摘要正文，不要输出任何解释或与摘要无关的内容。"
)


def build_compact_messages(history) -> list[dict]:
    """构造压缩请求的消息：system 放压缩 Prompt，user 放完整历史文本。"""
    history_text = "\n\n".join(_format_message(m) for m in history)
    return [
        {"role": "system", "content": COMPACT_PROMPT},
        {"role": "user", "content": "请根据系统提示的要求，对以下历史上下文生成压缩摘要：\n\n" + history_text},
    ]


def make_archive_note(archive_id: str) -> str:
    """生成追加在摘要末尾的归档说明。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"此前上下文已于 {now} 完成压缩，原始上下文保存在编号为 {archive_id} 的归档文件中。"
        "如需查找历史信息，请使用 recallContext 工具。"
    )


# ─── recallContext 工具定义 ───

RECALL_CONTEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "recallContext",
        "description": (
            "检索之前压缩保存的原始上下文归档。当上下文摘要缺少某些历史细节时，"
            "用关键词在 .agents/context/ 下查找原始记录，返回相关片段、归档编号与所在位置。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "可选。要检索的归档编号（如 20261022-153000）；不填则在所有归档中检索",
                },
                "text": {
                    "type": "string",
                    "description": "必填。要查找的关键词或文本内容",
                },
            },
            "required": ["text"],
        },
    },
}
