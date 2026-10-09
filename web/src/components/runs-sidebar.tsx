"use client";

// Every run, newest first (each is a folder under ~/.veil/runs with its transcript, screenshots and recording).
import { useEffect, useState } from "react";
import { Film, PanelLeftClose, PanelLeftOpen, Plus, Scale, UserPlus, VenetianMask } from "lucide-react";
import { api } from "@/lib/api";
import type { RunSummary } from "@/lib/types";

function dayLabel(ts: number | null): string {
  if (!ts) return "Earlier";
  const d = new Date(ts * 1000);
  const today = new Date();
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(today) - start(d)) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "This week";
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function timeLabel(ts: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "";
}

function dot(run: RunSummary, live: boolean): string {
  if (live) return "bg-green-400 animate-pulse";
  if (run.state === "stopped") return "bg-zinc-500";
  if (run.state === "failed" || run.ok === false) return "bg-red-400";
  if (run.ok) return "bg-green-500";
  return "bg-zinc-600";
}

interface Props {
  selectedId: string | null; // a past run being viewed; null = the live chat
  liveRunId: string | null;
  busy: boolean;
  refreshKey: string;
  onSelect: (id: string | null) => void;
  onNew: () => void;
}

export function RunsSidebar({ selectedId, liveRunId, busy, refreshKey, onSelect, onNew }: Props) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem("veil-sidebar") === "collapsed");
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .runs()
      .then((r) => !cancelled && setRuns(r.runs))
      .catch(() => !cancelled && setRuns((prev) => prev ?? []));
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  const setCollapsedSaved = (v: boolean) => {
    setCollapsed(v);
    try {
      localStorage.setItem("veil-sidebar", v ? "collapsed" : "open");
    } catch {
      /* ignore */
    }
  };

  if (collapsed) {
    return (
      <aside className="hidden md:flex flex-col items-center w-12 shrink-0 border-r border-zinc-800 bg-zinc-950 py-2 gap-2">
        <button onClick={() => setCollapsedSaved(false)} className="w-8 h-8 rounded-md text-zinc-400 hover:bg-zinc-800 flex items-center justify-center" title="Show runs">
          <PanelLeftOpen className="w-4 h-4" />
        </button>
        <button onClick={onNew} className="w-8 h-8 rounded-md text-zinc-400 hover:bg-zinc-800 flex items-center justify-center" title="New task">
          <Plus className="w-4 h-4" />
        </button>
      </aside>
    );
  }

  const groups: [string, RunSummary[]][] = [];
  for (const run of runs ?? []) {
    const label = dayLabel(run.started);
    const last = groups[groups.length - 1];
    if (last && last[0] === label) last[1].push(run);
    else groups.push([label, [run]]);
  }

  return (
    <aside className="hidden md:flex flex-col w-64 shrink-0 border-r border-zinc-800 bg-zinc-950 h-screen">
      <div className="h-11 flex items-center px-3 gap-2 border-b border-zinc-800">
        <VenetianMask className="w-4 h-4 text-zinc-400" />
        <span className="text-[13px] font-medium text-zinc-300">veil</span>
        <button onClick={() => setCollapsedSaved(true)} className="ml-auto w-7 h-7 rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300 flex items-center justify-center" title="Hide runs">
          <PanelLeftClose className="w-4 h-4" />
        </button>
      </div>
      <div className="p-2">
        <button
          onClick={onNew}
          className="w-full h-8 rounded-lg border border-zinc-800 text-[13px] text-zinc-300 hover:bg-zinc-900 inline-flex items-center justify-center gap-1.5"
        >
          <Plus className="w-4 h-4" /> New task
        </button>
      </div>
      <nav className="flex-1 overflow-y-auto px-2 pb-3" aria-label="Runs">
        {runs === null && <p className="px-2 py-1 text-[12px] text-zinc-600">Loading runs…</p>}
        {runs?.length === 0 && <p className="px-2 py-1 text-[12px] text-zinc-600">No runs yet. They&apos;ll show up here.</p>}
        {groups.map(([label, items]) => (
          <div key={label} className="mt-2">
            <p className="px-2 py-1 text-[11px] uppercase tracking-wider text-zinc-600">{label}</p>
            {items.map((run) => {
              const live = busy && run.id === liveRunId;
              const active = selectedId ? selectedId === run.id : run.id === liveRunId;
              return (
                <button
                  key={run.id}
                  onClick={() => onSelect(run.id === liveRunId ? null : run.id)}
                  className={`w-full text-left rounded-lg px-2 py-1.5 flex items-start gap-2 group ${active ? "bg-zinc-800/80" : "hover:bg-zinc-900"}`}
                  title={run.title}
                >
                  <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${dot(run, live)}`} />
                  <span className="min-w-0 flex-1">
                    <span className={`block truncate text-[13px] ${active ? "text-zinc-100" : "text-zinc-300"}`}>
                      {run.title.split("\n")[0] || "(untitled)"}
                    </span>
                    <span className="flex items-center gap-1.5 text-[11px] text-zinc-600">
                      {timeLabel(run.started)}
                      {live && <span className="text-green-400">running</span>}
                      {run.kind === "signup" && <UserPlus className="w-3 h-3" />}
                      {run.has_gif && <Film className="w-3 h-3" />}
                      {run.verdict && (
                        <span className="inline-flex items-center gap-0.5">
                          <Scale className="w-3 h-3" />
                          {run.verdict}
                        </span>
                      )}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        ))}
      </nav>
    </aside>
  );
}
