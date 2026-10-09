"use client";

// A past run, read-only: its transcript on the left; recording, review and identity on the right.
import { useEffect, useState } from "react";
import { ArrowLeft, Download, FileJson, Loader2, Scale } from "lucide-react";
import { useSettings } from "@/context/settings-context";
import { api, apiUrl } from "@/lib/api";
import { groupIntoTurns } from "@/lib/turns";
import type { RunDetail, Snapshot } from "@/lib/types";
import { ChatMessages } from "./chat-messages";
import { IdentityDetails } from "./identity-card";
import { ReviewCard } from "./review-card";

const STATE_LABEL: Record<string, string> = { done: "Finished", stopped: "Stopped", failed: "Failed", running: "Was running", unknown: "Unknown" };

export function RunView({ runId, liveBusy, onBack }: { runId: string; liveBusy: boolean; onBack: () => void }) {
  const { uiValues } = useSettings();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setRun(null);
    setError(null);
    api
      .runDetail(runId)
      .then((r) => !cancelled && setRun(r))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [runId]);

  async function review() {
    setReviewing(true);
    setError(null);
    try {
      setRun(await api.reviewRun(runId, uiValues()));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setReviewing(false);
    }
  }

  const snap: Snapshot = {
    session: "", group: null, label: "", sessions: [],
    version: 0, status: "idle", busy: false, events: run?.events ?? [], identity: run?.identity ?? null, human: null,
    browser_open: false, gif: null, result_file: null, run_id: runId, has_review: false,
  };
  const started = run?.started ? new Date(run.started * 1000).toLocaleString() : "";
  const hasReviewEvent = run?.events.some((e) => e.type === "judgement");

  return (
    <div className="flex h-screen w-full overflow-hidden">
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-11 border-b border-zinc-800 flex items-center px-4 gap-3">
          <button onClick={onBack} className="h-7 px-2 rounded-md text-[12px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 inline-flex items-center gap-1">
            <ArrowLeft className="w-3.5 h-3.5" /> {liveBusy ? "Back to the live run" : "Back"}
          </button>
          <span className="text-[12px] text-zinc-500 truncate">
            {run ? `${STATE_LABEL[run.state ?? "unknown"] ?? run.state} · ${started}` : ""}
          </span>
        </header>
        <div className="flex-1 overflow-y-auto px-6 py-6">
          <div className="max-w-2xl mx-auto">
            {!run && !error && (
              <div className="flex items-center gap-2 text-sm text-zinc-500">
                <Loader2 className="w-4 h-4 animate-spin" /> Loading run…
              </div>
            )}
            {error && <p className="text-sm text-red-400 mb-4 break-words">{error}</p>}
            {run && (run.events.length ? <ChatMessages turns={groupIntoTurns(run.events)} snap={snap} onHandBack={() => {}} /> : <p className="text-sm text-zinc-500">This run has no transcript.</p>)}
          </div>
        </div>
      </div>

      <aside className="hidden lg:flex flex-col w-[45%] shrink-0 border-l border-zinc-800 bg-zinc-950 overflow-y-auto">
        <div className="h-11 border-b border-zinc-800 flex items-center px-4">
          <span className="text-[13px] font-medium text-zinc-300">Recording</span>
          <div className="ml-auto flex items-center gap-3">
            {run?.gif && (
              <a href={apiUrl(run.gif)} download className="inline-flex items-center gap-1 text-[12px] text-blue-400 hover:underline">
                <Download className="w-3.5 h-3.5" /> GIF
              </a>
            )}
            {run?.result_file && (
              <a href={apiUrl(run.result_file)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[12px] text-blue-400 hover:underline">
                <FileJson className="w-3.5 h-3.5" /> result.json
              </a>
            )}
          </div>
        </div>
        <div className="p-4 space-y-4">
          {run?.gif ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={apiUrl(run.gif)} alt="Recording of the run" className="w-full rounded-lg ring-1 ring-zinc-800 bg-zinc-900" />
          ) : (
            <p className="text-[13px] text-zinc-500">{run ? "No recording for this run (it ended before the agent took a step)." : ""}</p>
          )}

          {run?.judgement && !hasReviewEvent && <ReviewCard judgement={run.judgement} file={run.judgement_file} />}
          {run && !run.judgement && run.events.some((e) => e.type === "step") && (
            <button
              onClick={review}
              disabled={reviewing}
              className="w-full rounded-xl border border-zinc-800 py-2.5 text-[13px] text-zinc-300 hover:bg-zinc-900 inline-flex items-center justify-center gap-1.5 disabled:opacity-50"
            >
              {reviewing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Scale className="w-4 h-4" />}
              {reviewing ? "Reviewing…" : "Review this run"}
            </button>
          )}

          {run?.identity && (
            <div className="rounded-xl border border-zinc-800 p-3">
              <p className="text-[11px] uppercase tracking-wider text-zinc-500 mb-2">Identity used</p>
              <IdentityDetails identity={run.identity} />
            </div>
          )}
          <p className="text-[11px] text-zinc-600 break-all">Saved in ~/.veil/runs/{runId}</p>
        </div>
      </aside>
    </div>
  );
}
