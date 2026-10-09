"use client";

// From chat-ui-example's step-section.tsx: one browser-use step (its goal, the actions it took,
// and on expand its self-evaluation, memory and screenshot).
import { useState } from "react";
import { Check, ChevronUp, ImageIcon } from "lucide-react";
import { apiUrl } from "@/lib/api";
import { toToolCall } from "@/lib/tool-labels";
import type { EventOf } from "@/lib/types";
import { ToolCallPill } from "./tool-call-pill";
import { ThinkingIndicator } from "./thinking-indicator";

export function StepSection({ step, inProgress }: { step: EventOf<"step">; inProgress: boolean }) {
  const [expanded, setExpanded] = useState(true);
  const [showDetails, setShowDetails] = useState(false);
  const title = step.next_goal || `Step ${step.n}`;
  const status = inProgress ? "running" : "completed";

  return (
    <div className="mb-2">
      <button onClick={() => setExpanded(!expanded)} className="flex items-center gap-2 w-full text-left py-1.5 group">
        {inProgress ? (
          <ThinkingIndicator label={title} />
        ) : (
          <>
            <div className="w-[18px] h-[18px] rounded-full bg-green-500/20 flex items-center justify-center shrink-0">
              <Check className="w-3 h-3 text-green-500" strokeWidth={2.5} />
            </div>
            <span className="text-[14px] text-zinc-200 font-medium">{title}</span>
          </>
        )}
        <span className="text-[11px] text-zinc-600 ml-auto shrink-0">step {step.n}</span>
        <ChevronUp className={`w-4 h-4 text-zinc-600 group-hover:text-zinc-400 transition-transform ${expanded ? "rotate-0" : "rotate-180"}`} />
      </button>

      {expanded && (
        <div className="pl-[26px] mt-1 space-y-1">
          {step.actions.map((a, i) => (
            <ToolCallPill key={i} toolCall={toToolCall(a, `${step.id}-${i}`, status)} />
          ))}
          {(step.evaluation || step.memory || step.screenshot) && (
            <button onClick={() => setShowDetails(!showDetails)} className="text-[12px] text-zinc-500 hover:text-zinc-300 pt-1">
              {showDetails ? "Hide details" : "Details"}
            </button>
          )}
          {showDetails && (
            <div className="text-[13px] text-zinc-400 space-y-1.5 border-l border-zinc-800 pl-3 py-1">
              {step.url && <p className="font-mono text-[12px] text-zinc-500 truncate">{step.url}</p>}
              {step.evaluation && (
                <p>
                  <span className="text-zinc-500">Last step: </span>
                  {step.evaluation}
                </p>
              )}
              {step.memory && (
                <p>
                  <span className="text-zinc-500">Memory: </span>
                  {step.memory}
                </p>
              )}
              {step.screenshot && (
                <a href={apiUrl(step.screenshot)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 text-blue-400 hover:underline">
                  <ImageIcon className="w-3.5 h-3.5" /> Screenshot the agent saw
                </a>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
