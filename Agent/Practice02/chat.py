import configparser
import json
import os
import subprocess
import sys

from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 读取与脚本同目录下的 config.ini，避免受启动目录影响
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")
section = config["llm"]

client = OpenAI(base_url=section["base_url"], api_key=section["api_key"])


def _decode(data: bytes) -> str:
    """把命令输出从字节解码成文本。

    GBK 几乎能解码任意字节序列，因此先按严格的 UTF-8 尝试，失败后再回退到 GBK，
    避免把 UTF-8 输出误解码成乱码。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _run_cmd(args):
    """运行本地命令并返回解码后的标准输出（去掉首尾空白）。"""
    result = subprocess.run(args, capture_output=True)
    return _decode(result.stdout).strip()


def get_date():
    """
    工具：获取当前电脑的日期和时间。

    按要求调用本地 date / time 命令来获取，而不是使用 Python 的 datetime 模块。
    Windows 下 date / time 是 cmd 的内置命令，需要经 cmd /c 调用，/t 表示只显示不修改。
    """
    try:
        date_str = _run_cmd(["cmd", "/c", "date", "/t"])
        time_str = _run_cmd(["cmd", "/c", "time", "/t"])
        return f"当前日期：{date_str}；当前时间：{time_str}"
    except Exception as exc:
        return f"获取日期时间失败：{exc}"


# 告诉模型的工具说明（函数名、用途、参数）
tools = [
    {
        "type": "function",
        "function": {
            "name": "getdate",
            "description": "获取当前电脑的日期和时间",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

# 工具名到实际执行函数的映射
tool_functions = {
    "getdate": get_date,
}

history = []

while True:
    prompt = input("please input your prompt：")
    print("---")
    history.append({"role": "user", "content": prompt})

    # 第一轮：非流式调用，处理模型可能发起的工具调用，直到模型给出最终回答
    while True:
        response = client.chat.completions.create(
            model=section["model"],
            messages=history,
            tools=tools,
            tool_choice="auto",
        )
        message = response.choices[0].message

        if not message.tool_calls:
            break  # 模型不再需要工具，进入下面的流式输出

        # 把 assistant 的“请求调用工具”这条消息追加进历史
        history.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ],
            }
        )

        # 依次执行每个工具，并把结果作为 tool 消息追加进历史
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments or "{}")
            result = tool_functions[name](**args)
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )

    # 第二轮：流式输出最终答案
    stream = client.chat.completions.create(
        model=section["model"],
        messages=history,
        stream=True,
    )

    reply = ""
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            reply += delta
            print(delta, end="", flush=True)
    print()
    history.append({"role": "assistant", "content": reply})
