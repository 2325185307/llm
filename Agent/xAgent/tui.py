"""xAgent 全屏终端界面（TUI）。

左侧为对话 + 输入区，右侧为状态边栏（上下文用量、MCP 服务器与工具、斜杠命令）。
MCP 采用异步加载：先进入界面，后台连接完成后动态刷新边栏。
"""

import asyncio
import queue
import threading

from prompt_toolkit import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

import chat
import config
import mcp_client
import sessions
import skills

# 斜杠命令清单：(命令, 说明)
SLASH_COMMANDS = [
    ("/new", "新建会话（自动保存当前会话）"),
    ("/resume", "从历史会话列表恢复"),
    ("/save", "保存当前会话"),
    ("/compact", "压缩上下文"),
    ("/clear", "清空当前会话（不保存）"),
    ("/help", "显示帮助"),
    ("/skills", "列出可用技能"),
    ("/mcp", "列出 MCP 工具"),
    ("/quit", "保存并退出"),
]

STYLE = Style.from_dict(
    {
        "title": "bold",
        "prompt": "bold #00d787",
        "info": "#888888",
        "warning": "#ffaa00",
        "error": "#ff5555",
        "selected": "reverse",
    }
)


class SlashCompleter(Completer):
    """输入以 / 开头时，弹出斜杠命令补全引导面板。"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for cmd, desc in SLASH_COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)


class XAgentTUI:
    def __init__(self):
        self.history = chat.new_history()
        # config.ini 缺失被自动生成时，把提示显示在初始日志里（终端全屏后 stderr 可能被覆盖）
        self._log_text = (
            "⚠ " + config.notice_text().replace("\n", "\n⚠ ") + "\n\n"
            if config.was_created()
            else ""
        )
        self.api_prompt_tokens = None
        self.last_reply = ""
        self.mode = "chat"  # "chat" | "resume"
        self.resume_list = []
        self.resume_index = 0

        self.input_buffer = Buffer(multiline=False, completer=SlashCompleter())
        # 只读日志区：用 TextArea 获得滚动条与自动滚动到底部
        self.log_area = TextArea(
            text="", read_only=True, scrollbar=True, focusable=False, wrap_lines=True
        )

    # ── 显示 ──

    def _render_main(self) -> str:
        if self.mode == "resume":
            lines = [" 选择要恢复的会话（↑↓ 选择，Enter 恢复，ESC 取消）\n"]
            for i, (sid, _path, preview) in enumerate(self.resume_list):
                mark = "▶" if i == self.resume_index else " "
                lines.append(f" {mark} {sid}   {preview}\n")
            return "".join(lines)
        return self._log_text

    def _refresh_display(self):
        text = self._render_main()
        self.log_area.document = Document(text, len(text))
        get_app().invalidate()

    def _add_log(self, text: str):
        self._log_text += text
        self._refresh_display()

    def render_sidebar(self):
        tokens = chat.current_tokens(self.history, self.api_prompt_tokens, self.last_reply)
        status, _pct = chat.status_line(tokens)
        warning = "  ⚠ 已达阈值，请 /compact" if chat.at_threshold(tokens) else ""
        mcp = chat.mcp_sidebar_text()

        lines = [("class:title", " xAgent 状态\n")]
        lines.append(("", " " + status + warning + "\n\n"))
        lines.append(("class:title", " MCP 服务器\n"))
        lines.append(("", " " + mcp.replace("\n", "\n ") + "\n\n"))
        lines.append(("class:title", " 斜杠命令\n"))
        for cmd, desc in SLASH_COMMANDS:
            lines.append(("", f" {cmd}  {desc}\n"))
        return lines

    # ── 斜杠命令 ──

    def _on_enter(self, app):
        text = self.input_buffer.text.strip()
        if not text:
            return
        self.input_buffer.reset()
        if text.startswith("/"):
            self._handle_slash(app, text)
            return
        # 普通提示词：达到阈值时暂停，仅允许 /compact
        tokens = chat.current_tokens(self.history, self.api_prompt_tokens, self.last_reply)
        if chat.at_threshold(tokens):
            self._add_log("⚠ 上下文已达阈值，请先执行 /compact 再继续输入。\n")
            return
        self._add_log("你: " + text + "\n")
        app.create_background_task(self._run_turn(text))

    def _handle_slash(self, app, text):
        cmd = text.split()[0].lower()
        if cmd == "/new":
            self._new_session()
        elif cmd == "/save":
            self._save_session()
        elif cmd == "/resume":
            self._enter_resume()
        elif cmd == "/clear":
            self._clear_session()
        elif cmd == "/help":
            self._show_help()
        elif cmd == "/skills":
            self._show_skills()
        elif cmd == "/mcp":
            self._show_mcp()
        elif cmd == "/compact":
            app.create_background_task(self._do_compact())
        elif cmd in ("/quit", "/exit"):
            self._quit(app)
        else:
            self._add_log(f"未知命令 {cmd}，输入 /help 查看帮助。\n")

    def _has_content(self) -> bool:
        return any(m.get("role") in ("user", "assistant") for m in self.history)

    def _new_session(self):
        if self._has_content():
            sid = sessions.save_session(self.history)
            self._add_log(f"已保存当前会话（编号 {sid}），开始新会话。\n")
        else:
            self._add_log("开始新会话。\n")
        self.history = chat.new_history()
        self.api_prompt_tokens = None
        self.last_reply = ""
        self._log_text = ""

    def _save_session(self):
        if not self._has_content():
            self._add_log("当前会话为空，无需保存。\n")
        else:
            sid = sessions.save_session(self.history)
            self._add_log(f"已保存当前会话，编号 {sid}。\n")

    def _clear_session(self):
        self.history = chat.new_history()
        self.api_prompt_tokens = None
        self.last_reply = ""
        self._log_text = ""
        self._add_log("已清空当前会话（未保存）。\n")

    def _enter_resume(self):
        self.resume_list = sessions.list_sessions()
        self.resume_index = 0
        if not self.resume_list:
            self._add_log("没有可恢复的历史会话（.agents/sessions/ 为空）。\n")
            return
        self.mode = "resume"
        self._refresh_display()

    def _resume_up(self):
        if self.resume_list:
            self.resume_index = (self.resume_index - 1) % len(self.resume_list)
            self._refresh_display()

    def _resume_down(self):
        if self.resume_list:
            self.resume_index = (self.resume_index + 1) % len(self.resume_list)
            self._refresh_display()

    def _select_resume(self):
        if not self.resume_list:
            self.mode = "chat"
            self._refresh_display()
            return
        sid, _path, preview = self.resume_list[self.resume_index]
        try:
            loaded = sessions.load_session(sid)
        except OSError as exc:
            self.mode = "chat"
            self._add_log(f"恢复失败：{exc}\n")
            return
        self.history = sessions.inject_agents_md(loaded)
        self.api_prompt_tokens = None
        self.last_reply = ""
        self.mode = "chat"
        self._log_text = ""
        self._add_log(f"已恢复会话 {sid}（{preview}）。\n")
        self._rebuild_log_from_history()

    def _cancel_resume(self):
        self.mode = "chat"
        self._add_log("已取消恢复。\n")

    def _rebuild_log_from_history(self):
        for m in self.history:
            role = m.get("role")
            content = str(m.get("content") or "")
            if role == "user" and content:
                self._add_log("你: " + content + "\n")
            elif role == "assistant" and content:
                self._add_log("AI: " + content + "\n")

    def _show_help(self):
        lines = ["可用斜杠命令："]
        for cmd, desc in SLASH_COMMANDS:
            lines.append(f"  {cmd}  {desc}")
        self._add_log("\n".join(lines) + "\n")

    def _show_skills(self):
        ss = getattr(skills, "_skills", None) or []
        if ss:
            lines = ["可用技能："]
            for s in ss:
                lines.append(f"  · {s['name']}: {(s.get('description') or '').strip()}")
            self._add_log("\n".join(lines) + "\n")
        else:
            self._add_log("（未发现任何技能）\n")

    def _show_mcp(self):
        self._add_log(chat.mcp_sidebar_text() + "\n")

    def _quit(self, app):
        if self._has_content():
            try:
                sid = sessions.save_session(self.history)
                self._add_log(f"已自动保存当前会话（编号 {sid}）。\n")
            except OSError:
                pass
        mcp_client.shutdown()
        app.exit()

    # ── 慢操作（后台线程执行，主循环刷新 UI）──

    async def _run_in_thread(self, fn):
        """在后台线程执行同步函数 fn(progress)，把进度推送到主循环。"""
        q: queue.Queue = queue.Queue()

        def _work():
            try:
                result = fn(progress=lambda s: q.put(("progress", s)))
                q.put(("done", result))
            except Exception as exc:
                q.put(("error", str(exc)))

        threading.Thread(target=_work, name="xagent-turn", daemon=True).start()
        while True:
            try:
                kind, payload = q.get(timeout=0.1)
            except queue.Empty:
                await asyncio.sleep(0.05)
                self._refresh_display()
                continue
            if kind == "progress":
                self._add_log("  " + payload + "\n")
            elif kind == "error":
                raise RuntimeError(payload)
            else:  # done
                return payload

    async def _run_turn(self, text):
        self._add_log("⏳ 思考中…\n")
        try:
            reply, api = await self._run_in_thread(
                lambda progress: chat.run_turn(self.history, text, progress)
            )
            self.api_prompt_tokens = api
            self.last_reply = reply
            self._add_log("AI: " + reply + "\n")
        except Exception as exc:
            self._add_log(f"❌ 出错了：{exc}\n")
        self._refresh_display()

    async def _do_compact(self):
        self._add_log("⏳ 压缩中…\n")
        try:
            new_history, ok, msg = await self._run_in_thread(
                lambda progress: chat.compact(self.history, progress)
            )
            if ok:
                self.history = new_history
                self.api_prompt_tokens = None
                self.last_reply = ""
            self._add_log(msg + "\n")
        except Exception as exc:
            self._add_log(f"❌ 压缩失败：{exc}\n")
        self._refresh_display()

    # ── MCP 异步加载 ──

    async def _mcp_watcher(self):
        while not chat.mcp_discovery_done():
            await asyncio.sleep(0.5)
            self._refresh_display()
        chat.sync_mcp_tools()
        self._refresh_display()

    def _on_pre_run(self):
        self._refresh_display()
        get_app().create_background_task(self._mcp_watcher())


def run_tui():
    tui = XAgentTUI()
    kb = KeyBindings()

    chat_mode = Condition(lambda: tui.mode == "chat")
    resume_mode = Condition(lambda: tui.mode == "resume")

    @kb.add("enter", filter=chat_mode)
    def _(event):
        tui._on_enter(event.app)

    @kb.add("up", filter=resume_mode)
    def _(event):
        tui._resume_up()

    @kb.add("down", filter=resume_mode)
    def _(event):
        tui._resume_down()

    @kb.add("enter", filter=resume_mode)
    def _(event):
        tui._select_resume()

    @kb.add("escape", filter=resume_mode)
    def _(event):
        tui._cancel_resume()

    @kb.add("c-c")
    def _(event):
        tui._quit(event.app)

    input_row = VSplit(
        [
            Window(
                content=FormattedTextControl([("class:prompt", "❯ ")]),
                width=3,
                dont_extend_width=True,
            ),
            Window(content=BufferControl(buffer=tui.input_buffer), height=1),
        ]
    )

    sidebar = Window(
        content=FormattedTextControl(text=tui.render_sidebar),
        width=40,
        wrap_lines=True,
    )

    root = VSplit(
        [
            HSplit([tui.log_area, input_row]),
            sidebar,
        ]
    )

    app = Application(
        layout=Layout(root),
        key_bindings=kb,
        full_screen=True,
        style=STYLE,
        mouse_support=True,
    )
    app.run(pre_run=tui._on_pre_run)


if __name__ == "__main__":
    run_tui()
