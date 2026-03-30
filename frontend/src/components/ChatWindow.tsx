"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { Send, Square, Bot, Zap, BookOpen, Settings } from "lucide-react";
import { MessageBubble, ChatMessage } from "./MessageBubble";
import { AgentEvent } from "./StepBlock";

const CONFIGURED_BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL?.trim() || "";
const BACKEND_CANDIDATES = [
  CONFIGURED_BACKEND_URL,
  "",
  "http://127.0.0.1:8012",
  "http://127.0.0.1:8015",
  "http://127.0.0.1:8000",
].filter((value, index, array) => value && array.indexOf(value) === index);

async function probeBackend(baseUrl: string): Promise<boolean> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 1500);
  try {
    const res = await fetch(`${baseUrl}/api/config`, {
      signal: controller.signal,
    });
    return res.ok;
  } catch {
    return false;
  } finally {
    window.clearTimeout(timeout);
  }
}

interface ConfigData {
  model: string;
  skills: { name: string; description: string }[];
  mcp_tools: { name: string; description: string }[];
}

export function ChatWindow() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [showConfig, setShowConfig] = useState(false);
  const [backendUrl, setBackendUrl] = useState(CONFIGURED_BACKEND_URL);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<(() => void) | null>(null);
  // 长对话记忆：后端返回的 history_update 存在这里，下次请求时作为 history 发送
  const conversationHistoryRef = useRef<{ role: string; content: string }[]>([]);

  // 加载配置
  useEffect(() => {
    let cancelled = false;

    const loadConfig = async () => {
      for (const candidate of BACKEND_CANDIDATES) {
        const isAvailable = await probeBackend(candidate);
        if (!isAvailable) continue;

        try {
          const res = await fetch(`${candidate}/api/config`);
          if (!res.ok) continue;
          const data = (await res.json()) as ConfigData;
          if (!cancelled) {
            setBackendUrl(candidate);
            setConfig(data);
          }
          return;
        } catch {
          // Continue probing other candidates.
        }
      }
    };

    void loadConfig();
    return () => {
      cancelled = true;
    };
  }, []);

  // 自动滚动到底部
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // 自动调整输入框高度
  useEffect(() => {
    const ta = inputRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [input]);

  const buildHistory = useCallback(() => {
    // 优先使用后端返回的 history（包含长对话记忆）
    return conversationHistoryRef.current;
  }, []);

  const sendMessage = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming) return;

    setInput("");
    setStreaming(true);

    // 添加用户消息
    const userMsgId = Date.now().toString();
    setMessages((prev) => [
      ...prev,
      { id: userMsgId, role: "user", content: text },
    ]);

    // 创建助手消息占位符
    const assistantMsgId = (Date.now() + 1).toString();
    setMessages((prev) => [
      ...prev,
      { id: assistantMsgId, role: "assistant", content: "", steps: [], streaming: true },
    ]);

    // 累积当前助手消息的状态
    let accContent = "";
    const accSteps: AgentEvent[] = [];

    const updateAssistantMsg = (patch: Partial<ChatMessage>) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsgId ? { ...m, ...patch } : m
        )
      );
    };

    try {
      const history = buildHistory();
      const res = await fetch(`${backendUrl}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history }),
        signal: undefined, // 使用 ReadableStream 手动管理
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // 存储取消函数
      abortRef.current = () => reader.cancel();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;

          let event: AgentEvent;
          try {
            event = JSON.parse(jsonStr);
          } catch {
            continue;
          }

          switch (event.type) {
            case "message_delta":
              accContent += event.content;
              updateAssistantMsg({ content: accContent, steps: [...accSteps] });
              break;

            case "message_done":
              accContent = event.content;
              updateAssistantMsg({ content: accContent, steps: [...accSteps], streaming: false });
              break;

            case "tool_call":
            case "tool_result":
            case "team_event":
            case "system_event":
            case "error":
              accSteps.push(event);
              updateAssistantMsg({ steps: [...accSteps] });
              break;

            case "done":
              updateAssistantMsg({ streaming: false, steps: [...accSteps] });
              break;

            case "history_update":
              // 后端返回完整 history，保存以供下次请求（长对话记忆）
              if (Array.isArray((event as { type: string; history?: unknown[] }).history)) {
                conversationHistoryRef.current = (event as { type: string; history: { role: string; content: string }[] }).history;
              }
              break;

            default:
              break;
          }
        }
      }
    } catch (err: unknown) {
      const errorMsg = err instanceof Error ? err.message : "未知错误";
      accSteps.push({ type: "error", message: errorMsg });
      updateAssistantMsg({ steps: [...accSteps], streaming: false });
    } finally {
      abortRef.current = null;
      setStreaming(false);
      inputRef.current?.focus();
    }
  }, [backendUrl, input, streaming, buildHistory]);

  const stopStreaming = useCallback(() => {
    abortRef.current?.();
    setStreaming(false);
    setMessages((prev) =>
      prev.map((m) =>
        m.streaming ? { ...m, streaming: false } : m
      )
    );
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const clearHistory = () => {
    setMessages([]);
  };

  return (
    <div className="flex flex-col h-screen bg-[#0f0f0f]">
      {/* 顶部导航栏 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#2e2e2e] bg-[#0f0f0f] flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center">
            <Bot className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-gray-100">Easy Claude</h1>
            <p className="text-xs text-gray-500">MCP + Skill Agent</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* 模型 */}
          {config && (
            <span className="text-xs px-2 py-1 bg-[#1a1a1a] border border-[#2e2e2e] rounded text-gray-400 font-mono">
              {config.model}
            </span>
          )}

          {/* 配置面板按钮 */}
          <button
            onClick={() => setShowConfig((p) => !p)}
            className="p-1.5 rounded-lg text-gray-500 hover:text-gray-300 hover:bg-[#1a1a1a] transition-colors"
            title="查看配置"
          >
            <Settings className="w-4 h-4" />
          </button>

          {/* 清空历史 */}
          {messages.length > 0 && (
            <button
              onClick={clearHistory}
              className="text-xs text-gray-500 hover:text-gray-300 px-2 py-1 rounded hover:bg-[#1a1a1a] transition-colors"
            >
              清空
            </button>
          )}
        </div>
      </div>

      {/* 配置面板（折叠）*/}
      {showConfig && config && (
        <div className="border-b border-[#2e2e2e] bg-[#111] px-4 py-3 flex gap-6 text-xs flex-shrink-0 overflow-x-auto">
          {config.skills.length > 0 && (
            <div className="flex-shrink-0">
              <div className="flex items-center gap-1 text-purple-400 font-medium mb-1.5">
                <BookOpen className="w-3 h-3" />
                Skills ({config.skills.length})
              </div>
              <div className="flex flex-wrap gap-1">
                {config.skills.map((s) => (
                  <span key={s.name} className="px-2 py-0.5 bg-purple-900/30 text-purple-300 border border-purple-700/40 rounded font-mono" title={s.description}>
                    {s.name}
                  </span>
                ))}
              </div>
            </div>
          )}
          {config.mcp_tools.length > 0 && (
            <div className="flex-shrink-0">
              <div className="flex items-center gap-1 text-blue-400 font-medium mb-1.5">
                <Zap className="w-3 h-3" />
                MCP 工具 ({config.mcp_tools.length})
              </div>
              <div className="flex flex-wrap gap-1">
                {config.mcp_tools.map((t) => (
                  <span key={t.name} className="px-2 py-0.5 bg-blue-900/30 text-blue-300 border border-blue-700/40 rounded font-mono" title={t.description}>
                    {t.name}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* 消息列表 */}
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center">
            <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-blue-500/20 to-purple-600/20 border border-blue-500/20 flex items-center justify-center mb-4">
              <Bot className="w-8 h-8 text-blue-400" />
            </div>
            <h2 className="text-lg font-semibold text-gray-200 mb-1">Easy Claude Agent</h2>
            <p className="text-sm text-gray-500 max-w-sm">
              支持 MCP 工具调用、Skill 技能加载、多智能体团队协作。所有中间步骤实时可见。
            </p>
            {config && (
              <div className="mt-4 flex flex-wrap justify-center gap-2 max-w-md">
                {config.skills.map((s) => (
                  <button
                    key={s.name}
                    onClick={() => setInput(`使用 ${s.name} 技能`)}
                    className="text-xs px-2.5 py-1 bg-purple-900/30 text-purple-300 border border-purple-700/40 rounded-full hover:bg-purple-900/50 transition-colors"
                  >
                    📚 {s.name}
                  </button>
                ))}
                {config.mcp_tools.map((t) => (
                  <button
                    key={t.name}
                    onClick={() => setInput(`调用 ${t.name} 工具`)}
                    className="text-xs px-2.5 py-1 bg-blue-900/30 text-blue-300 border border-blue-700/40 rounded-full hover:bg-blue-900/50 transition-colors"
                  >
                    ⚡ {t.name}
                  </button>
                ))}
              </div>
            )}
            <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-2 max-w-md text-left">
              {[
                "计算 (2 + 3) * 7 并告诉我当前时间",
                "使用 code-review 技能分析一段代码",
                "生成一个叫 alice 的队友，角色是数据分析师",
                "创建一个任务：编写测试用例",
              ].map((prompt) => (
                <button
                  key={prompt}
                  onClick={() => setInput(prompt)}
                  className="text-xs text-left px-3 py-2 bg-[#1a1a1a] border border-[#2e2e2e] rounded-lg text-gray-400 hover:text-gray-200 hover:border-[#3e3e3e] transition-colors"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto">
            {messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} />
            ))}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* 输入区域 */}
      <div className="flex-shrink-0 border-t border-[#2e2e2e] bg-[#0f0f0f] px-4 py-3">
        <div className="max-w-3xl mx-auto">
          <div className="flex items-end gap-2 bg-[#1a1a1a] border border-[#2e2e2e] rounded-2xl px-4 py-3 focus-within:border-blue-500/50 transition-colors">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="发送消息… (Enter 发送，Shift+Enter 换行)"
              rows={1}
              disabled={streaming}
              className="flex-1 bg-transparent text-sm text-gray-200 placeholder-gray-600 resize-none outline-none min-h-[24px] max-h-[200px] leading-6 disabled:opacity-60"
            />
            {streaming ? (
              <button
                onClick={stopStreaming}
                className="flex-shrink-0 p-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white transition-colors"
                title="停止"
              >
                <Square className="w-4 h-4" />
              </button>
            ) : (
              <button
                onClick={sendMessage}
                disabled={!input.trim()}
                className="flex-shrink-0 p-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                title="发送 (Enter)"
              >
                <Send className="w-4 h-4" />
              </button>
            )}
          </div>
          <p className="text-center text-xs text-gray-600 mt-2">
            步骤卡片可点击展开 · 支持 MCP / Skill / 团队协议
          </p>
        </div>
      </div>
    </div>
  );
}
