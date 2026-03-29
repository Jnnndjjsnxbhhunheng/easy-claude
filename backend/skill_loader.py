"""
Skill 加载器（s05 模式）
扫描 skills/ 目录，解析 SKILL.md 的 YAML frontmatter，按需加载完整内容。
"""
import re
from pathlib import Path


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills: dict[str, dict] = {}
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.rglob("SKILL.md")):
                text = skill_file.read_text(encoding="utf-8")
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta: dict = {}
                body = text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", skill_file.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

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

    def list_names(self) -> list[str]:
        return list(self.skills.keys())

    def as_openai_tool(self) -> dict:
        """将 load_skill 暴露为 OpenAI function calling 工具。"""
        skill_names = self.list_names()
        return {
            "type": "function",
            "function": {
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
            },
        }
