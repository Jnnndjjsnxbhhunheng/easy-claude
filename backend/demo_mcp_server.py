#!/usr/bin/env python3
"""
内置演示 MCP 服务器
提供三个示例工具：calculate / get_current_time / search_files
"""
import asyncio
import glob
import math
import os
from datetime import datetime, timezone

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server("demo-mcp-server")

WORKDIR = os.getcwd()

SAFE_MATH_NAMES = {
    k: v for k, v in vars(math).items() if not k.startswith("_")
}
SAFE_MATH_NAMES.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="calculate",
            description="安全地计算数学表达式，支持 +, -, *, /, **, sqrt, sin, cos 等",
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "要计算的数学表达式，如 '2 + 2' 或 'sqrt(16)'",
                    }
                },
                "required": ["expression"],
            },
        ),
        Tool(
            name="get_current_time",
            description="获取当前的 UTC 时间（ISO 8601 格式）",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="search_files",
            description="在工作目录中按 glob 模式搜索文件",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "glob 模式，如 '**/*.py' 或 '*.txt'",
                    }
                },
                "required": ["pattern"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "calculate":
        expression = arguments.get("expression", "")
        try:
            # 安全的数学表达式求值
            result = eval(expression, {"__builtins__": {}}, SAFE_MATH_NAMES)  # noqa: S307
            return [TextContent(type="text", text=str(result))]
        except Exception as e:
            return [TextContent(type="text", text=f"计算错误: {e}")]

    elif name == "get_current_time":
        now = datetime.now(timezone.utc).isoformat()
        return [TextContent(type="text", text=now)]

    elif name == "search_files":
        pattern = arguments.get("pattern", "*")
        try:
            matches = glob.glob(pattern, recursive=True, root_dir=WORKDIR)
            if not matches:
                return [TextContent(type="text", text="未找到匹配文件")]
            result = "\n".join(sorted(matches)[:50])
            return [TextContent(type="text", text=result)]
        except Exception as e:
            return [TextContent(type="text", text=f"搜索错误: {e}")]

    return [TextContent(type="text", text=f"未知工具: {name}")]


if __name__ == "__main__":
    asyncio.run(stdio_server(app))
