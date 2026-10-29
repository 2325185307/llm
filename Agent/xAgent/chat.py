import configparser
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import trafilatura
from openai import OpenAI

import skills

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 读取与脚本同目录下的 config.ini，避免受启动目录影响
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")

section = config["llm"]
client = OpenAI(base_url=section["base_url"], api_key=section["api_key"])

# 百度千帆智能搜索（原始搜索结果）接口
SEARCH_API_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"

# 网页正文返回给 LLM 的长度上限，避免超长页面占用过多上下文
MAX_MARKDOWN_CHARS = 12000

# RunCli 命令执行的超时（秒）
CLI_TIMEOUT = 30
# RunCli 返回给 LLM 的 stdout / stderr 长度上限，避免超长输出占用过多上下文
MAX_STDOUT_CHARS = 4000
MAX_STDERR_CHARS = 2000


def _decode(data: bytes) -> str:
    """把命令输出的字节解码成文本：先按 UTF-8，失败后回退到 GBK（中文 Windows 常见）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int) -> str:
    """截断过长的文本，并标明截断情况。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（内容过长，已截断，共 {len(text)} 字符）"


def _get_search_api_key():
    """从 config.ini 的 [search] 小节读取百度千帆 API Key，不在代码中硬编码。"""
    if not config.has_section("search"):
        return None, "config.ini 中缺少 [search] 小节，请添加 baidu_qianfan_api_key 配置"
    key = config["search"].get("baidu_qianfan_api_key", "").strip()
    if not key:
        return None, "config.ini 中 baidu_qianfan_api_key 为空，请先配置 API Key"
    return key, None


def _find_item_list(obj):
    """在响应 JSON 中递归查找由搜索结果条目组成的列表（每条含标题和 URL）。"""
    if isinstance(obj, dict):
        for value in obj.values():
            found = _find_item_list(value)
            if found:
                return found
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            sample = obj[0]
            if ("title" in sample or "name" in sample) and ("url" in sample or "link" in sample):
                return obj
        for item in obj:
            found = _find_item_list(item)
            if found:
                return found
    return None


def _format_search_results(data):
    """把 API 返回整理成：搜索摘要 + 参考来源标题 + URL。"""
    # 优先处理 API 层面的错误返回
    if isinstance(data, dict):
        if data.get("error_code") or data.get("error_msg"):
            return f"搜索 API 返回错误：{data.get('error_code')} {data.get('error_msg')}".strip()
        if data.get("code") is not None and data.get("message"):
            return f"搜索 API 返回错误：{data.get('code')} {data.get('message')}".strip()

    items = _find_item_list(data)
    if not items:
        raw = json.dumps(data, ensure_ascii=False)
        return f"搜索成功，但未能解析结果结构，原始返回如下：\n{raw[:2000]}"

    lines = [f"共搜索到 {len(items)} 条结果，以下为前 {min(len(items), 10)} 条："]
    for i, item in enumerate(items[:10], 1):
        title = item.get("title") or item.get("name") or "(无标题)"
        url = item.get("url") or item.get("link") or ""
        snippet = (
            item.get("abstract")
            or item.get("content")
            or item.get("snippet")
            or item.get("summary")
            or item.get("description")
            or ""
        )
        snippet = str(snippet).strip().replace("\n", " ")
        if len(snippet) > 300:
            snippet = snippet[:300] + "…"
        lines.append(f"{i}. {title}")
        if snippet:
            lines.append(f"   摘要：{snippet}")
        if url:
            lines.append(f"   来源：{url}")
    return "\n".join(lines)


def search_web(query):
    """工具：联网搜索，返回搜索摘要、参考来源标题及 URL。"""
    api_key, err = _get_search_api_key()
    if err:
        return f"错误：{err}"

    payload = {
        "messages": [{"role": "user", "content": query}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": 10}],
    }
    req = urllib.request.Request(
        SEARCH_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return f"错误：搜索请求失败（HTTP {exc.code} {exc.reason}）。{detail}".strip()
    except urllib.error.URLError as exc:
        return f"错误：网络请求失败：{exc.reason}"
    except TimeoutError:
        return "错误：网络请求超时（30 秒）"
    except json.JSONDecodeError:
        return "错误：搜索 API 返回了无法解析的内容"
    except Exception as exc:
        return f"错误：搜索请求出错：{exc}"

    return _format_search_results(data)


def fetch_web(url):
    """工具：获取指定网页的主要内容，返回 Markdown 正文 + 原始 URL。

    用 subprocess 以参数列表方式调用本地 curl 获取 HTML（不使用 shell=True），
    再用 trafilatura 提取正文并转为 Markdown，去除导航、广告、页脚等无关内容。
    """
    # 1. curl 抓取 HTML（参数列表方式）
    cmd = [
        "curl",
        "-L",  # 跟随重定向
        "--connect-timeout", "10",  # 连接超时（秒）
        "--max-time", "20",  # 整体超时（秒）
        "-s",  # 静默模式，不输出进度
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=40)
    except FileNotFoundError:
        return "错误：系统未找到 curl 程序，请确认已安装 curl 并加入 PATH"
    except subprocess.TimeoutExpired:
        return "错误：curl 请求超时"
    except Exception as exc:
        return f"错误：调用 curl 失败：{exc}"

    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        if not err:
            err = f"退出码 {result.returncode}"
        return f"错误：网页访问失败（curl 退出码 {result.returncode}）：{err[:500]}"

    if not result.stdout:
        return "错误：网页返回内容为空"

    # 2. trafilatura 提取正文并转换为 Markdown（传原始 bytes，由库自行识别编码）
    try:
        markdown = trafilatura.extract(
            result.stdout,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_formatting=True,
            include_comments=False,
            deduplicate=True,
        )
    except Exception as exc:
        return f"错误：HTML 解析失败：{exc}"

    if not markdown or not markdown.strip():
        return f"错误：无法从该网页提取出正文内容：{url}"

    if len(markdown) > MAX_MARKDOWN_CHARS:
        markdown = markdown[:MAX_MARKDOWN_CHARS] + "\n\n…（内容过长，已截断）"

    return f"来源：{url}\n\n{markdown}"


def run_cli(command, args):
    """工具：调用本地已安装的任意命令行程序（CLI）。

    把 command 与 args 拼装成参数列表，通过 subprocess 以 shell=False 方式执行，
    返回标准输出、标准错误和退出码，供 LLM 判断执行结果并决定下一步操作。
    """
    if not isinstance(args, list):
        return "错误：args 参数必须是字符串列表"

    cmd = [command] + [str(a) for a in args]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=CLI_TIMEOUT)
    except FileNotFoundError:
        return f"错误：找不到命令 {command}，请确认该程序已安装并加入 PATH"
    except subprocess.TimeoutExpired:
        return f"错误：命令 {command} 执行超时（超过 {CLI_TIMEOUT} 秒）"
    except Exception as exc:
        return f"错误：调用命令 {command} 失败：{exc}"

    stdout = _truncate(_decode(result.stdout).strip(), MAX_STDOUT_CHARS)
    stderr = _truncate(_decode(result.stderr).strip(), MAX_STDERR_CHARS)

    lines = [
        f"命令：{' '.join(cmd)}",
        f"退出码(exitcode)：{result.returncode}",
        f"标准输出(stdout)：\n{stdout if stdout else '（空）'}",
        f"标准错误(stderr)：\n{stderr if stderr else '（空）'}",
    ]
    return "\n\n".join(lines)


# 告诉模型的工具说明
tools = [
    {
        "type": "function",
        "function": {
            "name": "searchWeb",
            "description": "联网搜索，获取互联网上的最新信息。返回搜索摘要、参考来源标题及 URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要搜索的关键词或问题",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetchWeb",
            "description": "获取指定网页的主要内容，返回提取后的 Markdown 正文和原始 URL（去除导航、广告、页脚等无关内容）",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要获取内容的网页 URL"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RunCli",
            "description": "调用本地已安装的任意命令行程序（CLI），返回标准输出、标准错误和退出码，用于执行系统命令",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令（程序名称），例如 python、git、ping、ipconfig",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "传给命令的参数列表，例如 [\"--version\"]；没有参数时传空数组 []",
                    },
                },
                "required": ["command", "args"],
            },
        },
    },
]

# 工具名到实际执行函数的映射
tool_functions = {
    "searchWeb": search_web,
    "fetchWeb": fetch_web,
    "RunCli": run_cli,
}

# --- Agent Skills 动态加载 ---
# 初始化阶段：仅发现并注册各 Skill 的 name / description，不加载完整正文。
# Skill 的完整内容只在 LLM 调用 loadSkill 工具时才进入对话上下文。
skills.discover_skills()
SYSTEM_MESSAGE = skills.build_system_message()

# 追加 loadSkill 工具，与已有 searchWeb / fetchWeb / RunCli 协同工作
tools.append(skills.LOAD_SKILL_TOOL)
tool_functions["loadSkill"] = skills.load_skill

def main():
    """命令行交互入口：终端里的多轮对话 REPL。"""
    history = [SYSTEM_MESSAGE]
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
                break

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


if __name__ == "__main__":
    main()
