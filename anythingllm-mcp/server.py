"""AnythingLLM MCP Server —— 基于 MCP SDK 的标准 Streamable HTTP 服务。

把 AnythingLLM 工作区能力（提问、查看配置、聊天记录、向量检索）暴露为 MCP 工具，
供任意支持 MCP 的客户端（含本仓库 xAgent）通过 /mcp 端点连接调用。
"""

from __future__ import annotations

import os
from typing import Any, Literal

import httpx
from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

# AnythingLLM 连接配置（通过环境变量注入，不在代码里写死密钥）
ANYTHINGLLM_BASE = os.getenv("ANYTHINGLLM_BASE", "http://localhost:3001").rstrip("/")
ANYTHINGLLM_API_KEY = os.getenv("ANYTHINGLLM_API_KEY", "")
WORKSPACE_SLUG = os.getenv("WORKSPACE_SLUG", "")


class AskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1)


class ListChatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=100, ge=1, le=1000)
    order_by: Literal["asc", "desc"] = "desc"
    api_session_id: str | None = None


class VectorSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1)
    top_n: int = Field(default=4, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=0, le=1)


async def _call_api(method: str, path: str, **kwargs) -> Any:
    """调用 AnythingLLM API；未配置密钥/工作区或返回错误时抛出带中文原因的异常。"""
    if not ANYTHINGLLM_API_KEY:
        raise RuntimeError("未设置 ANYTHINGLLM_API_KEY 环境变量")
    if not WORKSPACE_SLUG:
        raise RuntimeError("未设置 WORKSPACE_SLUG 环境变量")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.request(
            method,
            f"{ANYTHINGLLM_BASE}{path}",
            headers={"Authorization": f"Bearer {ANYTHINGLLM_API_KEY}"},
            **kwargs,
        )
    if resp.is_error:
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text or "AnythingLLM 返回无效响应"}
        message = data.get("error") or data.get("message") or resp.reason_phrase
        raise RuntimeError(f"AnythingLLM API 错误 ({resp.status_code}): {message}")
    return resp.json()


def create_server() -> MCPServer:
    server = MCPServer(
        name="anythingllm",
        version="0.2.0",
        description="AnythingLLM 工作区的 MCP 服务",
    )

    @server.tool(name="ask_workspace", description="向 AnythingLLM 工作区提问并获取 AI 回答")
    async def ask_workspace(input: AskInput) -> str:
        data = await _call_api(
            "POST",
            f"/api/v1/workspace/{WORKSPACE_SLUG}/chat",
            json={"message": input.question.strip(), "mode": "query"},
        )
        return str(data.get("textResponse", "无响应"))

    @server.tool(name="get_workspace", description="获取当前 AnythingLLM 工作区的配置、文档和线程摘要")
    async def get_workspace() -> dict:
        return await _call_api("GET", f"/api/v1/workspace/{WORKSPACE_SLUG}")

    @server.tool(name="list_workspace_chats", description="获取当前 AnythingLLM 工作区的聊天记录")
    async def list_workspace_chats(input: ListChatsInput) -> dict:
        params: dict[str, Any] = {"limit": input.limit, "orderBy": input.order_by}
        if input.api_session_id:
            params["apiSessionId"] = input.api_session_id
        return await _call_api(
            "GET", f"/api/v1/workspace/{WORKSPACE_SLUG}/chats", params=params
        )

    @server.tool(name="vector_search", description="在当前工作区文档中执行向量相似度搜索")
    async def vector_search(input: VectorSearchInput) -> dict:
        payload: dict[str, Any] = {"query": input.query.strip(), "topN": input.top_n}
        if input.score_threshold is not None:
            payload["scoreThreshold"] = input.score_threshold
        return await _call_api(
            "POST",
            f"/api/v1/workspace/{WORKSPACE_SLUG}/vector-search",
            json=payload,
        )

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "anythingllm-mcp"})

    return server


async def run() -> None:
    server = create_server()
    await server.run_streamable_http_async(
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8081")),
        stateless_http=True,
        streamable_http_path="/mcp",
        json_response=True,
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
