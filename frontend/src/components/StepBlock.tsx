"use client";

import { useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Wrench,
  BookOpen,
  Terminal,
  Users,
  CheckCircle,
  XCircle,
  Loader2,
  Zap,
} from "lucide-react";

export type AgentEvent =
  | { type: "agent_start" }
  | { type: "tool_call"; id: string; name: string; tool_type: "mcp" | "skill" | "builtin"; input: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; content: string; error: boolean }
  | { type: "team_event"; event: string; teammate?: string; role?: string; request_id?: string; plan?: string; task_id?: number; subject?: string; tool?: string; output_preview?: string; error?: string; approved?: boolean; feedback?: string }
  | { type: "system_event"; event: string; message: string }
  | { type: "message_delta"; content: string }
  | { type: "message_done"; content: string }
  | { type: "error"; message: string }
  | { type: "done" };

interface StepBlockProps {
  event: AgentEvent;
  /** 对应的结果（仅 tool_call 需要） */
  result?: AgentEvent & { type: "tool_result" };
  /** 是否正在等待结果 */
  pending?: boolean;
}

const TOOL_TYPE_CONFIG = {
  mcp: { label: "MCP", bg: "bg-blue-900/40", text: "text-blue-300", border: "border-blue-700/50", icon: Zap },
  skill: { label: "Skill", bg: "bg-purple-900/40", text: "text-purple-300", border: "border-purple-700/50", icon: BookOpen },
  builtin: { label: "内置", bg: "bg-gray-800/60", text: "text-gray-400", border: "border-gray-600/50", icon: Terminal },
};

function JsonDisplay({ data }: { data: unknown }) {
  const str = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  const lines = str.split("\n");
  const preview = lines.slice(0, 8).join("\n");
  const hasMore = lines.length > 8;
  const [expanded, setExpanded] = useState(false);

  return (
    <pre className="text-xs overflow-x-auto whitespace-pre-wrap break-all text-gray-300 leading-relaxed">
      {expanded ? str : preview}
      {hasMore && !expanded && (
        <span>
          {"\n"}
          <button
            onClick={() => setExpanded(true)}
            className="text-blue-400 hover:text-blue-300 underline"
          >
            显示更多 ({lines.length - 8} 行)...
          </button>
        </span>
      )}
    </pre>
  );
}

export function ToolCallBlock({ event, result, pending }: StepBlockProps & { event: AgentEvent & { type: "tool_call" } }) {
  const [open, setOpen] = useState(false);
  const cfg = TOOL_TYPE_CONFIG[event.tool_type];
  const Icon = cfg.icon;
  const hasResult = !!result;
  const isError = result?.error;

  return (
    <div className={`rounded-lg border text-sm ${cfg.border} ${cfg.bg} overflow-hidden`}>
      {/* 头部 */}
      <button
        onClick={() => setOpen((p) => !p)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-white/5 transition-colors"
      >
        {open ? (
          <ChevronDown className="w-3.5 h-3.5 text-gray-500 flex-shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 text-gray-500 flex-shrink-0" />
        )}
        <Icon className={`w-3.5 h-3.5 flex-shrink-0 ${cfg.text}`} />
        <span className={`font-mono font-semibold ${cfg.text}`}>{event.name}</span>
        <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${cfg.bg} ${cfg.text} border ${cfg.border}`}>
          {cfg.label}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {pending && !hasResult && (
            <Loader2 className="w-3.5 h-3.5 text-gray-400 animate-spin" />
          )}
          {hasResult && !isError && (
            <CheckCircle className="w-3.5 h-3.5 text-green-400" />
          )}
          {hasResult && isError && (
            <XCircle className="w-3.5 h-3.5 text-red-400" />
          )}
        </div>
      </button>

      {/* 展开内容 */}
      {open && (
        <div className="border-t border-white/5 px-3 py-2 space-y-2">
          {/* 输入 */}
          {Object.keys(event.input).length > 0 && (
            <div>
              <div className="text-xs text-gray-500 mb-1 font-medium">输入</div>
              <JsonDisplay data={event.input} />
            </div>
          )}

          {/* 结果 */}
          {result && (
            <div>
              <div className={`text-xs mb-1 font-medium ${isError ? "text-red-400" : "text-gray-500"}`}>
                {isError ? "错误" : "输出"}
              </div>
              <JsonDisplay data={result.content} />
            </div>
          )}

          {/* 等待中 */}
          {pending && !hasResult && (
            <div className="text-xs text-gray-500 flex items-center gap-1">
              <Loader2 className="w-3 h-3 animate-spin" />
              执行中...
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function TeamEventBlock({ event }: { event: AgentEvent & { type: "team_event" } }) {
  const [open, setOpen] = useState(false);

  const getLabel = () => {
    switch (event.event) {
      case "spawned": return `队友 ${event.teammate} 已生成（${event.role}）`;
      case "idle": return `${event.teammate} 进入空闲`;
      case "shutdown_request": return `请求关闭 ${event.teammate}`;
      case "shutdown_complete": return `${event.teammate} 已关闭`;
      case "shutdown_timeout": return `${event.teammate} 超时关闭`;
      case "plan_submitted": return `${event.teammate} 提交计划等待审批`;
      case "plan_reviewed": return `计划${event.approved ? "已批准" : "已拒绝"}（${event.teammate}）`;
      case "task_claimed": return `${event.teammate} 认领任务 #${event.task_id}`;
      case "tool_call": return `${event.teammate}: 调用 ${event.tool}`;
      case "error": return `${event.teammate} 出错`;
      default: return `团队事件: ${event.event}`;
    }
  };

  const hasDetails = event.plan || event.feedback || event.output_preview || event.error;

  return (
    <div className="rounded-lg border border-amber-700/40 bg-amber-900/20 text-sm overflow-hidden">
      <button
        onClick={() => setOpen((p) => !p)}
        disabled={!hasDetails}
        className={`w-full flex items-center gap-2 px-3 py-2 text-left ${hasDetails ? "hover:bg-white/5" : ""} transition-colors`}
      >
        {hasDetails ? (
          open ? <ChevronDown className="w-3.5 h-3.5 text-gray-500 flex-shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 text-gray-500 flex-shrink-0" />
        ) : (
          <span className="w-3.5" />
        )}
        <Users className="w-3.5 h-3.5 text-amber-400 flex-shrink-0" />
        <span className="text-amber-300 font-medium">{getLabel()}</span>
        {event.request_id && (
          <span className="text-xs text-gray-500 font-mono ml-auto">#{event.request_id}</span>
        )}
      </button>
      {open && hasDetails && (
        <div className="border-t border-white/5 px-3 py-2 text-xs text-gray-400 space-y-1">
          {event.plan && (
            <div>
              <span className="text-gray-500 font-medium">计划内容：</span>
              <pre className="whitespace-pre-wrap mt-1">{event.plan}</pre>
            </div>
          )}
          {event.feedback && (
            <div>
              <span className="text-gray-500 font-medium">反馈：</span> {event.feedback}
            </div>
          )}
          {event.output_preview && (
            <div>
              <span className="text-gray-500 font-medium">输出预览：</span> {event.output_preview}
            </div>
          )}
          {event.error && (
            <div className="text-red-400">
              <span className="font-medium">错误：</span> {event.error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function SystemEventBlock({ event }: { event: AgentEvent & { type: "system_event" } }) {
  return (
    <div className="flex items-center gap-2 text-xs text-gray-500 px-1">
      <Wrench className="w-3 h-3" />
      <span>{event.message}</span>
    </div>
  );
}

export function ErrorBlock({ event }: { event: AgentEvent & { type: "error" } }) {
  return (
    <div className="rounded-lg border border-red-700/50 bg-red-900/20 px-3 py-2 text-sm flex items-center gap-2">
      <XCircle className="w-4 h-4 text-red-400 flex-shrink-0" />
      <span className="text-red-300">{event.message}</span>
    </div>
  );
}
