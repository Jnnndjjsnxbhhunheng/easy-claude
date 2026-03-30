"""
FastAPI 服务 — Responses API 版本
POST /api/chat       → SSE 流式响应（中间步骤 + 最终回答）
POST /api/chat/sync  → 同步等待直至返回最终结果 JSON
GET  /api/config     → 返回可用技能和 MCP 工具列表
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv(Path(__file__).parent.parent / ".env")

from agent import get_agent  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Easy Claude Agent API — Responses API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Message(BaseModel):
    role: str   # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[Message] = []


class ChatSyncRequest(ChatRequest):
    include_trace: bool = False


def _extract_answer_from_history(history: list[dict]) -> str:
    for item in reversed(history):
        if item.get("role") == "assistant" and item.get("content"):
            return str(item["content"])
    return ""


def _summarize_event(event: dict) -> dict | None:
    event_type = event.get("type")
    if event_type in {"message_delta", "history_update", "done"}:
        return None

    if event_type == "tool_call":
        return {
            "type": "tool_call",
            "id": event.get("id"),
            "name": event.get("name"),
            "tool_type": event.get("tool_type"),
            "input": event.get("input", {}),
        }

    if event_type == "tool_result":
        content = str(event.get("content", ""))
        return {
            "type": "tool_result",
            "id": event.get("id"),
            "name": event.get("name"),
            "error": bool(event.get("error")),
            "content_preview": content[:500],
        }

    if event_type == "message_done":
        content = str(event.get("content", ""))
        return {
            "type": "message_done",
            "content_preview": content[:1000],
        }

    if event_type == "error":
        return {
            "type": "error",
            "message": str(event.get("message", "")),
        }

    if event_type == "system_event":
        return {
            "type": "system_event",
            "event": event.get("event"),
            "message": event.get("message"),
        }

    return dict(event)


async def _run_agent_and_collect(message: str, history_dicts: list[dict]) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []

    def emit(event: dict):
        events.append(event)

    agent = get_agent()
    updated_history = await agent.run(message, history_dicts, emit)
    return updated_history, events


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """
    接受用户消息，返回 SSE 流。
    事件格式：data: {JSON}\\n\\n

    最后会发一个 history_update 事件，携带更新后的对话历史，
    供前端下次请求时作为 history 发送（实现长对话记忆）。
    """
    history_dicts = [{"role": m.role, "content": m.content} for m in req.history]

    async def event_stream():
        event_queue: asyncio.Queue = asyncio.Queue()

        def emit(event: dict):
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None:
                    event_queue.put_nowait(event)
                else:
                    event_queue.put_nowait(event)
            except Exception as e:
                logger.error(f"emit 错误: {e}")

        async def run_agent():
            try:
                updated_history, _ = await _run_agent_and_collect(req.message, history_dicts)
                # 通知前端更新历史（长对话记忆）
                emit({
                    "type": "history_update",
                    "history": updated_history,
                })
            except Exception as e:
                logger.error(f"Agent 错误: {e}", exc_info=True)
                emit({"type": "error", "message": str(e)})
            finally:
                await event_queue.put(None)  # 结束哨兵

        task = asyncio.create_task(run_agent())

        try:
            while True:
                event = await asyncio.wait_for(event_queue.get(), timeout=120.0)
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'message': '请求超时（120s）'}, ensure_ascii=False)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat/sync")
async def chat_sync(req: ChatSyncRequest):
    """
    直接等待 Agent 完整执行结束，返回最终结果 JSON。

    适合服务端到服务端调用，不需要消费 SSE。
    """
    history_dicts = [{"role": m.role, "content": m.content} for m in req.history]
    started_at = time.perf_counter()

    try:
        updated_history, events = await _run_agent_and_collect(req.message, history_dicts)
    except Exception as e:
        logger.error(f"Agent 错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

    answer = _extract_answer_from_history(updated_history)
    error_event = next((event for event in reversed(events) if event.get("type") == "error"), None)
    status = "completed"
    if error_event and not answer:
        status = "error"

    response = {
        "status": status,
        "message": req.message,
        "answer": answer,
        "history": updated_history,
        "metrics": {
            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            "tool_call_count": sum(1 for event in events if event.get("type") == "tool_call"),
            "system_event_count": sum(1 for event in events if event.get("type") == "system_event"),
        },
    }

    if error_event:
        response["error"] = str(error_event.get("message", ""))
    if req.include_trace:
        response["trace"] = [
            summarized
            for event in events
            if (summarized := _summarize_event(event)) is not None
        ]

    if status == "error":
        raise HTTPException(status_code=502, detail=response)

    return response


@app.get("/api/config")
async def get_config():
    """返回当前可用的技能和 MCP 工具配置。"""
    agent = get_agent()
    await agent._ensure_mcp()

    skills = [
        {"name": name, "description": skill["meta"].get("description", "")}
        for name, skill in agent.skills.skills.items()
    ]

    mcp_tools = []
    if agent.mcp:
        for tool in agent.mcp.tools:
            mcp_tools.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
            })

    return {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
        "base_url": os.environ.get("OPENAI_BASE_URL", "(default OpenAI)"),
        "api_format": "responses",
        "skills": skills,
        "mcp_tools": mcp_tools,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
