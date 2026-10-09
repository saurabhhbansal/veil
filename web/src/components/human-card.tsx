"use client";

import { useState } from "react";
import { Hand, Play } from "lucide-react";
import type { EventOf, HumanRequest } from "@/lib/types";

const SOURCE_TEXT: Record<string, string> = {
  detector: "veil spotted a check only a person can pass.",
  agent: "The agent asked for your help.",
  user: "You have the browser. The agent is paused.",
};

/** The open request: what to do, and the button that hands the browser back to the agent. */
export function HumanPrompt({ human, onContinue, compact }: { human: HumanRequest; onContinue: (note: string) => void; compact?: boolean }) {
  const [note, setNote] = useState("");
  const isUser = human.source === "user";
  return (
    <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 space-y-2" role="alert">
      <div className="flex items-start gap-2">
        <Hand className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
        <div className="text-[13px] leading-snug">
          <p className="text-amber-200 font-medium">{isUser ? "You're in control" : `Human needed: ${human.reason}`}</p>
          <p className="text-amber-200/70">
            {SOURCE_TEXT[human.source]} {isUser
              ? "Click and type in the browser view."
              : "Do it in the browser view, or type the code here and the agent enters it. Then continue."}
          </p>
        </div>
      </div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          onContinue(note);
          setNote("");
        }}
      >
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={compact ? "Code or note (optional)" : "Note for the agent, or the code to type (optional)"}
          className="flex-1 min-w-0 bg-zinc-900/80 border border-zinc-700 rounded-lg px-2.5 py-1.5 text-[13px] text-zinc-200 outline-none focus:border-zinc-500 placeholder-zinc-500"
        />
        <button
          type="submit"
          className={`shrink-0 inline-flex items-center gap-1.5 rounded-lg bg-amber-400 hover:bg-amber-300 text-zinc-950 text-[13px] font-semibold px-3 py-1.5 transition-colors`}
        >
          <Play className="w-3.5 h-3.5" fill="currentColor" />
          {isUser ? "Hand back to agent" : "Done, continue"}
        </button>
      </form>
    </div>
  );
}

/** A request in the transcript once it's settled. */
export function HumanLine({ event }: { event: EventOf<"human"> }) {
  const what = event.source === "user" ? "You took over the browser" : `Human needed: ${event.reason}`;
  const how = event.outcome === "handed back" ? "handed back" : event.outcome || "";
  return (
    <div className="flex items-start gap-2 text-[13px] text-zinc-400 py-1">
      <Hand className="w-4 h-4 text-amber-400/70 shrink-0 mt-0.5" />
      <span>
        <span className="text-zinc-300">{what}</span>
        {how && <> · {how}</>}
        {event.note && <span className="text-zinc-500"> · “{event.note}”</span>}
      </span>
    </div>
  );
}
