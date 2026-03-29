"""
主 Agent 循环
基于 s_full.py 模式，使用 OpenAI SDK + MCP + Skills + 团队协议
通过异步事件队列将中间步骤实时推送给前端（SSE）。
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
from typing import AsyncIterator, Callable

from openai import OpenAI

from mcp_manager import MCPManager, load_mcp_configs
from skill_loader import SkillLoader
from task_manager import TaskManager
from team_manager import MessageBus, TeamManager

# ===== 路径配置 =====
WORKDIR = Path(__file__).parent.parent
TASKS_DIR = WORKDIR / ".tasks"
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", str(WORKDIR / "skills")))
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOKEN_THRESHOLD = 80000

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")


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


# ===== Agent Runner =====
class AgentRunner:
    """
    主 Agent 类，持有所有子系统，提供 run() 异步方法。
    run() 通过 emit() 发送 SSE 事件。
    """

    def __init__(self):
        self.client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.todo = TodoManager()
        self.skills = SkillLoader(SKILLS_DIR)
        self.task_mgr = TaskManager(TASKS_DIR)
        self.bg = BackgroundManager()
        self.bus = MessageBus(INBOX_DIR)
        self.team = TeamManager(
            self.bus,
            self.task_mgr,
            TEAM_DIR,
            event_callback=None,  # 在 run() 中动态绑定
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
        """构建完整工具列表：内置工具 + MCP 工具 + skill 工具。"""
        builtin_tools = [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "执行 shell 命令",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string", "description": "Shell 命令"}},
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
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
            },
            {
                "type": "function",
                "function": {
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
            },
            {
                "type": "function",
                "function": {
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
            },
            {
                "type": "function",
                "function": {
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
                                        "activeForm": {"type": "string", "description": "进行时描述"},
                                    },
                                    "required": ["content", "status", "activeForm"],
                                },
                            }
                        },
                        "required": ["items"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
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
            },
            {
                "type": "function",
                "function": {
                    "name": "task_get",
                    "description": "获取任务详情",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "integer"}},
                        "required": ["task_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
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
            },
            {
                "type": "function",
                "function": {
                    "name": "task_list",
                    "description": "列出所有任务",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "background_run",
                    "description": "在后台线程执行命令",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "timeout": {"type": "integer", "default": 120},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "check_background",
                    "description": "检查后台任务状态",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
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
            },
            {
                "type": "function",
                "function": {
                    "name": "list_teammates",
                    "description": "列出所有队友及其状态",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "发送消息给队友",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to": {"type": "string"},
                            "content": {"type": "string"},
                            "msg_type": {
                                "type": "string",
                                "enum": ["message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_request", "plan_approval_response"],
                                "default": "message",
                            },
                        },
                        "required": ["to", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_inbox",
                    "description": "读取并清空 lead 的收件箱",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "broadcast",
                    "description": "向所有队友广播消息",
                    "parameters": {
                        "type": "object",
                        "properties": {"content": {"type": "string"}},
                        "required": ["content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shutdown_request",
                    "description": "优雅地请求队友关闭（s10 协议）",
                    "parameters": {
                        "type": "object",
                        "properties": {"teammate": {"type": "string"}},
                        "required": ["teammate"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "plan_approval",
                    "description": "批准或拒绝队友提交的计划（s10 协议）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string"},
                            "approve": {"type": "boolean"},
                            "feedback": {"type": "string", "default": ""},
                        },
                        "required": ["request_id", "approve"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "compress",
                    "description": "手动压缩对话上下文",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

        # Skill 工具
        if self.skills.list_names():
            builtin_tools.append(self.skills.as_openai_tool())

        # MCP 工具
        if self.mcp:
            builtin_tools.extend(self.mcp.tools)

        return builtin_tools

    def _build_system_prompt(self) -> str:
        return f"""你是一个全功能 AI Agent，工作目录：{WORKDIR}。

使用工具完成任务。优先使用 task_create/task_list 管理多步骤工作，用 TodoWrite 管理短清单。
需要专项知识时使用 load_skill 加载技能。
可用技能：
{self.skills.descriptions()}

MCP 工具可直接调用（来自外部 MCP 服务器）。
生成队友时使用 spawn_teammate，风险操作前队友需提交计划审批。"""

    async def _compress(self, messages: list) -> list:
        """自动压缩对话上下文（s06）。"""
        TRANSCRIPT_DIR.mkdir(exist_ok=True)
        path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
        conv_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
        response = self.client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": f"请总结以下对话以便继续工作（保留关键决策、待办事项和上下文）：\n{conv_text}",
                }
            ],
            max_tokens=2000,
        )
        summary = response.choices[0].message.content or "(压缩摘要)"
        return [
            {"role": "user", "content": f"[上下文已压缩，原始记录：{path}]\n{summary}"},
            {"role": "assistant", "content": "已理解压缩摘要，继续工作。"},
        ]

    async def run(
        self, user_message: str, history: list[dict], emit: Callable
    ) -> AsyncIterator[None]:
        """
        运行 Agent 循环。
        emit(event_dict) 将事件发送给前端（SSE）。
        """
        await self._ensure_mcp()

        # 绑定团队事件回调
        self.team.event_callback = emit

        messages = list(history)
        messages.append({"role": "user", "content": user_message})

        tools = self._build_tools()
        system_prompt = self._build_system_prompt()

        emit({"type": "agent_start"})

        rounds_without_todo = 0
        max_iterations = 30

        for iteration in range(max_iterations):
            # s08：注入后台任务通知
            notifs = self.bg.drain()
            if notifs:
                txt = "\n".join(
                    f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
                )
                messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
                messages.append({"role": "assistant", "content": "已收到后台任务结果。"})

            # s09：检查收件箱
            inbox = self.bus.read_inbox("lead")
            if inbox:
                messages.append(
                    {"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2, ensure_ascii=False)}</inbox>"}
                )
                messages.append({"role": "assistant", "content": "已查看收件箱。"})

            # s06：token 估算 + 自动压缩
            if estimate_tokens(messages) > TOKEN_THRESHOLD:
                emit({"type": "system_event", "event": "auto_compact", "message": "上下文已自动压缩"})
                messages = await self._compress(messages)

            # 调用 LLM
            response = self.client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=tools,
                max_tokens=4000,
                stream=True,
            )

            # 收集流式响应
            full_content = ""
            tool_calls_raw: dict[int, dict] = {}

            for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # 流式文本
                if delta.content:
                    full_content += delta.content
                    emit({"type": "message_delta", "content": delta.content})

                # 流式工具调用
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_raw:
                            tool_calls_raw[idx] = {
                                "id": tc_delta.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc_delta.id:
                            tool_calls_raw[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_calls_raw[idx]["function"]["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_calls_raw[idx]["function"]["arguments"] += tc_delta.function.arguments

            finish_reason = chunk.choices[0].finish_reason if chunk.choices else "stop"

            # 构建 assistant 消息
            tool_calls_list = [tool_calls_raw[i] for i in sorted(tool_calls_raw.keys())]
            assistant_msg: dict = {"role": "assistant", "content": full_content or None}
            if tool_calls_list:
                assistant_msg["tool_calls"] = tool_calls_list
            messages.append(assistant_msg)

            if finish_reason != "tool_calls" or not tool_calls_list:
                if full_content:
                    emit({"type": "message_done", "content": full_content})
                break

            # 执行工具调用
            used_todo = False
            manual_compress = False
            tool_results = []

            for tc in tool_calls_list:
                fn_name = tc["function"]["name"]
                fn_id = tc["id"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                except json.JSONDecodeError:
                    fn_args = {}

                # 判断工具类型
                tool_type = "builtin"
                if self.mcp and self.mcp.is_mcp_tool(fn_name):
                    tool_type = "mcp"
                elif fn_name == "load_skill":
                    tool_type = "skill"

                emit({
                    "type": "tool_call",
                    "id": fn_id,
                    "name": fn_name,
                    "tool_type": tool_type,
                    "input": fn_args,
                })

                # 分发工具
                try:
                    output = await self._dispatch_tool(fn_name, fn_args)
                except Exception as e:
                    output = f"工具执行错误: {e}"

                if fn_name == "TodoWrite":
                    used_todo = True
                if fn_name == "compress":
                    manual_compress = True

                emit({
                    "type": "tool_result",
                    "id": fn_id,
                    "name": fn_name,
                    "content": str(output)[:2000],
                    "error": str(output).startswith("错误") or str(output).startswith("Error"),
                })

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": fn_id,
                    "content": str(output)[:50000],
                })

            messages.extend(tool_results)

            # s03：Todo nag
            rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
            if self.todo.has_open_items() and rounds_without_todo >= 3:
                messages.append({"role": "user", "content": "<reminder>请更新你的 Todo 列表。</reminder>"})

            # 手动压缩
            if manual_compress:
                emit({"type": "system_event", "event": "manual_compact", "message": "上下文已手动压缩"})
                messages = await self._compress(messages)

        emit({"type": "done"})
        return messages

    async def _dispatch_tool(self, fn_name: str, fn_args: dict) -> str:
        """分发工具调用到对应处理函数。"""
        # MCP 工具
        if self.mcp and self.mcp.is_mcp_tool(fn_name):
            return await self.mcp.call_tool(fn_name, fn_args)

        # 内置工具
        if fn_name == "bash":
            return run_bash(fn_args["command"])
        elif fn_name == "read_file":
            return run_read(fn_args["path"], fn_args.get("limit"))
        elif fn_name == "write_file":
            return run_write(fn_args["path"], fn_args["content"])
        elif fn_name == "edit_file":
            return run_edit(fn_args["path"], fn_args["old_text"], fn_args["new_text"])
        elif fn_name == "TodoWrite":
            try:
                return self.todo.update(fn_args["items"])
            except ValueError as e:
                return f"错误: {e}"
        elif fn_name == "load_skill":
            return self.skills.load(fn_args["name"])
        elif fn_name == "task_create":
            return self.task_mgr.create(fn_args["subject"], fn_args.get("description", ""))
        elif fn_name == "task_get":
            return self.task_mgr.get(fn_args["task_id"])
        elif fn_name == "task_update":
            return self.task_mgr.update(
                fn_args["task_id"],
                fn_args.get("status"),
                fn_args.get("add_blocked_by"),
                fn_args.get("add_blocks"),
            )
        elif fn_name == "task_list":
            return self.task_mgr.list_all()
        elif fn_name == "background_run":
            return self.bg.run(fn_args["command"], fn_args.get("timeout", 120))
        elif fn_name == "check_background":
            return self.bg.check(fn_args.get("task_id"))
        elif fn_name == "spawn_teammate":
            return self.team.spawn(
                fn_args["name"],
                fn_args["role"],
                fn_args["prompt"],
                self.client,
                MODEL,
            )
        elif fn_name == "list_teammates":
            return self.team.list_all()
        elif fn_name == "send_message":
            return self.bus.send(
                "lead",
                fn_args["to"],
                fn_args["content"],
                fn_args.get("msg_type", "message"),
            )
        elif fn_name == "read_inbox":
            return json.dumps(self.bus.read_inbox("lead"), indent=2, ensure_ascii=False)
        elif fn_name == "broadcast":
            return self.bus.broadcast("lead", fn_args["content"], self.team.member_names())
        elif fn_name == "shutdown_request":
            return self.team.request_shutdown(fn_args["teammate"])
        elif fn_name == "plan_approval":
            return self.team.approve_plan(
                fn_args["request_id"],
                fn_args["approve"],
                fn_args.get("feedback", ""),
            )
        elif fn_name == "compress":
            return "压缩中..."
        else:
            return f"未知工具: {fn_name}"


# 全局单例（在 FastAPI 应用中复用）
_agent_instance: AgentRunner | None = None


def get_agent() -> AgentRunner:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = AgentRunner()
    return _agent_instance
