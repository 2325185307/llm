import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import trafilatura
from openai import OpenAI

import config
import context
import mcp_client
import sessions
import skills

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 配置：从 config.ini 读取（缺失时自动生成 mock 模板并提示）
cfg = config.load()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key)

# 供 ui.py 复用的配置段（兼容旧接口 section["model"]）
section = {"model": cfg.llm_model}

# 百度千帆智能搜索（原始搜索结果）接口
SEARCH_API_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"

# 以下参数均来自 config.ini（带默认值），避免硬编码
MAX_MARKDOWN_CHARS = cfg.max_markdown_chars
CLI_TIMEOUT = cfg.cli_timeout
MAX_STDOUT_CHARS = cfg.max_stdout_chars
MAX_STDERR_CHARS = cfg.max_stderr_chars
COMPACT_MAX_TOKENS = cfg.compact_max_tokens


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
    """从 config.ini 的 [search] 小节读取百度千帆 API Key。"""
    key = cfg.search_api_key.strip()
    if not key or "REPLACE-ME" in key:
        return None, "config.ini 中 baidu_qianfan_api_key 为空或仍是占位符，请配置真实密钥"
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
skills.discover_skills()

# 心理咨询师人设（系统提示）：温暖共情 + 安全边界 + 技能清单
COUNSELOR_PERSONA = (
    "你是「安心」，一位温暖、专业、富有共情心的心理咨询师，主要采用认知行为治疗（CBT）取向。\n"
    "你的职责是陪伴来访者，提供情绪疏导、心理支持与结构化的自助引导，而非医学诊断或治疗。\n"
    "\n"
    "【基本原则】\n"
    "1. 温暖、共情、非评判；协作式引导，让来访者自己发现答案，避免说教和直接给建议。\n"
    "2. 尊重来访者自主性，保持专业边界：只做心理支持与 CBT 自助引导，不做医学诊断、不开药。\n"
    "3. 先认可并正常化来访者的感受，再逐步引入结构化技巧（如认知重构、苏格拉底提问、行为实验）。\n"
    "4. 善于倾听与追问，一次聚焦一个议题，避免信息轰炸。\n"
    "\n"
    "【安全第一】\n"
    "一旦来访者表达自杀、自伤或伤害他人的念头或意图，立即停止常规流程，表达关切并转介：\n"
    "- 全国统一心理援助热线：400-161-9995\n"
    "- 北京心理危机研究与干预中心：010-82951332\n"
    "并建议前往就近医院急诊或联系信任的家人朋友。来访者安全永远优先于一切。\n"
    "\n"
    "【工作方式】\n"
    "下方列出了你拥有的心理咨询技能。当来访者的话题匹配某个技能时，\n"
    "先用 loadSkill 工具加载该技能以获取完整操作指引，再按其框架展开对话。"
)


def _build_system_message():
    """组合：心理咨询师人设 + 可用技能清单。"""
    skills_part = skills.build_system_message()["content"]
    return {"role": "system", "content": COUNSELOR_PERSONA + "\n\n" + skills_part}


SYSTEM_MESSAGE = _build_system_message()

# 追加 loadSkill / createSkill / recallContext 工具
tools.append(skills.LOAD_SKILL_TOOL)
tool_functions["loadSkill"] = skills.load_skill
tools.append(skills.CREATE_SKILL_TOOL)
tool_functions["createSkill"] = skills.create_skill
tools.append(context.RECALL_CONTEXT_TOOL)
tool_functions["recallContext"] = context.recall_context


# --- MCP 工具异步注册（先进入 TUI，加载完成后动态刷新边栏）---
_mcp_merged = False


def _reserved_names():
    return {t["function"]["name"] for t in tools}


# 后台线程中连接 MCP Server，立即返回；结果通过 sync_mcp_tools 合并
mcp_client.start_async_discovery(
    reserved_names=_reserved_names(), config_file=cfg.mcp_config_file
)


def sync_mcp_tools() -> bool:
    """MCP 发现完成后，把发现的工具合并进 tools / tool_functions。返回是否已合并。"""
    global _mcp_merged
    if _mcp_merged:
        return True
    done, defs, fns, _msgs = mcp_client.get_discovery_result()
    if not done:
        return False
    tools.extend(defs)
    tool_functions.update(fns)
    _mcp_merged = True
    return True


def mcp_discovery_done() -> bool:
    return mcp_client.get_discovery_result()[0]


def mcp_sidebar_text() -> str:
    """生成 TUI 边栏中 MCP 区域的文本。"""
    summary = mcp_client.discovery_summary()
    if not summary["done"]:
        return "MCP 加载中…"
    lines = []
    if summary["servers"]:
        for sv in summary["servers"]:
            lines.append(f"● {sv['name']} [{sv['transport']}]（{len(sv['tools'])} 个工具）")
            for t in sv["tools"]:
                lines.append(f"    · {t}")
    else:
        lines.append("（未连接任何 MCP Server）")
    for m in summary["messages"]:
        if m.startswith("已连接 MCP Server"):
            continue  # 成功信息已在上方服务器列表中展示
        lines.append(f"⚠ {m}")
    return "\n".join(lines)


# --- 会话核心逻辑 ---


def new_history() -> list:
    """新建会话：System Prompt + 可选注入 AGENTS.md（一次注入，全程复用）。"""
    return sessions.inject_agents_md([SYSTEM_MESSAGE])


def run_turn(history, user_text, progress=None):
    """执行一轮对话：先做工具调用循环，再给出最终回答。

    原地更新 history，返回 (回复文本, 最近一次 API 报告的输入 token 数)。
    progress 为可选回调，接收字符串，用于 TUI 实时显示工具调用进度。
    """
    history.append({"role": "user", "content": user_text})
    api_prompt_tokens = None

    while True:
        response = client.chat.completions.create(
            model=cfg.llm_model,
            messages=history,
            tools=tools,
            tool_choice="auto",
        )
        if getattr(response, "usage", None):
            api_prompt_tokens = getattr(response.usage, "prompt_tokens", None)
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

        for tc in message.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            if progress:
                progress(f"🔧 调用工具 {name}，参数：{json.dumps(args, ensure_ascii=False)}")
            try:
                result = tool_functions[name](**args)
            except Exception as exc:
                result = f"错误：工具 {name} 执行失败：{exc}"
            history.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if progress:
                progress(f"📋 {name} 返回：{result[:200]}")

    reply = (message.content or "").strip()
    if not reply:
        # 兜底：极少数情况下最终消息 content 为空，再请求一次
        resp2 = client.chat.completions.create(model=cfg.llm_model, messages=history)
        reply = (resp2.choices[0].message.content or "").strip()
    history.append({"role": "assistant", "content": reply})
    return reply, api_prompt_tokens


def compact(history, progress=None):
    """执行 /compact：先归档原始上下文，再调用 LLM 生成摘要并替换上下文。

    返回 (新 history, 是否成功, 提示消息)。失败时保持原 history 不变。
    """
    try:
        archive_id, archive_path = context.save_archive(history)
    except OSError as exc:
        return history, False, f"✗ 压缩失败：原始上下文归档保存失败：{exc}"

    if progress:
        progress(f"正在压缩上下文（归档编号 {archive_id}，共 {len(history)} 条消息）…")

    try:
        messages = context.build_compact_messages(history)
        resp = client.chat.completions.create(
            model=cfg.llm_model,
            messages=messages,
            max_tokens=COMPACT_MAX_TOKENS,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return history, False, f"✗ 压缩失败：{exc}\n原上下文已保留，归档在 {archive_path}"

    if not summary:
        return history, False, f"✗ 压缩失败：模型未返回摘要\n原上下文已保留，归档在 {archive_path}"

    summary = summary.rstrip() + "\n\n" + context.make_archive_note(archive_id)
    new_history = [SYSTEM_MESSAGE, {"role": "system", "content": summary}]
    new_tokens = context.count_history_tokens(new_history)
    return (
        new_history,
        True,
        f"✓ 压缩成功：归档编号 {archive_id}，新上下文约 {new_tokens} tokens（{context.format_k(new_tokens)}）",
    )


# --- 上下文用量与状态 ---


def context_window_info():
    """返回 (上下文窗口 token 数, 是否使用默认值)。"""
    window, is_default = context.get_context_window(cfg.llm_model)
    # config.ini 的 context_window 作为显式覆盖
    if cfg.context_window > 0 and cfg.context_window != window:
        window = cfg.context_window
        is_default = False
    return window, is_default


def current_tokens(history, api_prompt_tokens=None, last_reply=""):
    """估算/统计当前上下文 token 数：优先 API 报告，否则本地估算。"""
    if api_prompt_tokens is not None:
        return api_prompt_tokens + context.estimate_tokens(last_reply)
    return context.count_history_tokens(history)


def status_line(tokens):
    """生成上下文占用状态行，返回 (状态文本, 占用百分比)。"""
    window, is_default = context_window_info()
    pct = tokens / window * 100
    win_label = context.format_k(window) + ("（默认）" if is_default else "")
    line = f"[上下文] {context.format_k(tokens)} / {win_label}  ({pct:.1f}%)"
    if pct >= cfg.compact_threshold * 100:
        line += f"  ⚠️ 已达 {int(cfg.compact_threshold * 100)}% 阈值，请执行 /compact"
    return line, pct


def at_threshold(tokens):
    """上下文占用是否达到压缩阈值。"""
    window, _ = context_window_info()
    return tokens / window >= cfg.compact_threshold


def main():
    """命令行入口：启动全屏 TUI。"""
    from tui import run_tui

    run_tui()


if __name__ == "__main__":
    main()
