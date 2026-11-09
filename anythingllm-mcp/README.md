# AnythingLLM MCP Server

基于 MCP SDK 的 AnythingLLM 工作区 MCP 服务，把 AnythingLLM 的能力暴露为 MCP 工具，供支持 MCP 的客户端（含本仓库 xAgent）连接调用。

## 配置

通过环境变量注入连接信息（不在代码里写死密钥）：

```bash
set ANYTHINGLLM_BASE=http://localhost:3001
set ANYTHINGLLM_API_KEY=your-api-key
set WORKSPACE_SLUG=your-workspace-slug
```

`ANYTHINGLLM_API_KEY` 和 `WORKSPACE_SLUG` 必须显式配置，服务不会使用内置密钥或工作区。

## 启动

```bash
pip install -r requirements.txt
python server.py
```

MCP 端点为 `http://127.0.0.1:8081/mcp`，健康检查为 `http://127.0.0.1:8081/health`。

服务使用 MCP SDK 2.x 的 `MCPServer`，Streamable HTTP 协议，启用无状态模式（stateless），可直接被 MCP Inspector 或 SDK 客户端连接。端口可通过 `MCP_PORT` 覆盖（默认 8081，避免与宠物医院 Go 后端的 8080 冲突）。

## 工具

- `ask_workspace`：向工作区提问，返回 AI 回答。
- `get_workspace`：获取工作区配置、文档和线程摘要。
- `list_workspace_chats`：获取聊天记录，可传 `limit`、`order_by`、`api_session_id`。
- `vector_search`：在工作区文档中做向量相似度搜索，可传 `top_n`、`score_threshold`。

## 测试

可用 MCP Inspector 连接 `http://127.0.0.1:8081/mcp` 查看并调用上述工具；或使用本仓库 xAgent（配置根目录 `.mcp.json` 后启动 `Agent/xAgent/chat.py`）。
