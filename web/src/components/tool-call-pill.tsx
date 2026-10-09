"use client";

// From chat-ui-example's tool-call-pill.tsx, with icons for browser-use's actions.
import { useState, type ReactNode } from "react";
import {
  ArrowDown, Camera, CheckCheck, Clock, Compass, FileText, Globe, Hand, Keyboard,
  Loader2, MousePointer2, PanelsTopLeft, ScanText, Search,
} from "lucide-react";
import type { UIToolCall } from "@/lib/types";

function getIcon(type: string): ReactNode {
  const cls = "w-2.5 h-2.5 text-zinc-500";
  switch (type) {
    case "browse":     return <Compass className={cls} />;
    case "tab":        return <PanelsTopLeft className={cls} />;
    case "click":      return <MousePointer2 className={cls} />;
    case "scroll":     return <ArrowDown className={cls} />;
    case "search":     return <Search className={cls} />;
    case "wait":       return <Clock className={cls} />;
    case "screenshot": return <Camera className={cls} />;
    case "extract":    return <ScanText className={cls} />;
    case "file":       return <FileText className={cls} />;
    case "keys":
    case "type":       return <Keyboard className={cls} />;
    case "done":       return <CheckCheck className={cls} />;
    case "human":      return <Hand className="w-2.5 h-2.5 text-amber-400" />;
    default:           return <Globe className={cls} />;
  }
}

export function ToolCallPill({ toolCall }: { toolCall: UIToolCall }) {
  const [expanded, setExpanded] = useState(false);
  const isRunning = toolCall.status === "running" || toolCall.status === "pending";
  const details = JSON.stringify(toolCall.args, null, 2);

  return (
    <div className="flex flex-col items-start my-1">
      <button
        onClick={() => setExpanded(!expanded)}
        className="inline-flex items-center gap-2 bg-zinc-800 pl-2 pr-3 py-1.5 rounded-md text-[13px] hover:bg-zinc-700/80 transition-colors max-w-full"
      >
        <div className="w-[18px] h-[18px] rounded-full border border-zinc-700 flex items-center justify-center shrink-0">
          {isRunning ? <Loader2 className="w-2.5 h-2.5 text-zinc-500 animate-spin" /> : getIcon(toolCall.type)}
        </div>
        <span className="text-zinc-200 font-medium shrink-0">{toolCall.displayName}</span>
        {toolCall.displayValue && (
          <span className="text-zinc-500 font-mono text-[12px] truncate max-w-[350px]">{toolCall.displayValue}</span>
        )}
      </button>

      {expanded && details !== "{}" && (
        <pre className="mt-1 ml-7 p-3 bg-zinc-900/80 border border-zinc-800 rounded-lg text-[12px] text-zinc-400 overflow-x-auto max-h-[200px] overflow-y-auto whitespace-pre-wrap max-w-full">
          {details.slice(0, 2000)}
        </pre>
      )}
    </div>
  );
}
