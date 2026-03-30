import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

WORKDIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = WORKDIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main as backend_main


class _FakeAgent:
    def __init__(self, *, events: list[dict], updated_history: list[dict]):
        self.events = events
        self.updated_history = updated_history

    async def run(self, message, history, emit):
        for event in self.events:
            emit(event)
        return self.updated_history

    async def _ensure_mcp(self):
        return None


class MainApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend_main.app)

    def test_chat_sync_returns_final_answer_without_sse(self):
        fake_agent = _FakeAgent(
            events=[
                {"type": "agent_start"},
                {
                    "type": "tool_call",
                    "id": "call_1",
                    "name": "load_skill",
                    "tool_type": "skill",
                    "input": {"name": "brand-ranking"},
                },
                {
                    "type": "tool_result",
                    "id": "call_1",
                    "name": "load_skill",
                    "content": "<skill>brand-ranking</skill>",
                    "error": False,
                },
                {
                    "type": "message_done",
                    "content": "这是最终排行榜结果",
                },
                {"type": "done"},
            ],
            updated_history=[
                {"role": "user", "content": "厨房电器品牌排行榜"},
                {"role": "assistant", "content": "这是最终排行榜结果"},
            ],
        )

        with patch.object(backend_main, "get_agent", return_value=fake_agent):
            response = self.client.post(
                "/api/chat/sync",
                json={"message": "厨房电器品牌排行榜", "history": []},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["answer"], "这是最终排行榜结果")
        self.assertEqual(payload["message"], "厨房电器品牌排行榜")
        self.assertEqual(payload["metrics"]["tool_call_count"], 1)
        self.assertNotIn("trace", payload)

    def test_chat_sync_can_include_condensed_trace(self):
        fake_agent = _FakeAgent(
            events=[
                {
                    "type": "tool_call",
                    "id": "call_2",
                    "name": "ranking_ugc_search_aiapi",
                    "tool_type": "mcp",
                    "input": {"query": "厨房电器 品牌排行榜"},
                },
                {
                    "type": "tool_result",
                    "id": "call_2",
                    "name": "ranking_ugc_search_aiapi",
                    "content": "x" * 1200,
                    "error": False,
                },
                {
                    "type": "system_event",
                    "event": "stream_fallback",
                    "message": "流式响应不可用，已切换为非流式模式继续执行",
                },
                {"type": "message_done", "content": "最终结果"},
                {"type": "done"},
            ],
            updated_history=[
                {"role": "user", "content": "厨房电器品牌排行榜"},
                {"role": "assistant", "content": "最终结果"},
            ],
        )

        with patch.object(backend_main, "get_agent", return_value=fake_agent):
            response = self.client.post(
                "/api/chat/sync",
                json={
                    "message": "厨房电器品牌排行榜",
                    "history": [],
                    "include_trace": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["answer"], "最终结果")
        self.assertEqual(payload["trace"][0]["type"], "tool_call")
        self.assertEqual(payload["trace"][1]["type"], "tool_result")
        self.assertLessEqual(len(payload["trace"][1]["content_preview"]), 500)
        self.assertEqual(payload["trace"][2]["type"], "system_event")
        self.assertEqual(payload["trace"][3]["type"], "message_done")

    def test_chat_sync_maps_error_only_runs_to_502(self):
        fake_agent = _FakeAgent(
            events=[
                {"type": "error", "message": "模型调用超时（300s）"},
                {"type": "done"},
            ],
            updated_history=[
                {"role": "user", "content": "厨房电器品牌排行榜"},
            ],
        )

        with patch.object(backend_main, "get_agent", return_value=fake_agent):
            response = self.client.post(
                "/api/chat/sync",
                json={"message": "厨房电器品牌排行榜", "history": []},
            )

        self.assertEqual(response.status_code, 502)
        payload = response.json()["detail"]
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"], "模型调用超时（300s）")
        self.assertEqual(payload["answer"], "")


if __name__ == "__main__":
    unittest.main()
