import json
import os
import sys
import time

import gradio as gr

# 把脚本所在目录加入搜索路径，确保无论从哪个目录启动都能 import 到 chat.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用 chat.py 中的 LLM 客户端、工具说明与执行函数，避免重复定义
import chat
from chat import client, section, tools, tool_functions, SYSTEM_MESSAGE

# OpenAI 消息历史（与界面显示分开维护，供 API 调用使用），以 System Prompt 开头
openai_history = [SYSTEM_MESSAGE]


def respond(message, history):
    """处理一轮对话：先执行工具调用循环，再流式输出最终答案。

    参数 history 是界面上当前显示的消息列表（元素为 {role, content} 字典）。
    本函数是一个生成器，每 yield 一次就刷新一次界面。
    """
    history = history or []
    history.append({"role": "user", "content": message})
    yield history

    openai_history.append({"role": "user", "content": message})

    try:
        # 第一轮：非流式调用，处理模型可能发起的工具调用，直到模型给出最终回答
        while True:
            response = client.chat.completions.create(
                model=section["model"],
                messages=openai_history,
                tools=tools,
                tool_choice="auto",
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                break

            openai_history.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )

            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")

                # 在界面上展示“正在调用哪个工具、传了什么参数”
                history.append(
                    {
                        "role": "assistant",
                        "content": f"🔧 调用工具 `{name}`，参数：`{json.dumps(args, ensure_ascii=False)}`",
                    }
                )
                yield history

                result = tool_functions[name](**args)
                openai_history.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )

                # 在界面上展示该工具的返回结果
                history.append(
                    {
                        "role": "assistant",
                        "content": f"📋 工具 `{name}` 返回：\n\n```\n{result}\n```",
                    }
                )
                yield history

        # 第二轮：流式输出最终答案
        stream = client.chat.completions.create(
            model=section["model"],
            messages=openai_history,
            stream=True,
        )

        history.append({"role": "assistant", "content": ""})
        reply = ""
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                reply += delta
                history[-1]["content"] = reply
                yield history

        openai_history.append({"role": "assistant", "content": reply})

    except Exception as exc:
        history.append({"role": "assistant", "content": f"❌ 出错了：{exc}"})
        yield history


def clear_chat():
    """清空对话：重置 OpenAI 历史并返回带欢迎语的初始界面。"""
    global openai_history
    openai_history = [SYSTEM_MESSAGE]
    return [{"role": "assistant", "content": WELCOME_TEXT}]


def _wait_mcp(timeout: float = 10.0) -> bool:
    """等待 MCP 工具异步发现完成并合并进 tools / tool_functions（最多 timeout 秒）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chat.sync_mcp_tools():
            return True
        time.sleep(0.3)
    return chat.sync_mcp_tools()


WELCOME_TEXT = (
    "你好呀，我是「心安」🪷 很高兴见到你。\n\n"
    "我是一位 CBT（认知行为治疗）取向的 AI 心理咨询伙伴，"
    "会陪着你梳理情绪、看清想法，慢慢找到属于你自己的答案。\n\n"
    "你可以从下面任意一个话题开始，或者直接告诉我你现在的心情。"
)

EXAMPLES = [
    "我最近压力很大，晚上总睡不好",
    "总是忍不住否定自己，怎么办",
    "和家人的关系让我很焦虑",
    "一上台就紧张到发抖，怎么克服",
]

# —— 视觉主题与样式 ——
THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.teal,
    secondary_hue=gr.themes.colors.violet,
    neutral_hue=gr.themes.colors.slate,
)

CSS = """
.gradio-container {
    max-width: 1180px !important;
    margin: 0 auto !important;
    font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", sans-serif;
}
body {
    background: linear-gradient(135deg, #eef2ff 0%, #ecfeff 55%, #f0fdfa 100%);
}
footer { display: none !important; }
"""

HEADER_HTML = """
<div style="text-align:center; padding:26px 12px 6px;">
    <div style="font-size:44px; line-height:1;">🪷</div>
    <h1 style="margin:10px 0 6px; font-size:32px; font-weight:800; letter-spacing:1px;
        background:linear-gradient(90deg,#0d9488,#7c3aed);
        -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;">
        心安 · AI 心理咨询
    </h1>
    <p style="color:#64748b; margin:0; font-size:14px;">温暖陪伴 · CBT 认知行为取向 · 仅心理支持，非医学诊断</p>
</div>
"""

SIDEBAR_HTML = """
<div style="background:rgba(255,255,255,.7); border-radius:16px; padding:20px; box-shadow:0 4px 20px rgba(13,148,136,.08);">
    <h3 style="margin:0 0 8px; color:#0f766e;">🪷 关于心安</h3>
    <p style="color:#475569; font-size:13px; line-height:1.7; margin:0 0 16px;">
        我是你的 AI 心理咨询伙伴，陪你梳理情绪、看清想法，
        一起找到你自己的答案，而不是替你决定。
    </p>
    <h3 style="margin:0 0 8px; color:#0f766e;">✨ 我能帮你</h3>
    <ul style="color:#475569; font-size:13px; line-height:1.9; margin:0 0 16px; padding-left:18px;">
        <li>情绪疏导与压力管理</li>
        <li>认知重构：打破负面思维循环</li>
        <li>焦虑、失眠、人际关系的自助练习</li>
        <li>苏格拉底式提问，引导自我觉察</li>
    </ul>
    <h3 style="margin:0 0 8px; color:#0f766e;">🧰 可调用能力</h3>
    <p style="color:#64748b; font-size:12px; line-height:1.7; margin:0 0 16px;">
        联网搜索、网页抓取、心理技能（CBT 等），并可通过 MCP 连接宠物医院与 AnythingLLM 知识库。
    </p>
    <h3 style="margin:0 0 8px; color:#b91c1c;">⚠️ 应急热线</h3>
    <p style="color:#64748b; font-size:12px; line-height:1.8; margin:0;">
        全国统一心理援助热线：<b>400-161-9995</b><br>
        北京心理危机研究与干预中心：<b>010-82951332</b>
    </p>
</div>
"""

FOOTER_HTML = """
<div style="text-align:center; color:#94a3b8; font-size:12px; padding:18px 12px 24px; line-height:1.8;">
    「心安」仅提供心理支持与自助引导，不构成医疗诊断或治疗。<br>
    如出现自伤、伤人念头，请立即拨打心理援助热线 400-161-9995 或前往就近医院急诊。
</div>
"""


with gr.Blocks(title="心安 · AI 心理咨询") as demo:
    gr.HTML(HEADER_HTML)

    with gr.Row():
        # 左侧：介绍与安全信息
        with gr.Column(scale=1, min_width=250):
            gr.HTML(SIDEBAR_HTML)

        # 右侧：对话区
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(
                layout="bubble",
                height=540,
                placeholder="和心安聊聊你的心事…",
                buttons=["copy"],
                value=[{"role": "assistant", "content": WELCOME_TEXT}],
            )

            with gr.Row():
                msg = gr.Textbox(
                    placeholder="和心安聊聊你的心事… 回车发送",
                    scale=8,
                    show_label=False,
                    container=False,
                )
                send = gr.Button("发送", variant="primary", scale=1)
                clear = gr.Button("清空", scale=1)

            gr.Markdown("**💡 试着这样开始：**")
            with gr.Row():
                for ex in EXAMPLES:
                    gr.Button(ex, variant="secondary", scale=1).click(
                        lambda e=ex: e, None, msg
                    )

    gr.HTML(FOOTER_HTML)

    # 回车或点击“发送”都会触发 respond；完成后清空输入框
    msg.submit(respond, [msg, chatbot], [chatbot]).then(lambda: "", None, msg)
    send.click(respond, [msg, chatbot], [chatbot]).then(lambda: "", None, msg)
    clear.click(clear_chat, None, chatbot)


if __name__ == "__main__":
    _wait_mcp()
    demo.launch(server_name="127.0.0.1", theme=THEME, css=CSS)
