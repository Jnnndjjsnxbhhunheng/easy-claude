"""
主 Agent 循环 — Responses API 版本
使用 OpenAI Responses API (wire_api="responses") + MCP + Skills + 团队协议
通过异步事件回调将中间步骤实时推送给前端（SSE）
"""
import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from openai import AsyncOpenAI

from mcp_manager import MCPManager, load_mcp_configs
from skill_loader import SkillLoader
from task_manager import TaskManager
from team_manager import MessageBus, TeamManager

# ===== 路径配置 =====
WORKDIR = Path(__file__).parent.parent
TASKS_DIR = WORKDIR / ".tasks"
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
_skills_dir = Path(os.environ.get("SKILLS_DIR", str(WORKDIR / "skills")))
SKILLS_DIR = _skills_dir if _skills_dir.is_absolute() else (WORKDIR / _skills_dir).resolve()
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOKEN_THRESHOLD = 80000
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
DEFAULT_MODEL_TIMEOUT_SECONDS = 20
LONG_WORKFLOW_MODEL_TIMEOUT_SECONDS = 300
TOOL_OUTPUT_CONTEXT_LIMITS = {
    "load_skill": 18000,
    "read_file": 12000,
    "default": 12000,
}


def is_retryable_api_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return True
    text = str(exc).lower()
    retry_markers = (
        "503",
        "service temporarily unavailable",
        "timed out",
        "timeout",
        "connection reset",
        "connection error",
        "didn't receive a `response.completed` event",
    )
    return any(marker in text for marker in retry_markers)


def is_missing_response_completed_error(exc: Exception) -> bool:
    return "didn't receive a `response.completed` event" in str(exc).lower()


def format_api_error(exc: Exception, timeout_seconds: int) -> str:
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return f"模型调用超时（{timeout_seconds}s）"

    text = str(exc).strip()
    if text:
        return f"API 调用失败: {text}"

    exc_name = exc.__class__.__name__ or "UnknownError"
    return f"API 调用失败: {exc_name}"


def extract_output_text(response) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        output = response.get("output", [])
        if isinstance(output, str):
            return output
        parts: list[str] = []
        for item in output:
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                text = content.get("text")
                if text:
                    parts.append(text)
        if parts:
            return "".join(parts)
        return response.get("output_text", "") or response.get("text", "") or json.dumps(
            response, ensure_ascii=False
        )
    parts: list[str] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []):
            text = getattr(content, "text", None)
            if text:
                parts.append(text)
    return "".join(parts)


# ===== 工具函数 =====
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误：危险命令已被拦截"
    try:
        r = subprocess.run(  # noqa: S602
            command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：命令超时（120s）"
    except Exception as e:
        return f"错误: {e}"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} 行省略)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as e:
        return f"错误: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        c = fp.read_text(encoding="utf-8")
        if old_text not in c:
            return f"错误：在 {path} 中未找到目标文本"
        fp.write_text(c.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误: {e}"


def estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str)) // 4


def truncate_tool_output_for_context(fn_name: str, output: str) -> str:
    limit = TOOL_OUTPUT_CONTEXT_LIMITS.get(fn_name, TOOL_OUTPUT_CONTEXT_LIMITS["default"])
    if len(output) <= limit:
        return output
    return f"{output[:limit]}\n... [已截断，原始输出过长]"


def _to_responses_tool(chat_tool: dict) -> dict:
    """将 Chat Completions 工具格式转换为 Responses API 工具格式。

    Chat Completions: {"type":"function","function":{"name":...,"description":...,"parameters":...}}
    Responses API:   {"type":"function","name":...,"description":...,"parameters":...}
    """
    if chat_tool.get("type") == "function":
        fn = chat_tool.get("function", {})
        return {
            "type": "function",
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        }
    return chat_tool


# ===== Todo 管理器（s03）=====
class TodoManager:
    def __init__(self):
        self.items: list[dict] = []

    def update(self, items: list) -> str:
        validated, ip = [], 0
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            if not content:
                raise ValueError(f"条目 {i}: content 不能为空")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"条目 {i}: 无效状态 '{status}'")
            if not af:
                raise ValueError(f"条目 {i}: activeForm 不能为空")
            if status == "in_progress":
                ip += 1
            validated.append({"content": content, "status": status, "activeForm": af})
        if len(validated) > 20:
            raise ValueError("最多 20 个 Todo 条目")
        if ip > 1:
            raise ValueError("只允许一个 in_progress 条目")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "暂无 Todo"
        lines = []
        for item in self.items:
            mark = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(
                item["status"], "[?]"
            )
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{mark} {item['content']}{suffix}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} 已完成)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        return any(item.get("status") != "completed" for item in self.items)


# ===== 后台任务管理器（s08）=====
class BackgroundManager:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self._notif_queue: list[dict] = []
        self._lock = threading.Lock()

    def run(self, command: str, timeout: int = 120) -> str:
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(
            target=self._exec, args=(tid, command, timeout), daemon=True
        ).start()
        return f"后台任务 {tid} 已启动: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            r = subprocess.run(  # noqa: S602
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=timeout
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": output or "(无输出)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        with self._lock:
            self._notif_queue.append({
                "task_id": tid,
                "status": self.tasks[tid]["status"],
                "result": self.tasks[tid]["result"][:500],
            })

    def check(self, tid: str | None = None) -> str:
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result', '(运行中)')}" if t else f"未知任务: {tid}"
        return "\n".join(
            f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()
        ) or "无后台任务"

    def drain(self) -> list[dict]:
        with self._lock:
            notifs, self._notif_queue = self._notif_queue[:], []
        return notifs


# ===== 工具定义（Responses API 格式）=====
def _make_builtin_tools() -> list[dict]:
    """所有内置工具的 Responses API 格式定义。"""
    return [
        {
            "type": "function",
            "name": "bash",
            "description": "执行 shell 命令",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell 命令"}},
                "required": ["command"],
            },
        },
        {
            "type": "function",
            "name": "read_file",
            "description": "读取文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "description": "最大行数"},
                },
                "required": ["path"],
            },
        },
        {
            "type": "function",
            "name": "write_file",
            "description": "写入内容到文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "type": "function",
            "name": "edit_file",
            "description": "替换文件中的精确文本片段",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
        {
            "type": "function",
            "name": "TodoWrite",
            "description": "更新任务清单（最多20条，只允许1条 in_progress）",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                                "activeForm": {"type": "string"},
                            },
                            "required": ["content", "status", "activeForm"],
                        },
                    }
                },
                "required": ["items"],
            },
        },
        {
            "type": "function",
            "name": "task_create",
            "description": "创建一个持久化任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["subject"],
            },
        },
        {
            "type": "function",
            "name": "task_get",
            "description": "获取任务详情",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        },
        {
            "type": "function",
            "name": "task_update",
            "description": "更新任务状态或依赖关系",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
                    "add_blocked_by": {"type": "array", "items": {"type": "integer"}},
                    "add_blocks": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["task_id"],
            },
        },
        {
            "type": "function",
            "name": "task_list",
            "description": "列出所有任务",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "background_run",
            "description": "在后台线程执行命令",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
        {
            "type": "function",
            "name": "check_background",
            "description": "检查后台任务状态",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
            },
        },
        {
            "type": "function",
            "name": "spawn_teammate",
            "description": "生成一个自主队友",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["name", "role", "prompt"],
            },
        },
        {
            "type": "function",
            "name": "list_teammates",
            "description": "列出所有队友及其状态",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "send_message",
            "description": "发送消息给队友",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "content": {"type": "string"},
                    "msg_type": {
                        "type": "string",
                        "enum": ["message", "broadcast", "shutdown_request", "shutdown_response",
                                 "plan_approval_request", "plan_approval_response"],
                    },
                },
                "required": ["to", "content"],
            },
        },
        {
            "type": "function",
            "name": "read_inbox",
            "description": "读取并清空 lead 的收件箱",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "broadcast",
            "description": "向所有队友广播消息",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        },
        {
            "type": "function",
            "name": "shutdown_request",
            "description": "优雅地请求队友关闭（s10 协议）",
            "parameters": {
                "type": "object",
                "properties": {"teammate": {"type": "string"}},
                "required": ["teammate"],
            },
        },
        {
            "type": "function",
            "name": "plan_approval",
            "description": "批准或拒绝队友提交的计划（s10 协议）",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "approve": {"type": "boolean"},
                    "feedback": {"type": "string"},
                },
                "required": ["request_id", "approve"],
            },
        },
        {
            "type": "function",
            "name": "compress",
            "description": "手动压缩对话上下文",
            "parameters": {"type": "object", "properties": {}},
        },
    ]


# ===== Agent Runner =====
class AgentRunner:
    """
    主 Agent 类，持有所有子系统，提供 run() 异步方法。
    run() 通过 emit() 回调发送 SSE 事件。
    使用 OpenAI Responses API (wire_api="responses")。
    """

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL"),  # None = 使用默认 OpenAI
        )
        self.todo = TodoManager()
        self.skills = SkillLoader(SKILLS_DIR)
        self.task_mgr = TaskManager(TASKS_DIR)
        self.bg = BackgroundManager()
        self.bus = MessageBus(INBOX_DIR)
        self.team = TeamManager(
            self.bus,
            self.task_mgr,
            TEAM_DIR,
            event_callback=None,
        )
        self.mcp: MCPManager | None = None
        self._mcp_initialized = False

    async def _ensure_mcp(self):
        if not self._mcp_initialized:
            configs = load_mcp_configs()
            self.mcp = MCPManager(configs)
            await self.mcp.initialize()
            self._mcp_initialized = True

    def _build_tools(self) -> list[dict]:
        """构建完整工具列表（Responses API 格式）。"""
        tools = _make_builtin_tools()

        # Skill 工具
        skill_names = self.skills.list_names()
        if skill_names:
            tools.append({
                "type": "function",
                "name": "load_skill",
                "description": "按名称加载专项技能的完整内容，注入到对话上下文中",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": skill_names,
                            "description": "要加载的技能名称",
                        }
                    },
                    "required": ["name"],
                },
            })

        # MCP 工具（已是 Responses API 格式）
        if self.mcp:
            tools.extend(self.mcp.tools)

        return tools

    def _base_tool_names(self, user_message: str) -> list[str]:
        """按用户当前意图保留最小基础工具集，不在这里做任何 skill 专属拍板。"""
        by_name = {tool["name"]: tool for tool in self._build_tools()}
        selected: list[str] = []
        lowered = user_message.lower()

        def add(*names: str):
            for name in names:
                if name in by_name and name not in selected:
                    selected.append(name)

        add("load_skill")

        if any(token in user_message for token in ("计算", "当前时间", "时间")):
            add("calculate", "get_current_time")
        if "search_files" in lowered or any(token in user_message for token in ("搜索文件", "查找文件", "glob")):
            add("search_files")

        if "任务" in user_message or "task" in lowered:
            add("task_create", "task_get", "task_update", "task_list")
        if "todo" in lowered or "待办" in user_message:
            add("TodoWrite")

        if any(token in user_message for token in ("队友", "团队")) or any(
            token in lowered for token in ("teammate", "spawn_teammate", "broadcast")
        ):
            add(
                "spawn_teammate",
                "list_teammates",
                "send_message",
                "read_inbox",
                "broadcast",
                "shutdown_request",
                "plan_approval",
            )

        if any(token in user_message for token in ("命令", "shell", "工作目录")) or "bash" in lowered:
            add("bash")
        if any(token in user_message for token in ("读取文件", "查看文件")) or "read_file" in lowered:
            add("read_file")
        if any(token in user_message for token in ("写入文件", "编辑文件", "修改文件")) or any(
            token in lowered for token in ("write_file", "edit_file")
        ):
            add("read_file", "write_file", "edit_file")
        if "后台" in user_message or "background" in lowered:
            add("background_run", "check_background")
        if "压缩" in user_message or "compress" in lowered:
            add("compress")

        # 允许用户直接按工具名点名调用，但不为任意 skill 做关键词特判。
        for name in by_name:
            if name == "load_skill":
                continue
            if name.lower() in lowered:
                add(name)

        return selected

    def _select_tools(self, user_message: str, loaded_skills: set[str] | None = None) -> list[dict]:
        """基础工具裁剪 + 已加载 skill 驱动的增量能力开放。"""
        all_tools = self._build_tools()
        by_name = {tool["name"]: tool for tool in all_tools}
        selected: list[str] = []
        loaded_skills = loaded_skills or set()

        def add(*names: str):
            for name in names:
                if name in by_name and name not in selected:
                    selected.append(name)

        add(*self._base_tool_names(user_message))

        for skill_name in loaded_skills:
            profile = self.skills.get_profile(skill_name)
            for tool_name in profile.get("mentioned_tools", []):
                add(tool_name)
            if profile.get("referenced_files"):
                add("read_file")
            if profile.get("executable_files"):
                add("bash")

        return [by_name[name] for name in selected]

    def _build_system_prompt(self) -> str:
        return f"""你是一个全功能 AI Agent，工作目录：{WORKDIR}。

使用工具完成任务。优先使用 task_create/task_list 管理多步骤工作，用 TodoWrite 管理短清单。
先根据用户请求和下方 skill 描述，自主判断是否需要调用 load_skill 加载专项技能。
`load_skill` 只会提供该 skill 的 `SKILL.md` 入口说明，不会自动展开整个 skill 目录。
若 `SKILL.md` 中引用了相对路径文件，必须按该 skill 自身目录解析这些相对路径，而不是按仓库根目录解析。
读取或执行 skill 附件时，优先直接使用 `SKILL.md` 里写出的相对路径；系统会把这些相对路径解析到已加载 skill 的真实目录。
如果 `SKILL.md` 明确引用了其他附件文件，只读取当前步骤真正需要的那些文件。
如果 `SKILL.md` 明确引用了脚本或可执行文件，只在当前步骤需要时再运行。
一旦某个 skill 已加载，就必须遵守它在 `SKILL.md` 中写明的 Required / Optional、必读 / 必跑、workflow step 约束；不要跳过被标为必需的读取或执行步骤。
不要因为 skill 目录里还有其他文件就批量读取或执行。

可用技能：
{self.skills.descriptions()}

MCP 工具可直接调用（来自外部 MCP 服务器）。
生成队友时使用 spawn_teammate，队友执行风险操作前需提交计划审批。"""

    def _model_timeout_seconds(self, loaded_skills: set[str]) -> int:
        return LONG_WORKFLOW_MODEL_TIMEOUT_SECONDS if loaded_skills else DEFAULT_MODEL_TIMEOUT_SECONDS

    def _loaded_skill_attachment_specs(self, loaded_skills: set[str]) -> list[tuple[str, Path]]:
        specs: list[tuple[str, Path]] = []
        for skill_name in loaded_skills:
            skill_dir = self.skills.get_dir(skill_name)
            if skill_dir is None:
                continue
            profile = self.skills.get_profile(skill_name)
            rel_paths = list(profile.get("referenced_files", []))
            rel_paths.extend(profile.get("executable_files", []))
            rel_paths.extend(profile.get("resource_hints", {}).get("other_files", []))
            for rel_path in rel_paths:
                actual = (skill_dir / rel_path).resolve()
                if actual.exists() and actual.is_file():
                    specs.append((rel_path, actual))
        return specs

    def _resolve_loaded_skill_path(self, requested_path: str, loaded_skills: set[str]) -> Path | None:
        if not loaded_skills:
            return None

        normalized_requested = requested_path.replace("\\", "/")
        basename = Path(normalized_requested).name
        basename_matches: list[Path] = []

        for skill_name, actual in self._iter_skill_attachment_aliases(loaded_skills):
            alias = skill_name.replace("\\", "/")
            if normalized_requested == alias or normalized_requested.endswith(f"/{alias}"):
                return Path(actual)
            if Path(alias).name == basename:
                basename_matches.append(Path(actual))

        unique_matches = []
        for match in basename_matches:
            if match not in unique_matches:
                unique_matches.append(match)
        if len(unique_matches) == 1:
            return unique_matches[0]
        return None

    def _iter_skill_attachment_aliases(self, loaded_skills: set[str]) -> list[tuple[str, str]]:
        aliases: list[tuple[str, str]] = []
        basename_counts: dict[str, int] = {}
        specs = self._loaded_skill_attachment_specs(loaded_skills)
        for rel_path, actual in specs:
            basename_counts[Path(rel_path).name] = basename_counts.get(Path(rel_path).name, 0) + 1

        for skill_name in loaded_skills:
            skill_dir = self.skills.get_dir(skill_name)
            if skill_dir is None:
                continue
            for rel_path, actual in specs:
                if not str(actual).startswith(str(skill_dir)):
                    continue
                actual_str = actual.as_posix()
                alias_values = [
                    rel_path,
                    f"{skill_name}/{rel_path}",
                    f"skills/{skill_name}/{rel_path}",
                    (WORKDIR / skill_name / rel_path).as_posix(),
                    actual_str,
                ]
                if basename_counts.get(Path(rel_path).name) == 1:
                    alias_values.append(Path(rel_path).name)
                for alias in alias_values:
                    aliases.append((alias, actual_str))

        aliases.sort(key=lambda item: len(item[0]), reverse=True)
        return aliases

    def _run_read_for_loaded_skills(
        self,
        path: str,
        limit: int | None,
        loaded_skills: set[str],
    ) -> str:
        output = run_read(path, limit)
        if not str(output).startswith("错误"):
            return output
        resolved = self._resolve_loaded_skill_path(path, loaded_skills)
        if resolved is None:
            return output
        return run_read(resolved.as_posix(), limit)

    def _rewrite_bash_command_for_loaded_skills(
        self,
        command: str,
        loaded_skills: set[str],
    ) -> str:
        rewritten = command
        for alias, actual in self._iter_skill_attachment_aliases(loaded_skills):
            if not alias or alias == actual:
                continue
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_./-]){re.escape(alias)}(?![A-Za-z0-9_./-])"
            )
            rewritten = pattern.sub(actual, rewritten)
        return rewritten

    def _build_loaded_skill_followup(self, skill_name: str) -> str:
        skill_dir = self.skills.get_dir(skill_name)
        profile = self.skills.get_profile(skill_name)
        if skill_dir is None:
            return ""

        lines = [
            f"<loaded-skill name=\"{skill_name}\">",
            f"skill_root={skill_dir.as_posix()}",
        ]
        referenced_files = profile.get("referenced_files", [])
        executable_files = profile.get("executable_files", [])
        mentioned_tools = profile.get("mentioned_tools", [])

        if referenced_files:
            lines.append("referenced_files=" + ", ".join(referenced_files))
        if executable_files:
            lines.append("executable_files=" + ", ".join(executable_files))
        if mentioned_tools:
            lines.append("mentioned_tools=" + ", ".join(mentioned_tools))

        lines.append("规则：后续读取或执行这些附件时，直接使用上面的相对路径即可，系统会按 skill_root 解析。")
        lines.append("规则：若 SKILL.md 将某些读取、脚本或工具步骤标为 Required/必读/必跑/mandatory，完成这些步骤后才能结束。")
        lines.append("</loaded-skill>")
        return "\n".join(lines)

    def _convert_history_to_input(self, history: list[dict]) -> list[dict]:
        """
        将前端发来的简单历史（{role, content} 字符串对）转换为 Responses API input 格式。
        用户消息：直接使用。
        助手消息：包装为 output_text 格式（Responses API 要求）。
        """
        input_items = []
        for msg in history:
            if msg["role"] == "user":
                input_items.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant" and msg.get("content"):
                # Responses API 助手消息格式
                input_items.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": msg["content"]}],
                })
        return input_items

    async def _compress(self, input_items: list) -> list:
        """压缩对话上下文（s06），返回压缩后的 input_items。"""
        TRANSCRIPT_DIR.mkdir(exist_ok=True)
        path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in input_items),
            encoding="utf-8",
        )
        conv_text = json.dumps(input_items, ensure_ascii=False, default=str)[:80000]
        response = await self.client.responses.create(
            model=MODEL,
            input=[{
                "role": "user",
                "content": f"请总结以下对话以便继续工作（保留关键决策、待办和上下文）：\n{conv_text}",
            }],
            max_output_tokens=2000,
        )
        summary = extract_output_text(response)
        return [
            {"role": "user", "content": f"[上下文已压缩，原始记录：{path}]\n{summary}"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "已理解压缩摘要，继续工作。"}]},
        ]

    async def run(self, user_message: str, history: list[dict], emit: Callable) -> list[dict]:
        """
        运行 Agent 循环（Responses API）。
        emit(event_dict) 将事件推送给前端（SSE）。
        返回更新后的简单历史列表（供前端下次发送）。
        """
        await self._ensure_mcp()

        # 绑定团队事件回调
        self.team.event_callback = emit

        # 构建 Responses API input（含历史）
        input_items = self._convert_history_to_input(history)
        input_items.append({"role": "user", "content": user_message})

        loaded_skills: set[str] = set()
        tools = self._select_tools(user_message, loaded_skills)
        system_prompt = self._build_system_prompt()

        emit({"type": "agent_start"})

        rounds_without_todo = 0
        final_text = ""

        for iteration in range(30):
            # s08：注入后台任务通知
            notifs = self.bg.drain()
            if notifs:
                notif_text = "\n".join(
                    f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
                )
                input_items.append({
                    "role": "user",
                    "content": f"<background-results>\n{notif_text}\n</background-results>",
                })

            # s09：检查 lead 收件箱
            inbox = self.bus.read_inbox("lead")
            if inbox:
                input_items.append({
                    "role": "user",
                    "content": f"<inbox>{json.dumps(inbox, indent=2, ensure_ascii=False)}</inbox>",
                })

            # s06：token 估算 + 自动压缩
            compact_threshold = TOKEN_THRESHOLD * 2 if loaded_skills else TOKEN_THRESHOLD
            if estimate_tokens(input_items) > compact_threshold:
                emit({"type": "system_event", "event": "auto_compact", "message": "上下文已自动压缩"})
                input_items = await self._compress(input_items)

            # ===== 流式调用 Responses API =====
            turn_content = ""
            pending_tool_calls: dict[str, dict] = {}  # item_id -> {id, call_id, name, args_buffer}
            manual_compress = False
            used_todo = False

            final_response = None
            last_api_error = None
            used_non_stream_fallback = False
            force_non_stream_fallback = False
            model_timeout_seconds = self._model_timeout_seconds(loaded_skills)
            for api_attempt in range(3):
                try:
                    async with asyncio.timeout(model_timeout_seconds):
                        async with self.client.responses.stream(
                            model=MODEL,
                            input=input_items,
                            instructions=system_prompt,
                            tools=tools,
                        ) as stream:
                            async for event in stream:
                                etype = event.type

                                # 文本流
                                if etype == "response.output_text.delta":
                                    turn_content += event.delta
                                    emit({"type": "message_delta", "content": event.delta})

                                # 新的输出项（function_call 出现时）
                                elif etype == "response.output_item.added":
                                    item = event.item
                                    if getattr(item, "type", None) == "function_call":
                                        pending_tool_calls[item.id] = {
                                            "id": getattr(item, "id", item.call_id),
                                            "name": item.name,
                                            "call_id": item.call_id,
                                            "args_buffer": "",
                                        }

                                # 函数参数流
                                elif etype == "response.function_call_arguments.delta":
                                    item_id = getattr(event, "item_id", None)
                                    if item_id in pending_tool_calls:
                                        pending_tool_calls[item_id]["args_buffer"] += event.delta

                                # 函数参数完成 → emit tool_call
                                elif etype == "response.function_call_arguments.done":
                                    item_id = getattr(event, "item_id", None)
                                    if item_id in pending_tool_calls:
                                        tc = pending_tool_calls[item_id]
                                        try:
                                            tc["parsed_args"] = json.loads(event.arguments)
                                        except Exception:
                                            tc["parsed_args"] = {}

                                        fn_name = tc["name"]
                                        tool_type = "builtin"
                                        if self.mcp and self.mcp.is_mcp_tool(fn_name):
                                            tool_type = "mcp"
                                        elif fn_name == "load_skill":
                                            tool_type = "skill"

                                        emit({
                                            "type": "tool_call",
                                            "id": tc["call_id"],
                                            "name": fn_name,
                                            "tool_type": tool_type,
                                            "input": tc["parsed_args"],
                                        })

                            final_response = await stream.get_final_response()
                    break
                except Exception as e:
                    last_api_error = e
                    if is_missing_response_completed_error(e):
                        force_non_stream_fallback = True
                        break
                    if api_attempt < 2 and is_retryable_api_error(e):
                        emit({
                            "type": "system_event",
                            "event": "api_retry",
                            "message": f"上游模型服务暂时不可用，正在重试（{api_attempt + 1}/2）",
                        })
                        await asyncio.sleep(1 + api_attempt)
                        continue
                    break

            if final_response is None and last_api_error and (
                force_non_stream_fallback or is_retryable_api_error(last_api_error)
            ):
                fallback_message = (
                    "流式响应尾事件缺失，已自动补刷为非流式继续完成"
                    if force_non_stream_fallback
                    else "流式响应不可用，已切换为非流式模式继续执行"
                )
                emit({
                    "type": "system_event",
                    "event": "stream_fallback",
                    "message": fallback_message,
                })
                try:
                    async with asyncio.timeout(model_timeout_seconds):
                        final_response = await self.client.responses.create(
                            model=MODEL,
                            input=input_items,
                            instructions=system_prompt,
                            tools=tools,
                        )
                    used_non_stream_fallback = True
                    turn_content = extract_output_text(final_response)
                except Exception as e:
                    last_api_error = e

            if final_response is None:
                emit({
                    "type": "error",
                    "message": format_api_error(last_api_error, model_timeout_seconds),
                })
                break

            if used_non_stream_fallback:
                for item in final_response.output:
                    if item.type != "function_call":
                        continue
                    try:
                        parsed_args = json.loads(item.arguments) if item.arguments else {}
                    except Exception:
                        parsed_args = {}

                    tool_type = "builtin"
                    if self.mcp and self.mcp.is_mcp_tool(item.name):
                        tool_type = "mcp"
                    elif item.name == "load_skill":
                        tool_type = "skill"

                    emit({
                        "type": "tool_call",
                        "id": item.call_id,
                        "name": item.name,
                        "tool_type": tool_type,
                        "input": parsed_args,
                    })

            # ===== 将响应输出追加到 input_items（供下轮使用）=====
            tool_calls_this_turn = []
            for item in final_response.output:
                if item.type == "message":
                    # 文本消息（turn_content 已在流式中积累）
                    input_items.append({
                        "id": item.id,
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": turn_content}],
                    })
                elif item.type == "function_call":
                    input_items.append({
                        "type": "function_call",
                        "id": item.id,
                        "call_id": item.call_id,
                        "name": item.name,
                        "arguments": item.arguments,
                    })
                    tool_calls_this_turn.append(item)

            # 没有工具调用 → 本轮结束
            if not tool_calls_this_turn:
                final_text = turn_content
                if turn_content:
                    emit({"type": "message_done", "content": turn_content})
                break

            # ===== 执行工具调用 =====
            tools_changed = False
            for item in tool_calls_this_turn:
                fn_name = item.name
                try:
                    fn_args = json.loads(item.arguments) if item.arguments else {}
                except Exception:
                    fn_args = {}

                if fn_name == "TodoWrite":
                    used_todo = True
                if fn_name == "compress":
                    manual_compress = True

                try:
                    output = await self._dispatch_tool(fn_name, fn_args, loaded_skills)
                except Exception as e:
                    output = f"工具执行错误: {e}"

                output_text = str(output)
                context_output = truncate_tool_output_for_context(fn_name, output_text)
                is_error = output_text.startswith(("错误", "Error", "工具执行错误"))
                emit({
                    "type": "tool_result",
                    "id": item.call_id,
                    "name": fn_name,
                    "content": output_text[:2000],
                    "error": is_error,
                })

                input_items.append({
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": context_output,
                })

                if fn_name == "load_skill":
                    skill_name = str(fn_args.get("name", "")).strip()
                    if skill_name in self.skills.list_names() and skill_name not in loaded_skills:
                        loaded_skills.add(skill_name)
                        tools_changed = True
                        followup = self._build_loaded_skill_followup(skill_name)
                        if followup:
                            input_items.append({"role": "user", "content": followup})

            # s03：Todo nag
            rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
            if self.todo.has_open_items() and rounds_without_todo >= 3:
                input_items.append({"role": "user", "content": "<reminder>请更新你的 Todo 列表。</reminder>"})

            if tools_changed:
                tools = self._select_tools(user_message, loaded_skills)

            # 手动压缩
            if manual_compress:
                emit({"type": "system_event", "event": "manual_compact", "message": "上下文已手动压缩"})
                input_items = await self._compress(input_items)

        emit({"type": "done"})

        # 返回更新后的简单历史（用户消息 + 最终文本回答）
        updated_history = list(history)
        updated_history.append({"role": "user", "content": user_message})
        if final_text:
            updated_history.append({"role": "assistant", "content": final_text})
        return updated_history

    async def _dispatch_tool(self, fn_name: str, fn_args: dict, loaded_skills: set[str]) -> str:
        """分发工具调用到对应处理函数。"""
        # MCP 工具
        if self.mcp and self.mcp.is_mcp_tool(fn_name):
            return await self.mcp.call_tool(fn_name, fn_args)

        # 内置工具
        handlers = {
            "bash":             lambda: run_bash(
                self._rewrite_bash_command_for_loaded_skills(fn_args["command"], loaded_skills)
            ),
            "read_file":        lambda: self._run_read_for_loaded_skills(
                fn_args["path"], fn_args.get("limit"), loaded_skills
            ),
            "write_file":       lambda: run_write(fn_args["path"], fn_args["content"]),
            "edit_file":        lambda: run_edit(fn_args["path"], fn_args["old_text"], fn_args["new_text"]),
            "task_create":      lambda: self.task_mgr.create(fn_args["subject"], fn_args.get("description", "")),
            "task_get":         lambda: self.task_mgr.get(fn_args["task_id"]),
            "task_update":      lambda: self.task_mgr.update(fn_args["task_id"], fn_args.get("status"), fn_args.get("add_blocked_by"), fn_args.get("add_blocks")),
            "task_list":        lambda: self.task_mgr.list_all(),
            "background_run":   lambda: self.bg.run(fn_args["command"], fn_args.get("timeout", 120)),
            "check_background": lambda: self.bg.check(fn_args.get("task_id")),
            "list_teammates":   lambda: self.team.list_all(),
            "read_inbox":       lambda: json.dumps(self.bus.read_inbox("lead"), indent=2, ensure_ascii=False),
            "broadcast":        lambda: self.bus.broadcast("lead", fn_args["content"], self.team.member_names()),
            "shutdown_request": lambda: self.team.request_shutdown(fn_args["teammate"]),
            "plan_approval":    lambda: self.team.approve_plan(fn_args["request_id"], fn_args["approve"], fn_args.get("feedback", "")),
            "send_message":     lambda: self.bus.send("lead", fn_args["to"], fn_args["content"], fn_args.get("msg_type", "message")),
            "compress":         lambda: "压缩中...",
            "load_skill":       lambda: self.skills.load(fn_args["name"]),
        }

        if fn_name == "TodoWrite":
            try:
                return self.todo.update(fn_args["items"])
            except ValueError as e:
                return f"错误: {e}"

        if fn_name == "spawn_teammate":
            return self.team.spawn(
                fn_args["name"], fn_args["role"], fn_args["prompt"],
                None,  # 传 None，team_manager 内部自己创建同步客户端
                MODEL,
            )

        handler = handlers.get(fn_name)
        if handler:
            return handler()
        return f"未知工具: {fn_name}"


# 全局单例（在 FastAPI 应用中复用）
_agent_instance: AgentRunner | None = None


def get_agent() -> AgentRunner:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = AgentRunner()
    return _agent_instance
