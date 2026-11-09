"""AnythingLLM 模拟后端（演示用 Mock）。

真实 AnythingLLM 需要 Docker / 原生安装，本仓库的 AnythingLLM MCP 与「文档工作台」
网页都依赖 `http://localhost:3001` 的 AnythingLLM API。为了让这两个组件在本机无需安装
AnythingLLM 即可演示「能用」，这里提供一个零依赖的 Mock 后端，实现它们所需的最小接口集：

  GET  /health
  GET  /api/v1/workspaces                       —— 网页加载工作区列表
  GET  /api/v1/workspace/{slug}                 —— anythingllm-mcp.get_workspace
  POST /api/v1/workspace/{slug}/chat            —— anythingllm-mcp.ask_workspace
  GET  /api/v1/workspace/{slug}/chats           —— anythingllm-mcp.list_workspace_chats
  POST /api/v1/workspace/{slug}/vector-search   —— anythingllm-mcp.vector_search
  POST /api/v1/document/upload                  —— 网页上传文件

所有响应均带 CORS 头，保证网页以 file:// 打开时也能跨域访问。

启动：
    python mock_backend.py            # 默认 http://127.0.0.1:3001
    python mock_backend.py --port 3001

接入真实 AnythingLLM 时无需本文件：直接把 anythingllm-mcp 的 ANYTHINGLLM_BASE 指向真实
服务、配置真实 API Key 与 WORKSPACE_SLUG 即可。
"""

from __future__ import annotations

import json
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOST = "127.0.0.1"
PORT = 3001

# 演示用工作区
WORKSPACES = [
    {"id": 1, "name": "演示工作区", "slug": "demo", "description": "默认演示工作区"},
    {"id": 2, "name": "宠物医院知识库", "slug": "pet-hospital", "description": "宠物医院文档知识库"},
]

DOCUMENTS = {
    "demo": [
        {"id": 101, "title": "宠物医院运营手册.md", "status": "ready"},
        {"id": 102, "title": "常见宠物疾病诊疗指南.pdf", "status": "ready"},
    ],
    "pet-hospital": [
        {"id": 201, "title": "犬猫疫苗接种规范.md", "status": "ready"},
        {"id": 202, "title": "手术流程与注意事项.pdf", "status": "ready"},
    ],
}


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "AnythingLLM-Mock/0.1"

    def _send(self, status: int, payload, content_type="application/json; charset=utf-8"):
        body = _json_bytes(payload) if not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # CORS：网页以 file:// 打开时跨域访问所需
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _slug(self, prefix: str) -> str:
        """从路径中提取 /api/v1/workspace/{slug}/... 的 slug。"""
        rest = self.path[len(prefix):]
        return rest.split("/", 1)[0]

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self._send(200, {"status": "ok", "service": "anythingllm-mock"})

        if path == "/api/v1/workspaces":
            return self._send(200, {"workspaces": WORKSPACES})

        if path.startswith("/api/v1/workspace/") and path.endswith("/chats"):
            slug = path[len("/api/v1/workspace/"):-len("/chats")]
            return self._send(200, {
                "history": [
                    {
                        "id": 1, "slug": slug, "role": "user",
                        "content": "小猫打喷嚏是感冒了吗？", "createdAt": "2026-11-09T09:00:00.000Z",
                    },
                    {
                        "id": 2, "slug": slug, "role": "assistant",
                        "content": "打喷嚏可能是感冒、异物刺激或过敏，建议观察是否伴随流涕、食欲下降，必要时就医。",
                        "createdAt": "2026-11-09T09:00:05.000Z",
                    },
                ]
            })

        if path.startswith("/api/v1/workspace/"):
            slug = path[len("/api/v1/workspace/"):]
            return self._send(200, {
                "id": 1, "name": "演示工作区", "slug": slug,
                "documents": DOCUMENTS.get(slug, DOCUMENTS["demo"]),
                "threads": [{"slug": "thread-1", "name": "默认会话"}],
                "settings": {"similarityThreshold": 0.25},
            })

        return self._send(404, {"error": f"未找到路径 {path}"})

    def do_POST(self):
        path = urlsplit(self.path).path
        body = self._read_body()

        if path == "/api/v1/document/upload":
            # 演示：不解析 multipart，直接返回成功
            return self._send(200, {
                "success": True,
                "documents": [{"id": 999, "title": "已接收文件（演示）", "status": "ready"}],
                "message": "上传成功（Mock）",
            })

        if path.startswith("/api/v1/workspace/") and path.endswith("/chat"):
            slug = self._slug("/api/v1/workspace/")
            question = ""
            try:
                question = json.loads(body.decode("utf-8") if body else "{}").get("message", "")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            text = (
                f"（AnythingLLM 工作区 {slug} 演示回答）我已理解你的问题：「{question}」。"
                "这是 Mock 后端返回的 AI 回复，用于演示 ask_workspace 工具链路。"
            )
            return self._send(200, {"type": "textResponse", "textResponse": text, "sources": []})

        if path.startswith("/api/v1/workspace/") and path.endswith("/vector-search"):
            slug = self._slug("/api/v1/workspace/")
            query = ""
            try:
                query = json.loads(body.decode("utf-8") if body else "{}").get("query", "")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            return self._send(200, {
                "results": [
                    {"text": f"工作区 {slug} 中与「{query}」相关的文档片段（演示）", "score": 0.91,
                     "metadata": {"source": "常见宠物疾病诊疗指南.pdf"}},
                    {"text": "另一个相似片段（演示）", "score": 0.78,
                     "metadata": {"source": "宠物医院运营手册.md"}},
                ]
            })

        return self._send(404, {"error": f"未找到路径 {path}"})

    def log_message(self, fmt, *args):
        # 精简访问日志，便于演示
        print(f"[anythingllm-mock] {self.command} {self.path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AnythingLLM Mock 后端")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AnythingLLM Mock 后端已启动：http://{args.host}:{args.port}")
    print("端点：/api/v1/workspaces · /api/v1/workspace/{slug}(/chats /chat /vector-search) · /api/v1/document/upload")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
