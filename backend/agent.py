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

    def _build_system_prompt(self) -> str:
        return f"""你是一个全功能 AI Agent，工作目录：{WORKDIR}。

使用工具完成任务。优先使用 task_create/task_list 管理多步骤工作，用 TodoWrite 管理短清单。
需要专项知识时使用 load_skill 加载技能。

可用技能：
{self.skills.descriptions()}

MCP 工具可直接调用（来自外部 MCP 服务器）。
生成队友时使用 spawn_teammate，队友执行风险操作前需提交计划审批。"""

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
        summary = ""
        for item in response.output:
            if item.type == "message":
                for c in item.content:
                    if hasattr(c, "text"):
                        summary += c.text
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

        tools = self._build_tools()
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
            if estimate_tokens(input_items) > TOKEN_THRESHOLD:
                emit({"type": "system_event", "event": "auto_compact", "message": "上下文已自动压缩"})
                input_items = await self._compress(input_items)

            # ===== 流式调用 Responses API =====
            turn_content = ""
            pending_tool_calls: dict[str, dict] = {}  # call_id -> {id, name, args_buffer}
            manual_compress = False
            used_todo = False

            try:
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
                                pending_tool_calls[item.call_id] = {
                                    "id": getattr(item, "id", item.call_id),
                                    "name": item.name,
                                    "call_id": item.call_id,
                                    "args_buffer": "",
                                }

                        # 函数参数流
                        elif etype == "response.function_call_arguments.delta":
                            if event.call_id in pending_tool_calls:
                                pending_tool_calls[event.call_id]["args_buffer"] += event.delta

                        # 函数参数完成 → emit tool_call
                        elif etype == "response.function_call_arguments.done":
                            if event.call_id in pending_tool_calls:
                                tc = pending_tool_calls[event.call_id]
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

                    final_response = stream.get_final_response()

            except Exception as e:
                emit({"type": "error", "message": f"API 调用失败: {e}"})
                break

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
                    output = await self._dispatch_tool(fn_name, fn_args)
                except Exception as e:
                    output = f"工具执行错误: {e}"

                is_error = str(output).startswith(("错误", "Error", "工具执行错误"))
                emit({
                    "type": "tool_result",
                    "id": item.call_id,
                    "name": fn_name,
                    "content": str(output)[:2000],
                    "error": is_error,
                })

                input_items.append({
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": str(output)[:50000],
                })

            # s03：Todo nag
            rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
            if self.todo.has_open_items() and rounds_without_todo >= 3:
                input_items.append({"role": "user", "content": "<reminder>请更新你的 Todo 列表。</reminder>"})

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

    async def _dispatch_tool(self, fn_name: str, fn_args: dict) -> str:
        """分发工具调用到对应处理函数。"""
        # MCP 工具
        if self.mcp and self.mcp.is_mcp_tool(fn_name):
            return await self.mcp.call_tool(fn_name, fn_args)

        # 内置工具
        handlers = {
            "bash":             lambda: run_bash(fn_args["command"]),
            "read_file":        lambda: run_read(fn_args["path"], fn_args.get("limit")),
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
