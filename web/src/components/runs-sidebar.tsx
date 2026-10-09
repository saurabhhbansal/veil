"use client";

// Every run, newest first (each is a folder under ~/.veil/runs with its transcript, screenshots and recording).
// Each has a ⋯ menu: open, rename, download the recording, delete.
import { useEffect, useRef, useState } from "react";
import { Download, ExternalLink, Film, Layers, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Pencil, Plus, Scale, Trash2, UserPlus, VenetianMask } from "lucide-react";
import { api, apiUrl } from "@/lib/api";
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

function RunMenu({
  run,
  running,
  onOpen,
  onRename,
  onDelete,
}: {
  run: RunSummary;
  running: boolean;
  onOpen: () => void;
  onRename: () => void;
  onDelete: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (!open) setConfirming(false);
  }, [open]);

  const item = "w-full text-left px-2.5 py-1.5 text-[12px] rounded-md flex items-center gap-2 hover:bg-zinc-800 disabled:opacity-40 disabled:hover:bg-transparent";
  return (
    <div ref={ref} className="relative shrink-0">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(!open);
        }}
        className={`w-6 h-6 rounded-md flex items-center justify-center text-zinc-500 hover:text-zinc-200 hover:bg-zinc-700/60 ${open ? "opacity-100 bg-zinc-700/60" : "opacity-0 group-hover:opacity-100 focus:opacity-100"}`}
        aria-label={`Options for ${run.title.split("\n")[0]}`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <MoreHorizontal className="w-4 h-4" />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-7 z-30 w-44 rounded-lg border border-zinc-700 bg-zinc-900 shadow-xl p-1" onClick={(e) => e.stopPropagation()}>
          {confirming ? (
            <div className="p-1.5 space-y-2">
              <p className="text-[12px] text-zinc-300">Delete this run, its screenshots and recording? This can&apos;t be undone.</p>
              <div className="flex gap-1.5">
                <button
                  onClick={async () => {
                    setDeleting(true);
                    await onDelete();
                    setDeleting(false);
                    setOpen(false);
                  }}
                  disabled={deleting}
                  className="flex-1 rounded-md bg-red-600 hover:bg-red-500 text-white text-[12px] py-1 disabled:opacity-50"
                >
                  {deleting ? "Deleting…" : "Delete"}
                </button>
                <button onClick={() => setConfirming(false)} className="flex-1 rounded-md border border-zinc-700 text-zinc-300 text-[12px] py-1 hover:bg-zinc-800">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <>
              <button role="menuitem" className={`${item} text-zinc-300`} onClick={() => (setOpen(false), onOpen())}>
                <ExternalLink className="w-3.5 h-3.5" /> Open
              </button>
              <button role="menuitem" className={`${item} text-zinc-300`} onClick={() => (setOpen(false), onRename())}>
                <Pencil className="w-3.5 h-3.5" /> Rename
              </button>
              {run.gif && (
                <a role="menuitem" href={apiUrl(run.gif)} download={`veil-${run.id}.gif`} className={`${item} text-zinc-300`} onClick={() => setOpen(false)}>
                  <Download className="w-3.5 h-3.5" /> Download recording
                </a>
              )}
              <div className="my-1 border-t border-zinc-800" />
              <button
                role="menuitem"
                className={`${item} text-red-400`}
                onClick={() => setConfirming(true)}
                disabled={running}
                title={running ? "Stop the run first" : undefined}
              >
                <Trash2 className="w-3.5 h-3.5" /> Delete
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

interface Props {
  selectedId: string | null; // a past run being viewed; null = the live chat
  liveRunIds: string[]; // runs going right now (any session)
  currentRunId: string | null; // the run the live chat is showing
  refreshKey: string;
  onSelect: (id: string | null) => void;
  onNew: () => void;
  onDeleted: (id: string) => void;
}

export function RunsSidebar({ selectedId, liveRunIds, currentRunId, refreshKey, onSelect, onNew, onDeleted }: Props) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function rename(id: string) {
    const title = draft.trim();
    setRenaming(null);
    if (!title) return;
    setRuns((rs) => rs?.map((r) => (r.id === id ? { ...r, title } : r)) ?? rs);
    try {
      await api.renameRun(id, title);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function remove(id: string) {
    try {
      await api.deleteRun(id);
      setRuns((rs) => rs?.filter((r) => r.id !== id) ?? rs);
      onDeleted(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

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
        {error && (
          <p className="px-2 py-1 text-[12px] text-red-400 break-words">
            {error}{" "}
            <button onClick={() => setError(null)} className="underline text-zinc-500">
              dismiss
            </button>
          </p>
        )}
        {runs === null && <p className="px-2 py-1 text-[12px] text-zinc-600">Loading runs…</p>}
        {runs?.length === 0 && <p className="px-2 py-1 text-[12px] text-zinc-600">No runs yet. They&apos;ll show up here.</p>}
        {groups.map(([label, items]) => (
          <div key={label} className="mt-2">
            <p className="px-2 py-1 text-[11px] uppercase tracking-wider text-zinc-600">{label}</p>
            {items.map((run) => {
              const live = liveRunIds.includes(run.id);
              const active = selectedId ? selectedId === run.id : run.id === currentRunId;
              const open = () => onSelect(run.id === currentRunId ? null : run.id);
              return (
                <div
                  key={run.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => renaming !== run.id && open()}
                  onKeyDown={(e) => e.key === "Enter" && renaming !== run.id && e.target === e.currentTarget && open()}
                  className={`w-full text-left rounded-lg px-2 py-1.5 flex items-start gap-2 group cursor-pointer outline-none focus-visible:ring-1 focus-visible:ring-zinc-600 ${
                    active ? "bg-zinc-800/80" : "hover:bg-zinc-900"
                  }`}
                  title={run.title}
                >
                  <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${dot(run, live)}`} />
                  <span className="min-w-0 flex-1">
                    {renaming === run.id ? (
                      <input
                        autoFocus
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onClick={(e) => e.stopPropagation()}
                        onKeyDown={(e) => {
                          e.stopPropagation();
                          if (e.key === "Enter") void rename(run.id);
                          if (e.key === "Escape") setRenaming(null);
                        }}
                        onBlur={() => void rename(run.id)}
                        maxLength={200}
                        className="w-full bg-zinc-900 border border-zinc-600 rounded px-1.5 py-0.5 text-[13px] text-zinc-100 outline-none"
                        aria-label="Run name"
                      />
                    ) : (
                      <span className={`block truncate text-[13px] ${active ? "text-zinc-100" : "text-zinc-300"}`}>
                        {run.title.split("\n")[0] || "(untitled)"}
                      </span>
                    )}
                    <span className="flex items-center gap-1.5 text-[11px] text-zinc-600">
                      {timeLabel(run.started)}
                      {live && <span className="text-green-400">running</span>}
                      {(run.kind === "signup" || run.kind === "login") && <UserPlus className="w-3 h-3" />}
                      {run.group && <Layers className="w-3 h-3" aria-label="part of a split request" />}
                      {run.has_gif && <Film className="w-3 h-3" />}
                      {run.verdict && (
                        <span className="inline-flex items-center gap-0.5">
                          <Scale className="w-3 h-3" />
                          {run.verdict}
                        </span>
                      )}
                    </span>
                  </span>
                  <RunMenu
                    run={run}
                    running={live}
                    onOpen={open}
                    onRename={() => {
                      setDraft(run.title.split("\n")[0]);
                      setRenaming(run.id);
                    }}
                    onDelete={() => remove(run.id)}
                  />
                </div>
              );
            })}
          </div>
        ))}
      </nav>
    </aside>
  );
}
