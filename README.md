# 大语言模型课程项目

本仓库包含四个组件，构成「宠物医院 MCP 生态 + 心理咨询 Agent」的完整演示。

| 组件 | 路径 | 说明 |
| --- | --- | --- |
| 宠物医院 MCP | `petHospital_MCP/` | Go 宠物医院系统（内嵌网页 + REST API）+ Python MCP 适配服务 |
| AnythingLLM MCP | `anythingllm-mcp/` | 基于 MCP SDK 的 AnythingLLM 工作区 MCP 服务 |
| AnythingLLM 网页 | `AnythingLLMServer/` | 文档上传工作台网页（`upload.html`）+ 演示用 Mock 后端 |
| AI Agent | `Agent/xAgent/` | 心理咨询 Agent（CBT 取向，DeepSeek 驱动），支持 Skills 动态加载、MCP 客户端、上下文压缩 |

## 组件一：宠物医院 MCP

- Go 后端（内嵌网页 + 29 个 REST 接口）：
  `petHospital_MCP/pethospital.exe` → `http://127.0.0.1:8080/`
- Python MCP 适配（16 个 MCP 工具）：
  `cd petHospital_MCP/pet_hospital_mcp && pip install -e . && python -m pet_hospital_mcp` → `http://127.0.0.1:8000/mcp`

## 组件二 / 三：AnythingLLM MCP 与网页

这两个组件依赖 AnythingLLM 后端。真实 AnythingLLM 需要 Docker / 原生安装；本仓库提供
零依赖的 **Mock 后端** 用于演示（无需安装 AnythingLLM）：

1. 启动 Mock 后端：`python AnythingLLMServer/mock_backend.py` → `http://127.0.0.1:3001`
2. 打开网页：浏览器打开 `AnythingLLMServer/upload.html`（默认连接 `http://localhost:3001/api`）
3. 启动 MCP：`cd anythingllm-mcp && pip install -r requirements.txt && python server.py`，
   需先设置环境变量（PowerShell 用 `$env:NAME`）：
   ```bash
   set ANYTHINGLLM_API_KEY=demo-key
   set WORKSPACE_SLUG=demo
   ```
   → `http://127.0.0.1:8081/mcp`

> 接入真实 AnythingLLM：把 `ANYTHINGLLM_BASE` 指向真实服务、配置真实 API Key 与工作区 slug 即可，无需 Mock 后端。

## 组件四：心理咨询 Agent（xAgent）

- 运行：在仓库根目录执行 `python Agent/xAgent/chat.py`（全屏 TUI）
- 人设：「安心」，CBT（认知行为治疗）取向心理咨询师，含危机干预安全边界
- 内置 6 个心理咨询技能（CBT 治疗师、心理健康、心理健康助手、治疗师筛查、亲密关系治疗、治疗流派），位于 `Agent/xAgent/.agents/skills/`
- 通过根目录 `.mcp.json` 自动连接上述 MCP 服务（异步加载，TUI 边栏实时展示服务器与工具）
- 首次运行自动生成 `Agent/xAgent/config.ini` 模板，填入 DeepSeek / 百度千帆密钥后重启
- 斜杠命令：`/new /resume /save /compact /clear /help /skills /mcp /quit`

## 目录结构

```text
.
├── .mcp.json                        # MCP 服务器连接配置（根目录）
├── petHospital_MCP/                 # 组件一：宠物医院 MCP
├── anythingllm-mcp/                 # 组件二：AnythingLLM MCP
├── AnythingLLMServer/               # 组件三：AnythingLLM 网页 + Mock 后端
└── Agent/xAgent/                    # 组件四：心理咨询 Agent
    ├── chat.py / tui.py             # 终端界面与主流程
    ├── config.py / sessions.py      # 配置与会话持久化
    ├── skills.py / context.py       # 技能加载与上下文压缩
    ├── mcp_client.py                # MCP 客户端（异步发现）
    └── .agents/skills/              # 6 个心理咨询技能
```
