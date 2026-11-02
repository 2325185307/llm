"""MCP (Model Context Protocol) 客户端，让 xAgent 能连接外部 MCP Server。

职责：
1. 启动时读取「当前项目目录(cwd)」下的 .mcp.json，获得各 MCP Server 的连接信息；
2. 按 MCP Python SDK 标准接口建立连接（默认 Streamable HTTP，另支持 stdio），
   调用 list_tools() 发现工具的名称、描述与参数 Schema，转成 OpenAI Function Calling
   的工具定义，动态并入 xAgent 已有的 tools / tool_functions；
3. 维护「对外工具名 → (Server 名, 原始工具名)」映射，名称冲突时给对外名加 Server 前缀；
4. LLM 发起 MCP 工具调用时，根据映射找到对应 Server 转发名称与参数，并正确处理
   文本内容、结构化数据与错误信息。

MCP SDK 是异步的，本模块用守护线程跑一个独立事件循环，把异步调用桥接成同步接口，
从而无侵入地接入 chat.py / ui.py 现有的同步 Function Calling 流程。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import threading
from collections import Counter
from urllib.parse import urlsplit

CONFIG_FILE = ".mcp.json"

# 连接 / 列工具 / 调工具的超时（秒）
CONNECT_TIMEOUT = 20.0
LIST_TOOLS_TIMEOUT = 15.0
CALL_TOOL_TIMEOUT = 60.0


class _AsyncExecutor:
    """守护线程里运行独立事件循环，供同步代码调度协程。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="mcp-event-loop", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def shutdown(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


class _Server:
    """一个 MCP Server 的连接句柄。"""

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self.config = config
        self.transport = "http" if config.get("url") else "stdio"
        self.transport_cm = None    # 已进入的传输层上下文管理器
        self.http_client_cm = None  # 自定义 HTTP 客户端（仅 HTTP + headers 时）
        self.session = None         # 已进入并 initialize 的 ClientSession
        self.read_stream = None
        self.write_stream = None
        self.tools: list[dict] = []  # [{"name", "description", "input_schema"}]


def _sanitize_name(name: str) -> str:
    """把 Server 名净化为可作工具名片段的格式。"""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(name)).strip("_").lower()
    return cleaned or "mcp"


def _describe_exc(exc: BaseException) -> str:
    """异常描述：优先用 str，为空时退回异常类型名，避免出现空原因。"""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _preflight_http(url: str, timeout: float = 5.0) -> str | None:
    """HTTP Server 连接前的 TCP 预检。

    返回 None 表示端口可达；否则返回清晰的错误描述。SDK 内部连接失败时往往只抛
    CancelledError，先在这里预检，能让「服务器未启动」这类问题给出明确提示。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None  # URL 本身非法，交给 SDK 报错
    host = parts.hostname
    if not host:
        return None
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return f"无法连接到 {url}（{_describe_exc(exc)}，服务器可能未启动）"


def _to_openai_parameters(input_schema):
    """把 MCP 工具的 inputSchema 转成 OpenAI Function Calling 的 parameters。"""
    if not isinstance(input_schema, dict) or not input_schema:
        return {"type": "object", "properties": {}}
    if input_schema.get("type") != "object":
        # OpenAI / DeepSeek 要求 parameters.type 必须为 object；
        # 非 object 的 schema 无法表达，回退为空 object schema 以保调用可用。
        return {"type": "object", "properties": {}}
    params = {
        "type": "object",
        "properties": input_schema.get("properties") or {},
    }
    required = input_schema.get("required")
    if isinstance(required, list):
        params["required"] = required
    return params


def _to_jsonable(obj):
    """把结果对象尽量转成可 JSON 序列化的纯数据。"""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


def _format_result(result) -> str:
    """把 call_tool 返回的 CallToolResult 整理成给 LLM 的文本。"""
    is_error = bool(getattr(result, "is_error", False))
    content = getattr(result, "content", None) or []
    structured = getattr(result, "structured_content", None)

    text_parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            type_name = block.get("type")
        else:
            text = getattr(block, "text", None)
            type_name = getattr(block, "type", type(block).__name__)
        if text is not None:
            text_parts.append(str(text))
        else:
            text_parts.append(f"[{type_name}]")

    parts: list[str] = []
    if structured is not None:
        try:
            structured_text = json.dumps(
                _to_jsonable(structured), ensure_ascii=False, indent=2, default=str
            )
        except Exception:
            structured_text = str(structured)
        parts.append("结构化数据(structuredContent)：\n" + structured_text)

    text = "\n".join(p for p in text_parts if p)
    if text:
        parts.append(text)

    body = "\n\n".join(parts) if parts else "（工具未返回内容）"
    if is_error:
        return "MCP 工具返回错误：\n" + body
    return body


class McpClientManager:
    """管理所有 MCP Server 的连接、工具注册与调用转发。"""

    def __init__(self) -> None:
        self._executor = _AsyncExecutor()
        self._servers: dict[str, _Server] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}  # 对外名 -> (server, 原始名)
        self._started = False

    # ── 连接（运行在后台事件循环里）──

    def _ensure_executor(self) -> None:
        if not self._started:
            self._executor.start()
            self._started = True

    async def _enter_transport(self, server: _Server) -> None:
        """进入传输层上下文，产出 read/write 流。"""
        if server.transport == "http":
            from mcp.client.streamable_http import streamable_http_client

            headers = server.config.get("headers")
            http_client = None
            if headers:
                http_client = self._make_http_client(headers)
                await http_client.__aenter__()
                server.http_client_cm = http_client

            cm = streamable_http_client(server.config["url"], http_client=http_client)
            read, write = await cm.__aenter__()
            server.transport_cm = cm
        else:
            from mcp.client.stdio import stdio_client, StdioServerParameters

            params = StdioServerParameters(
                command=server.config["command"],
                args=server.config.get("args") or [],
                env=server.config.get("env") or None,
            )
            cm = stdio_client(params)
            read, write = await cm.__aenter__()
            server.transport_cm = cm
        server.read_stream = read
        server.write_stream = write

    @staticmethod
    def _make_http_client(headers: dict):
        """构造带自定义 headers 的 httpx2 客户端，沿用 SDK 推荐的 MCP 超时。"""
        try:
            from mcp.shared._httpx_utils import create_mcp_http_client

            return create_mcp_http_client(headers=headers)
        except Exception:
            import httpx2

            return httpx2.AsyncClient(headers=headers)

    async def _connect_and_list(self, server: _Server) -> list[dict]:
        """连接 + initialize + list_tools；失败时清理已建立的连接再抛出。"""
        try:
            await self._enter_transport(server)

            from mcp import ClientSession

            session = ClientSession(server.read_stream, server.write_stream)
            await session.__aenter__()
            await session.initialize()
            server.session = session

            result = await session.list_tools()
            tools = []
            for tool in (result.tools or []):
                tools.append(
                    {
                        "name": tool.name,
                        "description": (tool.description or "").strip(),
                        "input_schema": tool.input_schema,
                    }
                )
            server.tools = tools
            return tools
        except Exception:
            await self._close_server(server)
            raise

    async def _call_tool(self, server: _Server, name: str, arguments: dict):
        return await server.session.call_tool(name, arguments=arguments)

    async def _close_server(self, server: _Server) -> None:
        """按进入的逆序退出各上下文，清理连接。"""
        if server.session is not None:
            try:
                await server.session.__aexit__(None, None, None)
            except Exception:
                pass
            server.session = None
        if server.transport_cm is not None:
            try:
                await server.transport_cm.__aexit__(None, None, None)
            except Exception:
                pass
            server.transport_cm = None
        if server.http_client_cm is not None:
            try:
                await server.http_client_cm.__aexit__(None, None, None)
            except Exception:
                pass
            server.http_client_cm = None

    # ── 对外同步接口 ──

    def discover_and_register(self, reserved_names: set[str] | None = None):
        """读取 .mcp.json，连接所有 Server 并注册工具。

        返回 (tool_defs, callables, messages)：
          - tool_defs   OpenAI Function Calling 工具定义列表
          - callables   对外工具名 → 可同步调用的函数
          - messages    信息/错误提示列表（连接失败、无配置等）
        """
        self._ensure_executor()

        reserved = set(reserved_names or ())
        config_path = os.path.join(os.getcwd(), CONFIG_FILE)
        tool_defs: list[dict] = []
        callables: dict[str, object] = {}
        messages: list[str] = []

        if not os.path.isfile(config_path):
            return tool_defs, callables, [f"未找到 {CONFIG_FILE}，跳过 MCP 工具加载"]

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return tool_defs, callables, [f"读取 {CONFIG_FILE} 失败：{exc}"]

        servers_cfg = raw.get("mcpServers") if isinstance(raw, dict) else None
        if not isinstance(servers_cfg, dict) or not servers_cfg:
            return tool_defs, callables, [f"{CONFIG_FILE} 中未找到 mcpServers 配置"]

        # 1) 连接所有 Server，发现各自工具
        for name, cfg in servers_cfg.items():
            if not isinstance(cfg, dict):
                messages.append(f"Server {name!r} 配置非法（应为对象），已跳过")
                continue
            if not (cfg.get("url") or cfg.get("command")):
                messages.append(f"Server {name!r} 缺少 url 或 command，已跳过")
                continue

            server = _Server(str(name), cfg)
            if server.transport == "http":
                preflight = _preflight_http(server.config["url"])
                if preflight:
                    messages.append(f"连接 Server {name!r} 失败：{preflight}")
                    continue
            try:
                self._executor.run(
                    self._connect_and_list(server), timeout=CONNECT_TIMEOUT
                )
            except Exception as exc:
                messages.append(f"连接 Server {name!r} 失败：{_describe_exc(exc)}")
                continue
            self._servers[server.name] = server
            messages.append(
                f"已连接 MCP Server {name!r}（{server.transport}），发现 {len(server.tools)} 个工具"
            )

        # 2) 名称冲突检测 + 需要时加 Server 前缀
        counter = Counter(
            tool["name"] for s in self._servers.values() for tool in s.tools
        )
        used = set(reserved)

        def unique(base: str) -> str:
            candidate = base
            i = 2
            while candidate in used:
                candidate = f"{base}_{i}"
                i += 1
            used.add(candidate)
            return candidate

        for server in self._servers.values():
            prefix = _sanitize_name(server.name)
            for tool in server.tools:
                original = tool["name"]
                if counter[original] == 1 and original not in reserved:
                    exposed = unique(original)
                else:
                    exposed = unique(f"{prefix}__{original}")

                self._tool_map[exposed] = (server.name, original)
                tool_defs.append(
                    {
                        "type": "function",
                        "function": {
                            "name": exposed,
                            "description": tool["description"]
                            or f"MCP 工具 {original}（来自 Server {server.name}）",
                            "parameters": _to_openai_parameters(tool["input_schema"]),
                        },
                    }
                )
                callables[exposed] = self._make_callable(server.name, original)

        if not tool_defs and self._servers:
            messages.append("已连接 MCP Server，但未发现任何工具")
        return tool_defs, callables, messages

    def _make_callable(self, server_name: str, original: str):
        def call(**arguments):
            return self.call_tool(server_name, original, arguments)

        call.__name__ = f"mcp_{_sanitize_name(server_name)}_{_sanitize_name(original)}"
        return call

    def call_tool(self, server_name: str, original_name: str, arguments: dict | None) -> str:
        """同步转发一次工具调用到指定 Server，返回整理后的文本。"""
        server = self._servers.get(server_name)
        if server is None or server.session is None:
            return f"错误：MCP Server {server_name!r} 未连接"
        try:
            result = self._executor.run(
                self._call_tool(server, original_name, arguments or {}),
                timeout=CALL_TOOL_TIMEOUT,
            )
        except Exception as exc:
            return f"错误：调用 MCP 工具 {server_name}/{original_name} 失败：{_describe_exc(exc)}"
        return _format_result(result)

    def shutdown(self) -> None:
        """关闭所有连接并停止事件循环。"""
        async def _close_all() -> None:
            for server in list(self._servers.values()):
                await self._close_server(server)
            self._servers.clear()
            self._tool_map.clear()

        if self._started:
            try:
                self._executor.run(_close_all(), timeout=5.0)
            except Exception:
                pass
            self._executor.shutdown()
            self._started = False


# 模块级单例，供 chat.py / ui.py 复用
_manager = McpClientManager()


def discover_and_register(reserved_names: set[str] | None = None):
    return _manager.discover_and_register(reserved_names)


def call_tool(server_name: str, original_name: str, arguments: dict | None = None) -> str:
    return _manager.call_tool(server_name, original_name, arguments)


def shutdown() -> None:
    _manager.shutdown()
