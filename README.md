# Easy Claude — MCP + Skill Agent with Real-Time Frontend

基于 [learn-claude-code/s_full.py](https://github.com/shareAI-lab/learn-claude-code) 的完整 Agent 实现，前端实时展示所有中间步骤。

## 功能

| 模块 | 说明 |
|------|------|
| **OpenAI SDK** | 使用 GPT-4o / 任意 OpenAI 兼容模型 |
| **MCP 工具** | 通过子进程 stdio 连接 MCP 服务器，内置演示服务器 |
| **Skills** | 从 `skills/` 目录加载 SKILL.md，按需注入上下文 |
| **TodoWrite** | 任务清单管理（s03）|
| **持久化任务** | JSON 文件任务系统，支持依赖关系（s07）|
| **后台执行** | 异步子进程管理（s08）|
| **多智能体团队** | 生成队友、消息总线、任务自动认领（s09/s11）|
| **关闭协议** | 优雅握手关闭队友（s10）|
| **计划审批** | 队友提交计划 → lead 审批（s10）|
| **上下文压缩** | microcompact + auto_compact（s06）|
| **实时前端** | SSE 流式传输，所有中间步骤内嵌显示在聊天气泡中 |

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY
```

### 2. 启动后端

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev
```

打开 http://localhost:3000

## 项目结构

```
easy-claude/
├── backend/
│   ├── main.py              # FastAPI + SSE 服务
│   ├── agent.py             # 主 Agent 循环（所有功能）
│   ├── mcp_manager.py       # MCP 服务器管理
│   ├── skill_loader.py      # Skill 加载器
│   ├── task_manager.py      # 持久化任务（s07）
│   ├── team_manager.py      # 团队管理 + 协议（s09/s10/s11）
│   ├── demo_mcp_server.py   # 内置演示 MCP 服务器
│   └── requirements.txt
├── skills/
│   ├── code-review/SKILL.md
│   └── web-assistant/SKILL.md
├── frontend/
│   └── src/
│       ├── app/page.tsx
│       └── components/
│           ├── ChatWindow.tsx    # 完整聊天 UI
│           ├── MessageBubble.tsx # 消息气泡
│           └── StepBlock.tsx     # 中间步骤卡片
└── .env.example
```

## SSE 事件格式

| 事件类型 | 说明 |
|---------|------|
| `agent_start` | Agent 开始处理 |
| `tool_call` | 工具调用（含 tool_type: mcp/skill/builtin）|
| `tool_result` | 工具返回结果 |
| `team_event` | 团队事件（生成/关闭/计划审批等）|
| `system_event` | 系统事件（上下文压缩等）|
| `message_delta` | 流式文本片段 |
| `message_done` | 文本输出完成 |
| `error` | 错误信息 |
| `done` | 本轮结束 |

## 自定义 MCP 服务器

在 `.env` 中配置：

```bash
MCP_SERVERS=[{"name":"my-server","command":"python","args":["/path/to/server.py"]}]
```

### Brand Ranking MCP Bridge

如果你已经在本机启动了 `ranking_mcp_service.py`，可以把 brand-ranking 所需的 MCP bridge 加到 `MCP_SERVERS`：

```bash
RANKING_MCP_BASE_URL=http://127.0.0.1:8000
RANKING_MCP_TIMEOUT_SECONDS=30
# 可选：本地服务如果需要鉴权再设置
# RANKING_MCP_BEARER_TOKEN=
# RANKING_MCP_API_KEY=
# RANKING_MCP_API_KEY_HEADER=X-API-Key

MCP_SERVERS=[
  {"name":"demo","command":"python","args":["backend/demo_mcp_server.py"]},
  {"name":"ranking","command":"python","args":["backend/ranking_mcp_bridge.py"]}
]
```

接入后会新增 `brand-ranking` skill 和以下 canonical MCP 工具：

- `ranking_query_understanding`
- `ranking_ugc_search_aiapi`
- `ranking_ugc_search_uiapi`
- `ranking_hotsell_recall`
- `ranking_bmc_detail_enrich`
- `ranking_sh_detail_enrich`
- `ranking_brand_normalize`

兼容期内，`ranking.*` 和旧的 `mcp__ranking__*` 名字仍可调用，但只作为 deprecated aliases，不再作为主文档和 skill 名字使用。

## 添加 Skill

在 `skills/` 目录创建新文件夹，添加 `SKILL.md`：

```markdown
---
name: my-skill
description: 这是我的自定义技能
---

# 技能内容...
```
