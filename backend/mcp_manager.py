"""
MCP 服务器管理器
使用 mcp Python 包通过子进程 stdio 连接 MCP 服务器，
将 MCP 工具 schema 转换为 OpenAI function calling 格式。
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class MCPManager:
    """管理多个 MCP 服务器连接，汇总所有工具。"""

    def __init__(self, server_configs: list[dict]):
        """
        server_configs: [{"name": "demo", "command": "python", "args": [...]}]
        """
        self.server_configs = server_configs
        self._tools: list[dict] = []          # OpenAI function 格式
        self._tool_to_server: dict[str, str] = {}  # tool_name -> server_name
        self._sessions: dict[str, ClientSession] = {}
        self._contexts: list = []
        self._initialized = False

    async def initialize(self):
        """启动所有 MCP 服务器并列出它们的工具。"""
        for config in self.server_configs:
            name = config["name"]
            command = config["command"]
            args = config.get("args", [])

            # 解析相对路径（相对于项目根目录）
            if args and not os.path.isabs(args[0]):
                root = Path(__file__).parent.parent
                resolved = root / args[0]
                if resolved.exists():
                    args = [str(resolved)] + args[1:]

            params = StdioServerParameters(command=command, args=args)
            try:
                ctx = stdio_client(params)
                read, write = await ctx.__aenter__()
                self._contexts.append((ctx, name))

                session = ClientSession(read, write)
                await session.__aenter__()
                await session.initialize()

                self._sessions[name] = session

                # 列出工具并转换格式
                tools_response = await session.list_tools()
                for tool in tools_response.tools:
                    openai_tool = {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        },
                    }
                    self._tools.append(openai_tool)
                    self._tool_to_server[tool.name] = name

                logger.info(f"MCP 服务器 '{name}' 已连接，工具: {[t.name for t in tools_response.tools]}")
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
        """返回所有 MCP 工具的 OpenAI 格式列表。"""
        return self._tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_server

    async def close(self):
        for name, session in self._sessions.items():
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
        for ctx, name in self._contexts:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass


def load_mcp_configs() -> list[dict]:
    """从环境变量加载 MCP 服务器配置。"""
    raw = os.environ.get("MCP_SERVERS", "")
    if not raw:
        # 默认启动内置演示服务器
        demo_script = Path(__file__).parent / "demo_mcp_server.py"
        return [
            {
                "name": "demo",
                "command": sys.executable,
                "args": [str(demo_script)],
            }
        ]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("MCP_SERVERS 环境变量格式错误，使用默认演示服务器")
        demo_script = Path(__file__).parent / "demo_mcp_server.py"
        return [
            {
                "name": "demo",
                "command": sys.executable,
                "args": [str(demo_script)],
            }
        ]
