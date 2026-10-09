"use client";

import { Scale } from "lucide-react";
import { apiUrl } from "@/lib/api";
import type { Judgement } from "@/lib/types";

const VERDICT: Record<Judgement["verdict"], string> = {
  success: "bg-green-500/15 text-green-400",
  partial: "bg-amber-500/15 text-amber-400",
  failure: "bg-red-500/15 text-red-400",
};

/** The run review: verdict, what went wrong, fixes, and what the next run is told. */
export function ReviewCard({ judgement, file, compact }: { judgement: Judgement; file?: string | null; compact?: boolean }) {
  const j = judgement;
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4 space-y-3 text-[13px]">
      <div className="flex items-center gap-2">
        <Scale className="w-4 h-4 text-zinc-400" />
        <span className="text-zinc-200 font-medium">Run review</span>
        <span className={`text-[11px] px-1.5 py-0.5 rounded ${VERDICT[j.verdict] ?? VERDICT.partial}`}>{j.verdict}</span>
        {file && (
          <a href={apiUrl(file)} target="_blank" rel="noopener noreferrer" className="ml-auto text-[12px] text-blue-400 hover:underline">
            judgement.md
          </a>
        )}
      </div>
      <p className="text-zinc-300 leading-relaxed">{j.summary}</p>
      {j.what_went_wrong.length > 0 && (
        <div>
          <p className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">What went wrong</p>
          <ul className="list-disc ml-5 space-y-1 text-zinc-300">
            {j.what_went_wrong.map((x, i) => (
              <li key={i}>{x}</li>
            ))}
          </ul>
        </div>
      )}
      {j.fixes.length > 0 && (
        <div>
          <p className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">Possible fixes</p>
          <ul className="list-disc ml-5 space-y-1 text-zinc-300">
            {j.fixes.map((x, i) => (
              <li key={i}>{x}</li>
            ))}
          </ul>
        </div>
      )}
      {!compact && j.advice_for_next_run && (
        <div className="border-t border-zinc-800 pt-2.5">
          <p className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">Passed to the next run (when Review is on)</p>
          <p className="text-zinc-400 italic">{j.advice_for_next_run}</p>
        </div>
      )}
    </div>
  );
}
