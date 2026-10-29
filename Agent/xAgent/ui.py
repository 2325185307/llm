import json
import os
import sys

import gradio as gr

# 把脚本所在目录加入搜索路径，确保无论从哪个目录启动都能 import 到 chat.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用 chat.py 中的 LLM 客户端、工具说明与执行函数，避免重复定义
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
    """清空对话：重置 OpenAI 历史并返回空列表刷新界面。"""
    global openai_history
    openai_history = [SYSTEM_MESSAGE]
    return []


with gr.Blocks(title="xAgent 交互界面") as demo:
    gr.Markdown("# xAgent 交互界面")
    gr.Markdown(
        "可调用本地命令行工具（RunCli）、联网搜索（searchWeb）与网页抓取（fetchWeb）。"
        "每次工具调用都会在对话中实时展示参数与返回结果。"
    )

    chatbot = gr.Chatbot(height=520, buttons=["copy"])

    with gr.Row():
        msg = gr.Textbox(
            placeholder="请输入你的问题，回车发送…", scale=8, show_label=False
        )
        send = gr.Button("发送", variant="primary", scale=1)
        clear = gr.Button("清空", scale=1)

    # 回车或点击“发送”都会触发 respond；完成后清空输入框
    msg.submit(respond, [msg, chatbot], [chatbot]).then(lambda: "", None, msg)
    send.click(respond, [msg, chatbot], [chatbot]).then(lambda: "", None, msg)
    clear.click(clear_chat, None, chatbot)


if __name__ == "__main__":
    demo.launch()
