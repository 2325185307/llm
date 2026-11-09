"""会话持久化与项目说明（AGENTS.md）注入。

会话以 JSONL 形式保存到 .agents/sessions/（每行一条 OpenAI 消息），
文件名即会话编号（形如 20261109-153000）。支持保存 / 列出 / 恢复。
"""

import json
import os
import time

# 与 skills.py / context.py 一致：以程序启动时的 cwd 作为项目根目录
PROJECT_ROOT = os.path.abspath(os.getcwd())
SESSIONS_DIR = os.path.join(PROJECT_ROOT, ".agents", "sessions")

AGENTS_MD = os.path.join(PROJECT_ROOT, "AGENTS.md")
_AGENTS_MARKER = "[AGENTS.md]"


# ─── 会话保存 / 列出 / 恢复 ───


def ensure_sessions_dir() -> str:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return SESSIONS_DIR


def _unique_path(base: str):
    """返回一个不冲突的 (会话编号, 文件路径)，同秒重复时加 -2、-3 后缀。"""
    sid = base
    path = os.path.join(SESSIONS_DIR, f"{sid}.jsonl")
    i = 2
    while os.path.exists(path):
        sid = f"{base}-{i}"
        path = os.path.join(SESSIONS_DIR, f"{sid}.jsonl")
        i += 1
    return sid, path


def save_session(history, session_id: str | None = None) -> str:
    """把完整消息历史保存为 JSONL 会话文件，返回会话编号。"""
    ensure_sessions_dir()
    base = session_id or time.strftime("%Y%m%d-%H%M%S")
    sid, path = _unique_path(base)
    with open(path, "w", encoding="utf-8") as f:
        for msg in history:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return sid


def _preview(path: str) -> str:
    """取会话中第一条 user 消息作为预览，便于在恢复面板中识别。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("role") == "user":
                    content = str(msg.get("content", "")).replace("\n", " ")
                    return content[:60] + ("…" if len(content) > 60 else "")
    except OSError:
        pass
    return "(空会话)"


def list_sessions():
    """列出所有会话，返回 [(会话编号, 文件路径, 预览)]，新的在前。"""
    if not os.path.isdir(SESSIONS_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        if not fn.endswith(".jsonl"):
            continue
        path = os.path.join(SESSIONS_DIR, fn)
        out.append((fn[:-6], path, _preview(path)))
    return out


def load_session(session_id: str) -> list:
    """按会话编号恢复消息历史；编号不存在时抛出 FileNotFoundError。"""
    path = os.path.join(SESSIONS_DIR, f"{session_id}.jsonl")
    history = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                history.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return history


# ─── AGENTS.md：一次注入，全程复用 ───


def inject_agents_md(history: list) -> list:
    """会话首次加载时，若项目根目录存在 AGENTS.md，则作为一条 system 消息注入。

    注入后该消息会一直留在 history 中（全程复用），并随会话保存进事件日志，
    恢复会话时因已含标记而不会重复注入。
    """
    if not os.path.isfile(AGENTS_MD):
        return history
    for msg in history:
        content = msg.get("content", "")
        if isinstance(content, str) and content.startswith(_AGENTS_MARKER):
            return history  # 已注入过，跳过
    try:
        with open(AGENTS_MD, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return history
    if not content:
        return history
    history.append(
        {
            "role": "system",
            "content": f"{_AGENTS_MARKER} 项目说明（一次注入，全程复用）：\n\n{content}",
        }
    )
    return history
