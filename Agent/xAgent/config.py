"""配置管理：从 config.ini 读取配置，缺失时用默认值；文件不存在时自动生成带 mock 值的模板。

config.ini 放在脚本同目录（BASE_DIR），与密钥一起被 .gitignore 忽略。
"""

import configparser
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")

# 各配置项的默认值（config.ini 中缺失时使用）
_DEFAULTS = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-REPLACE-ME",
        "model": "deepseek-chat",
    },
    "search": {
        "baidu_qianfan_api_key": "bce-v3/REPLACE-ME",
    },
    "agent": {
        "context_window": "131072",  # 128k
        "compact_threshold": "0.70",
        "compact_max_tokens": "8192",
        "max_markdown_chars": "12000",
        "cli_timeout": "30",
        "max_stdout_chars": "4000",
        "max_stderr_chars": "2000",
    },
    "mcp": {
        "config_file": ".mcp.json",
    },
}

# 首次生成 config.ini 时写入的模板（密钥为占位符，需用户替换后重启）
_TEMPLATE = """\
; xAgent 配置文件（自动生成的模板，请填入真实信息后重新启动）
;
; 说明：
;   - api_key / baidu_qianfan_api_key 目前是占位符（mock），必须替换为真实密钥。
;   - 修改本文件后，需要重新启动 xAgent 才会生效。

[llm]
base_url = https://api.deepseek.com/v1
api_key = sk-REPLACE-ME
model = deepseek-chat

[search]
baidu_qianfan_api_key = bce-v3/REPLACE-ME

[agent]
context_window = 131072
compact_threshold = 0.70
compact_max_tokens = 8192
max_markdown_chars = 12000
cli_timeout = 30
max_stdout_chars = 4000
max_stderr_chars = 2000

[mcp]
config_file = .mcp.json
"""


class Config:
    """config.ini 的包装器，按节/键读取，带类型转换与默认值。"""

    def __init__(self, parser: configparser.ConfigParser):
        self._p = parser

    def get(self, section: str, key: str, default: str = "") -> str:
        try:
            return self._p.get(section, key)
        except (configparser.Error, KeyError):
            return default

    def getint(self, section: str, key: str, default: int = 0) -> int:
        value = self.get(section, key, "")
        try:
            return int(value)
        except ValueError:
            return default

    def getfloat(self, section: str, key: str, default: float = 0.0) -> float:
        value = self.get(section, key, "")
        try:
            return float(value)
        except ValueError:
            return default

    # ── 常用配置项（对外只读属性）──
    @property
    def llm_base_url(self) -> str:
        return self.get("llm", "base_url", _DEFAULTS["llm"]["base_url"])

    @property
    def llm_api_key(self) -> str:
        return self.get("llm", "api_key", _DEFAULTS["llm"]["api_key"])

    @property
    def llm_model(self) -> str:
        return self.get("llm", "model", _DEFAULTS["llm"]["model"])

    @property
    def search_api_key(self) -> str:
        return self.get("search", "baidu_qianfan_api_key", "")

    @property
    def context_window(self) -> int:
        return self.getint("agent", "context_window", 128 * 1024)

    @property
    def compact_threshold(self) -> float:
        return self.getfloat("agent", "compact_threshold", 0.70)

    @property
    def compact_max_tokens(self) -> int:
        return self.getint("agent", "compact_max_tokens", 8192)

    @property
    def max_markdown_chars(self) -> int:
        return self.getint("agent", "max_markdown_chars", 12000)

    @property
    def cli_timeout(self) -> int:
        return self.getint("agent", "cli_timeout", 30)

    @property
    def max_stdout_chars(self) -> int:
        return self.getint("agent", "max_stdout_chars", 4000)

    @property
    def max_stderr_chars(self) -> int:
        return self.getint("agent", "max_stderr_chars", 2000)

    @property
    def mcp_config_file(self) -> str:
        return self.get("mcp", "config_file", ".mcp.json")


def _ensure_config_file() -> bool:
    """config.ini 不存在时生成模板，返回是否为新生成。"""
    if os.path.isfile(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(_TEMPLATE)
    except OSError:
        pass
    return True


_created = False


def was_created() -> bool:
    """本次启动是否刚自动生成了 config.ini（用于在 TUI 里弹提示）。"""
    return _created


def notice_text() -> str:
    """首次生成 config.ini 时弹出的提示文本。"""
    return (
        "检测到 config.ini 不存在，已自动生成模板文件：\n"
        f"  {CONFIG_PATH}\n"
        "其中 API 密钥为占位符（mock），请填入真实的 DeepSeek / 百度千帆密钥后，\n"
        "重新启动 xAgent 才能正常调用模型与联网搜索。"
    )


def load() -> Config:
    """加载配置；若 config.ini 缺失则生成模板并提示。"""
    global _created
    _created = _ensure_config_file()
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    cfg = Config(parser)
    if _created:
        print(notice_text(), file=sys.stderr)
    return cfg
