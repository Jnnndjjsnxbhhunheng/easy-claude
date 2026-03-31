"""
MCP 服务器管理器 — Responses API 版本
使用 mcp Python 包通过子进程 stdio 连接 MCP 服务器，
将 MCP 工具 schema 转换为 OpenAI Responses API 工具格式。
"""
import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class MCPManager:
    """管理多个 MCP 服务器连接，汇总所有工具（Responses API 格式）。"""

    def __init__(self, server_configs: list[dict]):
        self.server_configs = server_configs
        self._tools: list[dict] = []           # Responses API 格式
        self._tool_to_server: dict[str, str] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stack = AsyncExitStack()
        self._initialized = False

    async def initialize(self):
        """启动所有 MCP 服务器并获取工具列表。"""
        if self._initialized:
            return

        for config in self.server_configs:
            name = config["name"]
            command = config["command"]
            args = config.get("args", [])

            # 解析相对路径
            if args and not os.path.isabs(str(args[0])):
                root = Path(__file__).parent.parent
                resolved = root / args[0]
                if resolved.exists():
                    args = [str(resolved)] + list(args[1:])

            params = StdioServerParameters(
                command=command,
                args=args,
                env=dict(os.environ),
                cwd=str(Path(__file__).parent.parent),
            )
            try:
                read, write = await self._exit_stack.enter_async_context(stdio_client(params))
                session = await self._exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session

                tools_response = await session.list_tools()
                for tool in tools_response.tools:
                    # Responses API 格式：name 在顶层
                    responses_tool = {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema or {
                            "type": "object",
                            "properties": {},
                        },
                    }
                    self._tools.append(responses_tool)
                    self._tool_to_server[tool.name] = name

                logger.info(
                    f"MCP 服务器 '{name}' 已连接，工具: "
                    f"{[t.name for t in tools_response.tools]}"
                )
            except Exception as e:
                logger.error(f"MCP 服务器 '{name}' 启动失败: {e}")

        self._initialized = True

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用指定工具，返回字符串结果。"""
        server_name = self._tool_to_server.get(tool_name)
        if not server_name:
            return f"错误：未知 MCP 工具 '{tool_name}'"

        session = self._sessions.get(server_name)
        if not session:
            return f"错误：MCP 服务器 '{server_name}' 未连接"

        try:
            result = await session.call_tool(tool_name, arguments)
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                else:
                    parts.append(str(content))
            return "\n".join(parts) if parts else "(无输出)"
        except Exception as e:
            return f"MCP 工具调用错误: {e}"

    @property
    def tools(self) -> list[dict]:
        """返回所有 MCP 工具的 Responses API 格式列表。"""
        return self._tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_server

    async def close(self):
        if not self._initialized and not self._sessions:
            return

        try:
            await self._exit_stack.aclose()
        except Exception as e:
            logger.error(f"MCP 关闭失败: {e}")
        finally:
            self._tools.clear()
            self._tool_to_server.clear()
            self._sessions.clear()
            self._exit_stack = AsyncExitStack()
            self._initialized = False


def load_mcp_configs() -> list[dict]:
    """从环境变量加载 MCP 服务器配置。"""
    raw = os.environ.get("MCP_SERVERS", "")
    if not raw or raw.strip() in ("[]", ""):
        # 不启动内置演示服务器（需用户配置）
        return []
    try:
        configs = json.loads(raw)
        # 支持 command = "python" 时自动替换为当前 Python 解释器
        for c in configs:
            if c.get("command") == "python":
                c["command"] = sys.executable
        return configs
    except json.JSONDecodeError:
        logger.error("MCP_SERVERS 环境变量格式错误，跳过 MCP 服务器")
        return []
