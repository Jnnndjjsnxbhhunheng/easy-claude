"""
Skill 加载器（s05 模式）— Responses API 版本
扫描 skills/ 目录，解析 SKILL.md 的 YAML frontmatter，按需加载完整内容。
额外解析 SKILL.md 中明确提到的工具与附件文件，供 Agent 做渐进式能力扩展。
工具定义使用 Responses API 格式（name 在顶层）。
"""
import os
import re
from pathlib import Path

READABLE_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
EXECUTABLE_SUFFIXES = {".py", ".sh"}
BACKTICK_PATTERN = re.compile(r"`([^`\n]+)`")
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills: dict[str, dict] = {}
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.rglob("SKILL.md")):
                text = skill_file.read_text(encoding="utf-8")
                meta, body = self._parse_skill_file(text)
                name = meta.get("name", skill_file.parent.name)
                self.skills[name] = {
                    "meta": meta,
                    "body": body,
                    "dir": skill_file.parent,
                    "profile": self._build_profile(skill_file.parent, body),
                }

    def _parse_skill_file(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        meta: dict = {}
        body = text
        if match:
            for line in match.group(1).strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = match.group(2).strip()
        return meta, body

    def _build_profile(self, skill_dir: Path, skill_body: str) -> dict:
        skill_dir = skill_dir.resolve()
        referenced_files: list[str] = []
        executable_files: list[str] = []
        other_files: list[str] = []
        mentioned_tools: list[str] = []

        for raw_token in BACKTICK_PATTERN.findall(skill_body):
            token = raw_token.strip()
            resolved = self._resolve_referenced_file(skill_dir, token)
            if resolved is not None:
                relative = resolved.relative_to(skill_dir).as_posix()
                category = self._classify_file(resolved)
                if category == "readable":
                    self._append_unique(referenced_files, relative)
                elif category == "executable":
                    self._append_unique(executable_files, relative)
                else:
                    self._append_unique(other_files, relative)
                continue

            if TOOL_NAME_PATTERN.match(token):
                self._append_unique(mentioned_tools, token)

        return {
            "mentioned_tools": mentioned_tools,
            "referenced_files": referenced_files,
            "executable_files": executable_files,
            "resource_hints": {
                "other_files": other_files,
                "has_readable_files": bool(referenced_files),
                "has_executable_files": bool(executable_files),
                "has_other_files": bool(other_files),
            },
        }

    def _resolve_referenced_file(self, skill_dir: Path, token: str) -> Path | None:
        skill_dir = skill_dir.resolve()
        if not self._looks_like_relative_path(token):
            return None
        try:
            resolved = (skill_dir / token).resolve()
        except OSError:
            return None
        if not resolved.exists() or not resolved.is_file():
            return None
        try:
            resolved.relative_to(skill_dir)
        except ValueError:
            return None
        return resolved

    def _looks_like_relative_path(self, token: str) -> bool:
        if not token or token.startswith(("http://", "https://", "/", "~")):
            return False
        if any(ch in token for ch in ("\n", "\r")):
            return False
        path = Path(token)
        if token.startswith(".") or "/" in token or "\\" in token:
            return True
        return bool(path.suffix)

    def _classify_file(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in READABLE_SUFFIXES:
            return "readable"
        if suffix in EXECUTABLE_SUFFIXES or os.access(path, os.X_OK):
            return "executable"
        return "other"

    def _append_unique(self, items: list[str], value: str):
        if value not in items:
            items.append(value)

    def descriptions(self) -> str:
        """返回所有技能的简短描述，用于注入系统提示词。"""
        if not self.skills:
            return "(暂无技能)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "无描述")
            lines.append(f"  - {name}: {desc}")
        return "\n".join(lines)

    def load(self, name: str) -> str:
        """按名称加载技能的完整内容。"""
        skill = self.skills.get(name)
        if not skill:
            available = ", ".join(self.skills.keys())
            return f"错误：未知技能 '{name}'。可用技能：{available}"
        return f'<skill name="{name}">\n{skill["body"]}\n</skill>'

    def get_profile(self, name: str) -> dict:
        skill = self.skills.get(name)
        if not skill:
            return {
                "mentioned_tools": [],
                "referenced_files": [],
                "executable_files": [],
                "resource_hints": {
                    "other_files": [],
                    "has_readable_files": False,
                    "has_executable_files": False,
                    "has_other_files": False,
                },
            }
        return skill["profile"]

    def get_dir(self, name: str) -> Path | None:
        skill = self.skills.get(name)
        if not skill:
            return None
        return Path(skill["dir"]).resolve()

    def list_names(self) -> list[str]:
        return list(self.skills.keys())

    def as_openai_tool(self) -> dict:
        """将 load_skill 暴露为 Responses API 工具（name 在顶层）。"""
        skill_names = self.list_names()
        return {
            "type": "function",
            "name": "load_skill",
            "description": "按名称加载专项技能的完整内容，注入到对话上下文中",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": skill_names if skill_names else ["(无技能)"],
                        "description": "要加载的技能名称",
                    }
                },
                "required": ["name"],
            },
        }
