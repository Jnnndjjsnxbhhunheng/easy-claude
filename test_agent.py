#!/usr/bin/env python3
"""
本地测试脚本 — 直接测试 Agent 各项功能（无需启动服务器）
运行：python test_agent.py
需要先设置环境变量（或有 .env 文件）
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# 加载 .env
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# 加入 backend 路径
sys.path.insert(0, str(Path(__file__).parent / "backend"))

from agent import AgentRunner  # noqa: E402

# ===== 颜色输出 =====
def c(text, color):
    codes = {"green": "\033[92m", "yellow": "\033[93m", "blue": "\033[94m",
             "cyan": "\033[96m", "red": "\033[91m", "bold": "\033[1m", "reset": "\033[0m"}
    return f"{codes.get(color,'')}{text}{codes['reset']}"


def print_event(event: dict):
    t = event.get("type", "")
    if t == "agent_start":
        print(c("  ⚡ Agent 开始处理...", "cyan"))
    elif t == "tool_call":
        badge = {"mcp": c("[MCP]", "blue"), "skill": c("[Skill]", "yellow"), "builtin": c("[内置]", "bold")}.get(event.get("tool_type", ""), "")
        inp = json.dumps(event.get("input", {}), ensure_ascii=False)
        if len(inp) > 80:
            inp = inp[:80] + "..."
        print(f"  🔧 {badge} {c(event['name'], 'bold')} ← {inp}")
    elif t == "tool_result":
        content = event.get("content", "")
        if len(content) > 120:
            content = content[:120] + "..."
        status = c("✗ 错误", "red") if event.get("error") else c("✓", "green")
        print(f"  {status} {c(event['name'], 'bold')} → {content}")
    elif t == "team_event":
        print(f"  👥 {c('团队', 'yellow')}: {event.get('event')} {event.get('teammate', '')}")
    elif t == "system_event":
        print(f"  🔄 系统: {event.get('message', '')}")
    elif t == "message_delta":
        print(event.get("content", ""), end="", flush=True)
    elif t == "message_done":
        print()  # 换行
    elif t == "error":
        print(c(f"  ❌ 错误: {event.get('message', '')}", "red"))
    elif t == "done":
        print(c("  ✅ 完成", "green"))
    elif t == "history_update":
        pass  # 静默


async def run_test(name: str, message: str, history: list = None, agent: AgentRunner = None):
    print(f"\n{'='*60}")
    print(c(f"测试: {name}", "bold"))
    print(f"用户: {message}")
    print("-" * 60)

    events = []

    def emit(event):
        events.append(event)
        print_event(event)

    result_history = await agent.run(message, history or [], emit)
    return result_history


async def main():
    print(c("\n🚀 Easy Claude Agent — 功能测试", "bold"))
    print(f"模型: {os.environ.get('OPENAI_MODEL', 'gpt-4o')}")
    print(f"API Base: {os.environ.get('OPENAI_BASE_URL', '(默认 OpenAI)')}")

    agent = AgentRunner()

    # ===== 测试 1: 基础对话 =====
    h = await run_test(
        "基础对话",
        "你好！请用一句话介绍你自己，以及你能做什么。",
        agent=agent,
    )

    # ===== 测试 2: 内置工具 - bash =====
    h = await run_test(
        "内置工具 bash",
        "运行 `echo hello && date` 命令，告诉我输出结果。",
        agent=agent,
    )

    # ===== 测试 3: 内置工具 - 文件读写 =====
    h = await run_test(
        "内置工具 文件读写",
        "在当前目录创建一个名为 test_output.txt 的文件，内容为 'Agent test passed!'，然后读取并确认内容。",
        agent=agent,
    )

    # ===== 测试 4: Skill 加载 =====
    h = await run_test(
        "Skill 加载（code-review）",
        "加载 code-review 技能，然后对这段代码做简单审查：\n```python\ndef add(a, b): return a+b\nresult = add('1', 2)\n```",
        agent=agent,
    )

    # ===== 测试 5: 任务管理（s07）=====
    h = await run_test(
        "任务管理（task_create/list）",
        "创建两个任务：1) '编写测试文档'  2) '代码重构'，然后列出所有任务。",
        agent=agent,
    )

    # ===== 测试 6: TodoWrite（s03）=====
    h = await run_test(
        "TodoWrite 任务清单",
        "用 TodoWrite 工具创建一个包含3项的待办清单：安装依赖（pending）、编写代码（in_progress）、运行测试（pending）。",
        agent=agent,
    )

    # ===== 测试 7: 长对话记忆 =====
    print(f"\n{'='*60}")
    print(c("测试: 长对话记忆（多轮）", "bold"))
    print("-" * 60)

    long_history = []

    turns = [
        "我叫小明，我在学习 Python 编程。",
        "我之前提到我在学什么？另外，请介绍 Python 的列表推导式。",
        "很好。现在请给我写一个使用列表推导式生成 1-10 的平方数的例子，并用 bash 运行它验证结果。",
        "把上面的代码保存到 test_squares.py 文件，然后再运行一次。",
        "总结一下我们这轮对话做了什么事情，以及我叫什么名字。",
    ]

    for i, turn in enumerate(turns, 1):
        print(f"\n第 {i} 轮: {turn}")
        print("-" * 40)
        long_history = await run_test(
            f"长对话第{i}轮",
            turn,
            history=long_history,
            agent=agent,
        )

    # ===== 测试 8: 多步骤复杂任务（TodoWrite + 任务管理 + bash）=====
    h = await run_test(
        "多步骤复杂任务",
        """请完成以下任务：
1. 用 TodoWrite 创建一个计划清单（3项）
2. 创建一个持久化任务 '完成复杂测试'
3. 运行 `ls -la *.py 2>/dev/null || echo 'no py files'` 查看当前目录的 Python 文件
4. 更新第2步创建的任务状态为 completed
5. 更新 TodoWrite 清单，把前两项标记完成
告诉我每步的结果。""",
        agent=agent,
    )

    print(f"\n{'='*60}")
    print(c("✅ 所有测试完成！", "bold"))

    # 清理测试文件
    for f in ["test_output.txt", "test_squares.py"]:
        fp = Path(f)
        if fp.exists():
            fp.unlink()
            print(f"已清理: {f}")


if __name__ == "__main__":
    asyncio.run(main())
