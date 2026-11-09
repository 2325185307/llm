# 大语言模型课程项目

本仓库包含四个组件，构成「宠物医院 MCP 生态 + AI Agent」的完整演示。

| 组件 | 路径 | 说明 |
| --- | --- | --- |
| 宠物医院 MCP | `petHospital_MCP/` | Go 宠物医院系统（内嵌网页 + REST API）+ Python MCP 适配服务 |
| AnythingLLM MCP | `anythingllm-mcp/` | 基于 MCP SDK 的 AnythingLLM 工作区 MCP 服务 |
| AnythingLLM 网页 | `AnythingLLMServer/upload.html` | 文档上传工作台网页（连接 AnythingLLM API） |
| AI Agent | `Agent/xAgent/` | DeepSeek 驱动的 Agent，支持 Skills 动态加载、MCP 客户端、上下文压缩 |

## 启动

1. 宠物医院 Go 后端：`petHospital_MCP/pethospital.exe`（网页 `http://127.0.0.1:8080/`）
2. 宠物医院 MCP：`cd petHospital_MCP/pet_hospital_mcp && pip install -e . && python -m pet_hospital_mcp`（`http://127.0.0.1:8000/mcp`）
3. AnythingLLM MCP：`cd anythingllm-mcp && pip install -r requirements.txt && python server.py`（`http://127.0.0.1:8081/mcp`，需配置 `ANYTHINGLLM_*` 环境变量）
4. xAgent：在仓库根目录运行 `python Agent/xAgent/chat.py`，通过根目录 `.mcp.json` 连接上述 MCP 服务
