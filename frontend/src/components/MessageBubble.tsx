"use client";

import { AgentEvent, ToolCallBlock, TeamEventBlock, SystemEventBlock, ErrorBlock } from "./StepBlock";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** Agent 在生成此回复时产生的中间步骤事件列表 */
  steps?: AgentEvent[];
  streaming?: boolean;
}

interface MessageBubbleProps {
  message: ChatMessage;
}

function renderMarkdown(text: string): React.ReactNode {
  // 简单的 markdown 渲染（代码块 + 换行）
  const parts = text.split(/(```[\s\S]*?```|`[^`]+`)/g);
  return parts.map((part, i) => {
    if (part.startsWith("```")) {
      const newline = part.indexOf("\n");
      const lang = part.slice(3, newline).trim();
      const code = part.slice(newline + 1, -3).trim();
      return (
        <pre key={i} className="my-2">
          {lang && <div className="text-xs text-gray-500 mb-1 font-mono">{lang}</div>}
          <code>{code}</code>
        </pre>
      );
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return <code key={i} className="bg-gray-800 px-1 py-0.5 rounded text-sm font-mono">{part.slice(1, -1)}</code>;
    }
    // 处理换行
    return (
      <span key={i}>
        {part.split("\n").map((line, j) => (
          <span key={j}>
            {line}
            {j < part.split("\n").length - 1 && <br />}
          </span>
        ))}
      </span>
    );
  });
}

function StepsList({ steps }: { steps: AgentEvent[] }) {
  // 构建 tool_call → tool_result 配对映射
  const resultMap = new Map<string, AgentEvent & { type: "tool_result" }>();
  for (const ev of steps) {
    if (ev.type === "tool_result") {
      resultMap.set(ev.id, ev);
    }
  }

  // 渲染步骤（去重：tool_result 单独处理，不重复渲染）
  const rendered: React.ReactNode[] = [];
  const seenResultIds = new Set<string>();

  for (let i = 0; i < steps.length; i++) {
    const ev = steps[i];

    if (ev.type === "tool_call") {
      const result = resultMap.get(ev.id);
      if (result) seenResultIds.add(ev.id);
      rendered.push(
        <ToolCallBlock
          key={`tc-${ev.id}`}
          event={ev}
          result={result}
          pending={!result}
        />
      );
    } else if (ev.type === "tool_result" && !seenResultIds.has(ev.id)) {
      // 孤立结果（未找到对应 call）
      rendered.push(
        <div key={`tr-${ev.id}`} className="text-xs text-gray-500 px-1">
          结果: {ev.content.slice(0, 100)}
        </div>
      );
    } else if (ev.type === "team_event") {
      rendered.push(<TeamEventBlock key={`te-${i}`} event={ev} />);
    } else if (ev.type === "system_event") {
      rendered.push(<SystemEventBlock key={`se-${i}`} event={ev} />);
    } else if (ev.type === "error") {
      rendered.push(<ErrorBlock key={`er-${i}`} event={ev} />);
    }
  }

  if (rendered.length === 0) return null;

  return (
    <div className="space-y-1.5 mb-3">
      {rendered}
    </div>
  );
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className="flex justify-end mb-4">
        <div className="max-w-[80%] bg-blue-600 text-white rounded-2xl rounded-br-sm px-4 py-2.5 text-sm leading-relaxed">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start mb-4">
      <div className="max-w-[88%] space-y-0">
        {/* 助手标签 */}
        <div className="flex items-center gap-1.5 mb-2 px-1">
          <div className="w-5 h-5 rounded-full bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center flex-shrink-0">
            <span className="text-[10px] text-white font-bold">A</span>
          </div>
          <span className="text-xs text-gray-500 font-medium">Agent</span>
          {message.streaming && (
            <span className="text-xs text-gray-600">• 思考中...</span>
          )}
        </div>

        {/* 步骤块（中间过程）*/}
        {message.steps && message.steps.length > 0 && (
          <StepsList steps={message.steps} />
        )}

        {/* 最终文本内容 */}
        {message.content && (
          <div className="bg-[#1a1a1a] border border-[#2e2e2e] rounded-2xl rounded-tl-sm px-4 py-3 text-sm leading-relaxed text-gray-200">
            {renderMarkdown(message.content)}
            {message.streaming && <span className="cursor-blink ml-0.5" />}
          </div>
        )}

        {/* 仅有步骤、没有文本时的等待状态 */}
        {!message.content && message.streaming && (
          <div className="bg-[#1a1a1a] border border-[#2e2e2e] rounded-2xl rounded-tl-sm px-4 py-3 flex items-center gap-2">
            <div className="flex gap-1">
              <span className="w-1.5 h-1.5 bg-gray-500 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
              <span className="w-1.5 h-1.5 bg-gray-500 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
              <span className="w-1.5 h-1.5 bg-gray-500 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
