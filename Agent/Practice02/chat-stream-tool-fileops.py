import configparser
import json
import os
import shutil
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

# 读取文件内容的上限，避免把超大文件塞进上下文
MAX_READ_BYTES = 1024 * 1024  # 1MB


def _decode(data: bytes) -> str:
    """把字节解码成文本，先按严格的 UTF-8，失败后回退到 GBK。"""
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


# ---------------------------------------------------------------------------
# 已有的两个工具：getdate、isleapyear
# ---------------------------------------------------------------------------

def get_date():
    """
    工具：获取当前电脑的日期和时间。
    按要求调用本地 date / time 命令来获取，而不是使用 Python 的 datetime 模块。
    """
    try:
        date_str = _run_cmd(["cmd", "/c", "date", "/t"])
        time_str = _run_cmd(["cmd", "/c", "time", "/t"])
        return f"当前日期：{date_str}；当前时间：{time_str}"
    except Exception as exc:
        return f"获取日期时间失败：{exc}"


def is_leap_year(year):
    """
    工具：判断输入的年份是否是闰年（用 Python 代码计算）。
    闰年规则：能被 4 整除且不能被 100 整除，或者能被 400 整除。
    """
    try:
        year = int(year)
    except (TypeError, ValueError):
        return f"{year} 不是合法的年份"
    if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
        return f"{year} 年是闰年"
    return f"{year} 年不是闰年"


# ---------------------------------------------------------------------------
# 新增的 10 个本地文件操作工具（统一用 Python 标准库实现）
# ---------------------------------------------------------------------------

def get_cwd():
    """工具：获取当前工作目录。"""
    return os.getcwd()


def list_dir(path):
    """工具：列出指定目录下的所有文件和子目录。"""
    if not os.path.isdir(path):
        return f"{path} 不是有效的目录"
    try:
        entries = os.listdir(path)
    except Exception as exc:
        return f"列出目录失败：{exc}"
    if not entries:
        return f"{path} 是空目录"

    dirs = sorted(e for e in entries if os.path.isdir(os.path.join(path, e)))
    files = sorted(e for e in entries if os.path.isfile(os.path.join(path, e)))
    lines = [f"目录 {os.path.abspath(path)} 包含 {len(dirs)} 个子目录、{len(files)} 个文件："]
    for d in dirs:
        lines.append(f"  [目录] {d}/")
    for f in files:
        lines.append(f"  [文件] {f}")
    return "\n".join(lines)


def get_file_type(path):
    """工具：获取指定文件的文件类型（根据扩展名判断）。"""
    if not os.path.exists(path):
        return f"{path} 不存在"
    if os.path.isdir(path):
        return f"{path} 是一个目录"

    ext = os.path.splitext(path)[1].lower()
    type_map = {
        ".txt": "纯文本文件",
        ".md": "Markdown 文档",
        ".py": "Python 源码",
        ".js": "JavaScript 源码",
        ".json": "JSON 文件",
        ".csv": "CSV 表格",
        ".html": "网页 (HTML)",
        ".css": "样式表 (CSS)",
        ".ini": "配置文件",
        ".log": "日志文件",
        ".jpg": "图片 (JPEG)",
        ".jpeg": "图片 (JPEG)",
        ".png": "图片 (PNG)",
        ".gif": "图片 (GIF)",
        ".pdf": "PDF 文档",
        ".docx": "Word 文档",
        ".xlsx": "Excel 表格",
    }
    type_name = type_map.get(ext, f"扩展名为 {ext or '（无扩展名）'} 的文件")
    return f"{path} 的文件类型：{type_name}"


def read_file(path):
    """工具：获取指定文件的内容（仅支持纯文本格式文件）。"""
    if not os.path.exists(path):
        return f"{path} 不存在"
    if os.path.isdir(path):
        return f"{path} 是目录，无法读取内容"
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as exc:
        return f"读取文件失败：{exc}"

    if b"\x00" in data:
        return f"{path} 是二进制文件，仅支持纯文本格式文件"
    if len(data) > MAX_READ_BYTES:
        return f"{path} 文件过大（超过 {MAX_READ_BYTES} 字节），无法直接读取"
    return _decode(data)


def create_file(directory, filename):
    """工具：根据指定的目录和文件名创建新文件（空文件）。"""
    if not os.path.isdir(directory):
        return f"目录 {directory} 不存在，请先创建该目录"
    path = os.path.join(directory, filename)
    if os.path.exists(path):
        return f"{path} 已存在"
    try:
        with open(path, "w", encoding="utf-8"):
            pass
    except Exception as exc:
        return f"创建文件失败：{exc}"
    return f"已创建文件：{path}"


def edit_file(path, content):
    """工具：编辑指定文件的内容（覆盖写入，仅支持纯文本格式文件）。"""
    if not os.path.exists(path):
        return f"{path} 不存在"
    if os.path.isdir(path):
        return f"{path} 是目录，无法编辑"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        return f"写入文件失败：{exc}"
    return f"已写入文件：{path}"


def create_dir(path):
    """工具：创建新目录（含中间层目录）。"""
    if os.path.exists(path):
        return f"{path} 已存在"
    try:
        os.makedirs(path)
    except Exception as exc:
        return f"创建目录失败：{exc}"
    return f"已创建目录：{path}"


def delete_dir(path):
    """工具：删除目录（含目录内所有内容，即非空目录也可删除）。"""
    if not os.path.exists(path):
        return f"{path} 不存在"
    if not os.path.isdir(path):
        return f"{path} 不是目录"
    try:
        shutil.rmtree(path)
    except Exception as exc:
        return f"删除目录失败：{exc}"
    return f"已删除目录：{path}"


def delete_file(path):
    """工具：删除文件。"""
    if not os.path.exists(path):
        return f"{path} 不存在"
    if os.path.isdir(path):
        return f"{path} 是目录，请使用删除目录功能"
    try:
        os.remove(path)
    except Exception as exc:
        return f"删除文件失败：{exc}"
    return f"已删除文件：{path}"


def rename_item(path, new_name):
    """工具：重命名文件或目录（新名称仅为名称，不能包含路径）。"""
    if not os.path.exists(path):
        return f"{path} 不存在"
    if new_name != os.path.basename(new_name):
        return f"新名称 {new_name} 不合法，只能包含名称，不能包含路径分隔符"
    new_path = os.path.join(os.path.dirname(path), new_name)
    if os.path.exists(new_path):
        return f"{new_path} 已存在"
    try:
        os.rename(path, new_path)
    except Exception as exc:
        return f"重命名失败：{exc}"
    return f"已将 {path} 重命名为 {new_path}"


# ---------------------------------------------------------------------------
# 工具说明（告诉模型有哪些函数、各自用途和参数）
# ---------------------------------------------------------------------------

tools = [
    {
        "type": "function",
        "function": {
            "name": "getdate",
            "description": "获取当前电脑的日期和时间",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "isleapyear",
            "description": "判断输入的年份是否是闰年",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "要判断的年份，例如 2026"},
                },
                "required": ["year"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "getcwd",
            "description": "获取当前工作目录",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listdir",
            "description": "列出指定目录下的所有文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要列出的目录路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "getfiletype",
            "description": "获取指定文件的文件类型（根据扩展名判断）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "readfile",
            "description": "获取指定文件的内容（仅支持纯文本格式文件）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "createfile",
            "description": "根据指定的目录和文件名创建新文件（空文件）",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "要创建文件的目录路径"},
                    "filename": {"type": "string", "description": "新文件的文件名"},
                },
                "required": ["directory", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "editfile",
            "description": "编辑指定文件的内容（覆盖写入，仅支持纯文本格式文件）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要编辑的文件路径"},
                    "content": {"type": "string", "description": "要写入文件的新内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "createdir",
            "description": "创建新目录（含中间层目录）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要创建的目录路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deletedir",
            "description": "删除目录（含目录内所有内容，非空目录也可删除）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的目录路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deletefile",
            "description": "删除文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename",
            "description": "重命名文件或目录（新名称仅为名称，不能包含路径）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要重命名的文件或目录的当前路径"},
                    "new_name": {"type": "string", "description": "新的名称（仅名称，不含路径）"},
                },
                "required": ["path", "new_name"],
            },
        },
    },
]

# 工具名到实际执行函数的映射
tool_functions = {
    "getdate": get_date,
    "isleapyear": is_leap_year,
    "getcwd": get_cwd,
    "listdir": list_dir,
    "getfiletype": get_file_type,
    "readfile": read_file,
    "createfile": create_file,
    "editfile": edit_file,
    "createdir": create_dir,
    "deletedir": delete_dir,
    "deletefile": delete_file,
    "rename": rename_item,
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
