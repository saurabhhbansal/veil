"use client";

// Adapted from chat-ui-example's chat-messages.tsx: a turn is the task, then what happened in order
// (identities tried, steps, blocks, human handoffs) and the result.
import { useState } from "react";
import { AlertTriangle, CircleX, CornerDownRight, Info, OctagonX, ShieldAlert } from "lucide-react";
import type { ConversationTurn, EventOf, RunEvent, RunStatus, Snapshot } from "@/lib/types";
import { HumanLine, HumanPrompt } from "./human-card";
import { IdentityLine } from "./identity-card";
import { Markdown } from "./markdown";
import { ReviewCard } from "./review-card";
import { StepSection } from "./step-section";
import { ThinkingIndicator } from "./thinking-indicator";

const WAITING: Partial<Record<RunStatus, string>> = {
  queued: "Waiting for a free browser (sessions run a few at a time)…",
  starting: "Launching identity: proxy → geo → device → Fortress…",
  running: "Thinking…",
  stopping: "Stopping after the current step…",
  reviewing: "Reviewing the run…",
};

function Notice({ event }: { event: EventOf<"notice"> }) {
  const Icon = event.level === "blocked" ? ShieldAlert : event.level === "error" ? CircleX : event.level === "info" ? Info : AlertTriangle;
  const color = event.level === "blocked" ? "text-orange-400" : event.level === "error" ? "text-red-400" : "text-zinc-400";
  return (
    <div className="flex items-start gap-2 text-[13px] py-1">
      <Icon className={`w-4 h-4 shrink-0 mt-0.5 ${color}`} />
      <span className="text-zinc-300 break-words min-w-0">{event.text}</span>
    </div>
  );
}

function Result({ event }: { event: EventOf<"result"> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-2">
      {event.stopped ? (
        <div className="flex items-center gap-2 text-[14px] text-zinc-300">
          <OctagonX className="w-4 h-4 text-zinc-500" /> Stopped before the agent finished.
        </div>
      ) : (
        <>
          {!event.ok && <p className="text-[13px] text-red-400 font-medium">Did not finish</p>}
          <div className="text-[15px] text-zinc-200 leading-relaxed">
            <Markdown>{event.output || "_(no output)_"}</Markdown>
          </div>
        </>
      )}
      {event.attempts.length > 0 && (
        <div>
          <button onClick={() => setOpen(!open)} className="text-[12px] text-zinc-500 hover:text-zinc-300">
            {open ? "Hide" : "Show"} {event.attempts.length} attempt{event.attempts.length > 1 ? "s" : ""}
          </button>
          {open && (
            <ol className="mt-1 space-y-1 text-[12px] text-zinc-400 list-decimal ml-5">
              {event.attempts.map((a, i) => (
                <li key={i}>
                  <span className="text-zinc-200">{a.outcome}</span> · {a.proxy || "direct"} → {a.exit_ip || "?"} · {a.persona || "no device"}
                  {a.detail && <div className="font-mono text-zinc-500 break-words">{a.detail.slice(0, 300)}</div>}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

function Item({ ev, inProgress }: { ev: RunEvent; inProgress: boolean }) {
  switch (ev.type) {
    case "attempt":
      return <IdentityLine identity={ev.identity} />;
    case "step":
      return <StepSection step={ev} inProgress={inProgress} />;
    case "notice":
      return <Notice event={ev} />;
    case "human":
      return ev.resolved ? <HumanLine event={ev} /> : null; // the open request is drawn below, with its button
    case "result":
      return <Result event={ev} />;
    case "steer":
      return (
        <div className="flex justify-end">
          <div className="max-w-[480px] rounded-2xl border border-zinc-700 px-3.5 py-2 text-[14px] text-zinc-200">
            <div className="flex items-center gap-1.5 text-[11px] text-zinc-500 mb-0.5">
              <CornerDownRight className="w-3 h-3" />
              {ev.followup
                ? "Steer · the agent carried on with it"
                : ev.delivered
                  ? "Steer · read by the agent"
                  : ev.undelivered
                    ? "Steer · the run ended first"
                    : "Steer · at the agent's next step"}
            </div>
            <span className="whitespace-pre-wrap break-words">{ev.text}</span>
          </div>
        </div>
      );
    case "judgement":
      return <ReviewCard judgement={ev.judgement} file={ev.file} />;
    default:
      return null;
  }
}

export function ChatMessages({ turns, snap, onHandBack }: { turns: ConversationTurn[]; snap: Snapshot; onHandBack: (note: string) => void }) {
  return (
    <div className="space-y-8">
      {turns.map((turn, ti) => {
        const isLast = ti === turns.length - 1;
        const live = isLast && !turn.isComplete && snap.busy;
        const lastItem = turn.items[turn.items.length - 1];
        // browser-use reports a step when the model has decided, before its actions run.
        const stepRunning = live && snap.status === "running" && lastItem?.type === "step";
        const waiting = live && !stepRunning && snap.status !== "needs_human" ? WAITING[snap.status] : undefined;
        return (
          <div key={turn.id} className="space-y-3">
            <div className="flex justify-end">
              <div className="bg-zinc-800 rounded-2xl px-4 py-2.5 max-w-[480px] text-[15px] text-zinc-100 whitespace-pre-wrap break-words">
                {turn.task.text}
              </div>
            </div>
            {turn.items.map((ev) => (
              <Item key={ev.id} ev={ev} inProgress={stepRunning && ev === lastItem} />
            ))}
            {live && snap.status === "paused" && <p className="text-[13px] text-zinc-500">⏸ Paused. The browser stays open.</p>}
            {live && snap.human && <HumanPrompt human={snap.human} onContinue={onHandBack} />}
            {waiting && <ThinkingIndicator label={waiting} />}
          </div>
        );
      })}
    </div>
  );
}
