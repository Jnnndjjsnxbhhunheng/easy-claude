"""
持久化任务管理器（s07 模式）
任务以 JSON 文件存储在 .tasks/ 目录中。
"""
import json
from pathlib import Path


class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.tasks_dir = tasks_dir
        tasks_dir.mkdir(parents=True, exist_ok=True)

    def _next_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.tasks_dir.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        p = self.tasks_dir / f"task_{tid}.json"
        if not p.exists():
            raise ValueError(f"任务 {tid} 不存在")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        (self.tasks_dir / f"task_{task['id']}.json").write_text(
            json.dumps(task, indent=2, ensure_ascii=False)
        )

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id(),
            "subject": subject,
            "description": description,
            "status": "pending",
            "owner": None,
            "blockedBy": [],
            "blocks": [],
        }
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, tid: int) -> str:
        return json.dumps(self._load(tid), indent=2, ensure_ascii=False)

    def update(
        self,
        tid: int,
        status: str | None = None,
        add_blocked_by: list | None = None,
        add_blocks: list | None = None,
    ) -> str:
        task = self._load(tid)
        if status:
            task["status"] = status
            if status == "completed":
                # 解除其他任务的依赖
                for f in self.tasks_dir.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
            if status == "deleted":
                (self.tasks_dir / f"task_{tid}.json").unlink(missing_ok=True)
                return f"任务 {tid} 已删除"
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_all(self) -> str:
        tasks = [
            json.loads(f.read_text())
            for f in sorted(self.tasks_dir.glob("task_*.json"))
        ]
        if not tasks:
            return "暂无任务"
        lines = []
        for t in tasks:
            mark = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(
                t["status"], "[?]"
            )
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (阻塞于: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{mark} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"任务 #{tid} 已被 {owner} 认领"

    def get_unclaimed(self) -> list[dict]:
        result = []
        for f in sorted(self.tasks_dir.glob("task_*.json")):
            t = json.loads(f.read_text())
            if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                result.append(t)
        return result
