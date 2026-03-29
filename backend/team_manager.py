"""
团队管理器（s09/s10/s11 模式）— Responses API 版本
消息总线（JSONL 收件箱）+ 队友生命周期 + 关闭协议 + 计划审批协议
队友使用 OpenAI Responses API（同步，在独立线程中运行）。
"""
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

POLL_INTERVAL = 5
IDLE_TIMEOUT = 60
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_request",
    "plan_approval_response",
}

# Responses API 格式的队友工具
_TEAMMATE_TOOLS = [
    {
        "type": "function",
        "name": "bash",
        "description": "执行 shell 命令",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "读取文件内容",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "写入文件",
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
        "name": "send_message",
        "description": "发送消息给团队成员",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["to", "content"],
        },
    },
    {
        "type": "function",
        "name": "submit_plan",
        "description": "向 lead 提交计划等待审批，在执行风险操作前必须调用",
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "计划详情"},
            },
            "required": ["plan"],
        },
    },
    {
        "type": "function",
        "name": "idle",
        "description": "当前工作完成，进入空闲状态",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "claim_task",
        "description": "认领一个待处理任务",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
]


class MessageBus:
    """基于 JSONL 文件的消息总线，每个 agent 一个收件箱文件。"""

    def __init__(self, inbox_dir: Path):
        self.inbox_dir = inbox_dir
        inbox_dir.mkdir(parents=True, exist_ok=True)

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict | None = None,
    ) -> str:
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        with open(self.inbox_dir / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"消息已发送给 {to}"

    def read_inbox(self, name: str) -> list[dict]:
        """读取并清空收件箱。"""
        path = self.inbox_dir / f"{name}.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        path.write_text("")
        return [json.loads(line) for line in lines if line.strip()]

    def broadcast(self, sender: str, content: str, names: list[str]) -> str:
        count = 0
        for name in names:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"已广播给 {count} 位队友"


class TeamManager:
    """管理队友的生命周期，支持 s10 关闭协议和计划审批协议。"""

    def __init__(
        self,
        bus: MessageBus,
        task_manager,
        team_dir: Path,
        event_callback: Callable | None = None,
    ):
        self.bus = bus
        self.task_manager = task_manager
        self.team_dir = team_dir
        self.event_callback = event_callback
        team_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = team_dir / "config.json"
        self.config = self._load_config()
        self.threads: dict[str, threading.Thread] = {}

        # s10 协议状态追踪
        self.shutdown_requests: dict[str, dict] = {}
        self.plan_requests: dict[str, dict] = {}

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False)
        )

    def _find_member(self, name: str) -> dict | None:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def _emit(self, event: dict):
        if self.event_callback:
            self.event_callback(event)

    def spawn(
        self,
        name: str,
        role: str,
        prompt: str,
        _unused_client,  # 保持签名兼容，内部自己创建客户端
        model: str,
    ) -> str:
        """生成一个新的队友线程。"""
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"错误：'{name}' 当前状态为 {member['status']}，无法重新生成"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()

        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt, model),
            daemon=True,
        )
        thread.start()
        self.threads[name] = thread

        self._emit({"type": "team_event", "event": "spawned", "teammate": name, "role": role})
        return f"队友 '{name}'（角色：{role}）已生成"

    def _teammate_loop(self, name: str, role: str, initial_prompt: str, model: str):
        """队友的主循环（同步，在独立线程中运行，使用 Responses API）。"""
        from openai import OpenAI

        # 队友使用同步 OpenAI 客户端（在线程中）
        client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL"),
        )

        team_name = self.config["team_name"]
        sys_prompt = (
            f"你是 '{name}'，角色：{role}，所属团队：{team_name}。"
            f"完成工作后调用 idle 工具。你可以自动认领任务。"
            f"需要执行风险操作前，先调用 submit_plan 工具提交计划等待审批。"
        )

        # Responses API input 格式
        input_items: list[dict] = [{"role": "user", "content": initial_prompt}]

        def _call_api() -> tuple[str, list]:
            """调用 Responses API，返回 (text_content, function_call_items)."""
            response = client.responses.create(
                model=model,
                input=input_items,
                instructions=sys_prompt,
                tools=_TEAMMATE_TOOLS,
            )
            text = ""
            calls = []
            for item in response.output:
                if item.type == "message":
                    for c in item.content:
                        if hasattr(c, "text"):
                            text += c.text
                    input_items.append({
                        "id": item.id,
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
                elif item.type == "function_call":
                    input_items.append({
                        "type": "function_call",
                        "id": item.id,
                        "call_id": item.call_id,
                        "name": item.name,
                        "arguments": item.arguments,
                    })
                    calls.append(item)
            return text, calls

        while True:
            # ---- 工作阶段 ----
            for _ in range(50):
                # 检查收件箱
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        req_id = msg.get("request_id", "")
                        self.bus.send(name, "lead", "已收到关闭请求，正在清理...",
                                      "shutdown_response", {"request_id": req_id})
                        self._set_status(name, "shutdown")
                        self._emit({"type": "team_event", "event": "shutdown_complete", "teammate": name})
                        return
                    elif msg.get("type") == "plan_approval_response":
                        approved = msg.get("approve", False)
                        feedback = msg.get("feedback", "")
                        status_text = "已批准" if approved else "已拒绝"
                        input_items.append({
                            "role": "user",
                            "content": f"<plan_approval>{status_text}. 反馈: {feedback}</plan_approval>",
                        })
                    else:
                        input_items.append({
                            "role": "user",
                            "content": json.dumps(msg, ensure_ascii=False),
                        })

                try:
                    _, tool_calls = _call_api()
                except Exception as e:
                    self._emit({"type": "team_event", "event": "error", "teammate": name, "error": str(e)})
                    self._set_status(name, "shutdown")
                    return

                if not tool_calls:
                    break

                idle_requested = False
                for tc in tool_calls:
                    fn_name = tc.name
                    try:
                        fn_args = json.loads(tc.arguments) if tc.arguments else {}
                    except Exception:
                        fn_args = {}

                    if fn_name == "idle":
                        idle_requested = True
                        output = "进入空闲阶段"
                    elif fn_name == "send_message":
                        output = self.bus.send(name, fn_args["to"], fn_args["content"])
                    elif fn_name == "claim_task":
                        output = self.task_manager.claim(fn_args["task_id"], name)
                    elif fn_name == "submit_plan":
                        req_id = str(uuid.uuid4())[:8]
                        plan_text = fn_args.get("plan", "")
                        self.plan_requests[req_id] = {"from": name, "status": "pending", "plan": plan_text}
                        self.bus.send(name, "lead", plan_text, "plan_approval_request",
                                      {"request_id": req_id})
                        self._emit({
                            "type": "team_event",
                            "event": "plan_submitted",
                            "teammate": name,
                            "request_id": req_id,
                            "plan": plan_text,
                        })
                        output = f"计划已提交，等待审批（request_id: {req_id}）"
                    elif fn_name == "bash":
                        try:
                            r = subprocess.run(  # noqa: S602
                                fn_args["command"], shell=True,
                                capture_output=True, text=True, timeout=30
                            )
                            output = (r.stdout + r.stderr).strip()[:5000] or "(无输出)"
                        except Exception as e:
                            output = f"错误: {e}"
                    elif fn_name == "read_file":
                        try:
                            output = Path(fn_args["path"]).read_text(encoding="utf-8")[:5000]
                        except Exception as e:
                            output = f"错误: {e}"
                    elif fn_name == "write_file":
                        try:
                            p = Path(fn_args["path"])
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_text(fn_args["content"], encoding="utf-8")
                            output = f"已写入 {fn_args['path']}"
                        except Exception as e:
                            output = f"错误: {e}"
                    else:
                        output = f"未知工具: {fn_name}"

                    self._emit({
                        "type": "team_event",
                        "event": "tool_call",
                        "teammate": name,
                        "tool": fn_name,
                        "output_preview": str(output)[:200],
                    })

                    input_items.append({
                        "type": "function_call_output",
                        "call_id": tc.call_id,
                        "output": str(output),
                    })

                if idle_requested:
                    break

            # ---- 空闲阶段（s11：轮询任务和消息）----
            self._set_status(name, "idle")
            self._emit({"type": "team_event", "event": "idle", "teammate": name})
            resumed = False

            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)

                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            req_id = msg.get("request_id", "")
                            self.bus.send(name, "lead", "已收到关闭请求",
                                          "shutdown_response", {"request_id": req_id})
                            self._set_status(name, "shutdown")
                            self._emit({"type": "team_event", "event": "shutdown_complete", "teammate": name})
                            return
                        input_items.append({
                            "role": "user",
                            "content": json.dumps(msg, ensure_ascii=False),
                        })
                    resumed = True
                    break

                # 自动认领无主任务（s11）
                unclaimed = self.task_manager.get_unclaimed()
                if unclaimed:
                    task = unclaimed[0]
                    self.task_manager.claim(task["id"], name)
                    input_items.append({
                        "role": "user",
                        "content": (
                            f"<auto-claimed>任务 #{task['id']}: {task['subject']}\n"
                            f"{task.get('description', '')}</auto-claimed>"
                        ),
                    })
                    self._emit({
                        "type": "team_event",
                        "event": "task_claimed",
                        "teammate": name,
                        "task_id": task["id"],
                        "subject": task["subject"],
                    })
                    resumed = True
                    break

            if not resumed:
                self._set_status(name, "shutdown")
                self._emit({"type": "team_event", "event": "shutdown_timeout", "teammate": name})
                return

            self._set_status(name, "working")

    def list_all(self) -> str:
        if not self.config["members"]:
            return "暂无队友"
        lines = [f"团队: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list[str]:
        return [m["name"] for m in self.config["members"]]

    # s10 关闭协议
    def request_shutdown(self, teammate: str) -> str:
        req_id = str(uuid.uuid4())[:8]
        self.shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
        self.bus.send("lead", teammate, "请优雅关闭", "shutdown_request", {"request_id": req_id})
        self._emit({
            "type": "team_event",
            "event": "shutdown_request",
            "teammate": teammate,
            "request_id": req_id,
        })
        return f"关闭请求 {req_id} 已发送给 '{teammate}'"

    # s10 计划审批协议
    def approve_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        req = self.plan_requests.get(request_id)
        if not req:
            return f"错误：未知 request_id '{request_id}'"
        req["status"] = "approved" if approve else "rejected"
        self.bus.send(
            "lead",
            req["from"],
            feedback,
            "plan_approval_response",
            {"request_id": request_id, "approve": approve, "feedback": feedback},
        )
        status_text = "批准" if approve else "拒绝"
        self._emit({
            "type": "team_event",
            "event": "plan_reviewed",
            "teammate": req["from"],
            "request_id": request_id,
            "approved": approve,
            "feedback": feedback,
        })
        return f"已{status_text}来自 '{req['from']}' 的计划"
