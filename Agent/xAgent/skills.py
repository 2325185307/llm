"""Agent Skills 动态加载机制。

负责三件事：
1. 以程序启动时的工作目录为项目根目录，扫描 .agents/skills/ 下的各 Skill 子目录，
   解析每个 SKILL.md 的 YAML Frontmatter，初始化阶段只提取 name 与 description；
2. 生成包含可用技能列表的 System Prompt，让 LLM 知道有哪些技能、如何按需加载；
3. 提供 loadSkill 工具实现，按需读取 SKILL.md 全文或技能目录内的附加文件，
   并把读取范围严格限制在指定 Skill 目录内。
"""

import os
import re

# 程序启动时的工作目录作为项目根目录（模块 import 时固化，避免后续 chdir 影响）
PROJECT_ROOT = os.path.abspath(os.getcwd())
SKILLS_DIR = os.path.join(PROJECT_ROOT, ".agents", "skills")

# 已发现的技能列表，每项：{"name", "description", "dir", "skill_md"}
_skills = []


# ─── YAML Frontmatter 解析 ───


def _unquote(value):
    """去掉首尾成对的单/双引号。"""
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return value


def _parse_yaml_simple(text):
    """极简 YAML 标量解析：提取顶层 `key: value` 标量，支持引号与 | / > 块标量。

    仅在缺少 PyYAML 时作为回退，够用即可（本机制只需要 name / description）。
    """
    result = {}
    lines = text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        if not raw or raw[0].isspace() or raw.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:\s*(.*)$", raw)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2)
        if rest in ("|", ">"):
            # 块标量：收集后续缩进行
            block_lines = []
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j][0].isspace()):
                if lines[j].strip():
                    block_lines.append(lines[j].strip())
                j += 1
            sep = "\n" if rest == "|" else " "
            result[key] = sep.join(block_lines)
            i = j
            continue
        result[key] = _unquote(rest.strip())
        i += 1
    return result


def _parse_frontmatter(text):
    """解析 SKILL.md 开头的 YAML Frontmatter，返回 name/description 字典。

    Frontmatter 形如：
        ---
        name: xxx
        description: "yyy"
        ---
    解析失败时返回空字典，由调用方决定如何回退。
    """
    result = {"name": "", "description": ""}
    text = text.lstrip("﻿")
    lines = text.splitlines()

    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != "---":
        return result

    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return result

    block_text = "\n".join(lines[start + 1 : end])

    # 优先用 PyYAML，缺失时回退到极简解析
    data = {}
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            parsed = yaml.safe_load(block_text)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    if not data:
        data = _parse_yaml_simple(block_text)

    if isinstance(data.get("name"), str):
        result["name"] = data["name"].strip()
    if isinstance(data.get("description"), str):
        result["description"] = data["description"].strip()
    return result


# ─── Skill 发现与注册 ───


def discover_skills():
    """扫描 SKILLS_DIR，注册所有 Skill（仅加载 name 与 description）。

    每个 Skill 子目录需包含 SKILL.md；Frontmatter 缺少 name 时回退到目录名。
    """
    global _skills
    _skills = []
    if not os.path.isdir(SKILLS_DIR):
        return _skills

    try:
        entries = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return _skills

    for entry in entries:
        skill_dir = os.path.join(SKILLS_DIR, entry)
        if not os.path.isdir(skill_dir):
            continue
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue

        fm = _parse_frontmatter(text)
        name = fm.get("name") or entry  # 缺失时回退到目录名
        description = fm.get("description") or ""
        _skills.append(
            {
                "name": name,
                "description": description,
                "dir": os.path.abspath(skill_dir),
                "skill_md": skill_md,
            }
        )
    return _skills


def _find_skill(name):
    """按技能名称查找已注册的 Skill（优先 Frontmatter 的 name，其次目录名）。"""
    if not name:
        return None
    name = str(name).strip()
    for s in _skills:
        if s["name"] == name:
            return s
    for s in _skills:
        if os.path.basename(s["dir"]) == name:
            return s
    return None


# ─── 文件路径解析与读取 ───


def _is_within(path, base):
    """判断 path 是否位于 base 目录内（含 base 自身），用于防止路径越界。"""
    try:
        p = os.path.realpath(path)
        b = os.path.realpath(base)
    except OSError:
        return False
    return p == b or p.startswith(b + os.sep)


def _read_text(path, label=None):
    """读取文本文件，UTF-8 优先，失败回退 GBK（中文 Windows 常见）。"""
    content = None
    for enc in ("utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, OSError):
            continue
    if content is None:
        return f"错误：读取文件失败：{path}"
    if label:
        return f"=== {label} ===\n\n{content}"
    return content


def _resolve_skill_file(skill, filename):
    """把 filename 解析为 Skill 目录内的一个文件。

    兼容：相对于 Skill 目录的路径、项目相对路径、绝对路径，优先精确路径匹配；
    只给文件名时递归搜索 Skill 目录，唯一命中才读取，多个命中返回候选路径。

    返回 (status, payload)：
      - ("file", path)         唯一目标文件
      - ("candidates", [...])  多个同名文件，需 LLM 进一步选择
      - ("outside", None)      解析结果越出 Skill 目录
      - ("not_found", None)    未找到
    """
    skill_dir = skill["dir"]
    filename = filename.strip()

    # 1) 绝对路径：必须落在 Skill 目录内
    if os.path.isabs(filename):
        p = os.path.normpath(filename)
        if not _is_within(p, skill_dir):
            return ("outside", None)
        if os.path.isfile(p):
            return ("file", p)
        return ("not_found", None)

    # 2) 精确路径匹配：先相对 Skill 目录，再相对项目根目录
    p_skill = os.path.normpath(os.path.join(skill_dir, filename))
    if not _is_within(p_skill, skill_dir):
        # 相对路径试图越出 Skill 目录（如 ../xxx），直接拒绝
        return ("outside", None)
    if os.path.isfile(p_skill):
        return ("file", p_skill)

    p_proj = os.path.normpath(os.path.join(PROJECT_ROOT, filename))
    if _is_within(p_proj, skill_dir) and os.path.isfile(p_proj):
        return ("file", p_proj)

    # 3) 只给了文件名（或精确路径未命中）→ 递归搜索 Skill 目录
    base_name = os.path.basename(filename)
    matches = []
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # 跳过隐藏目录
        for f in files:
            if f == base_name:
                matches.append(os.path.join(root, f))
    if len(matches) == 1:
        return ("file", matches[0])
    if len(matches) > 1:
        return ("candidates", sorted(matches))
    return ("not_found", None)


# ─── loadSkill 工具实现 ───


def load_skill(name, filename=None):
    """loadSkill 工具：按需读取技能的 SKILL.md 全文或技能目录内的附加文件。

    仅提供 name 时返回 SKILL.md 全文；同时提供 filename 时读取对应文件。
    """
    skill = _find_skill(name)
    if skill is None:
        available = ", ".join(s["name"] for s in _skills) or "（无）"
        return f"错误：未找到名为 {name!r} 的技能。当前可用技能：{available}"

    if filename is None or not str(filename).strip():
        return _read_text(skill["skill_md"], label=f"SKILL.md（技能 {skill['name']}）")

    filename = str(filename).strip()
    status, payload = _resolve_skill_file(skill, filename)

    if status == "file":
        rel = os.path.relpath(payload, skill["dir"])
        return _read_text(payload, label=f"{skill['name']}/{rel}")

    if status == "candidates":
        rels = [os.path.relpath(p, skill["dir"]) for p in payload]
        lines = ["找到多个同名文件，请选择其一，用相对路径重新调用 loadSkill："]
        for r in rels:
            lines.append(f"- {r}")
        return "\n".join(lines)

    if status == "outside":
        return f"错误：文件 {filename!r} 不在技能 {skill['name']!r} 的目录内，已拒绝读取。"

    return f"错误：在技能 {skill['name']!r} 中未找到文件 {filename!r}。"


# ─── System Prompt 与工具定义 ───


def build_system_message(skills=None):
    """生成包含可用技能列表的 System Prompt 消息。"""
    skills = _skills if skills is None else skills
    lines = ["You have access to the following skills:"]
    if skills:
        for s in skills:
            desc = (s.get("description") or "").replace("\n", " ").strip()
            lines.append(f"- '{s['name']}': {desc}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append(
        "Use the 'loadSkill' tool to load the full instructions of a skill when "
        "needed. You can also load additional files referenced by a skill."
    )
    return {"role": "system", "content": "\n".join(lines)}


LOAD_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "loadSkill",
        "description": (
            "加载某个技能(Skill)的完整说明或该技能引用的附加文件。"
            "仅提供 name 时返回该技能的 SKILL.md 全文；同时提供 filename 时读取指定文件。"
            "filename 支持相对技能目录的路径、项目相对路径、绝对路径，"
            "也可只给文件名（会递归搜索该技能目录）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名称（SKILL.md 的 Frontmatter 中声明的 name）",
                },
                "filename": {
                    "type": "string",
                    "description": "可选。要加载的附加文件路径或文件名；缺省时加载 SKILL.md 全文",
                },
            },
            "required": ["name"],
        },
    },
}
