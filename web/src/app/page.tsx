"use client";

// chat-ui-example's home page and session page in one. With no task yet it's the home screen; after that, chat
// on the left and the agent's live browser on the right. A request split into jobs runs them side by side,
// one session each: tabs above the chat switch between them (chat and live browser follow the tab).
import { useEffect, useRef, useState } from "react";
import { Download, FileJson, Globe, Hand, Loader2, MessageSquare, MonitorPlay, Plus, UserRound, VenetianMask, X } from "lucide-react";
import { AccountsPanel } from "@/components/accounts-panel";
import { ProxiesPanel } from "@/components/proxies-panel";
import { RunView } from "@/components/run-view";
import { RunsSidebar } from "@/components/runs-sidebar";
import { BrowserPanel } from "@/components/browser-panel";
import { ChatInput } from "@/components/chat-input";
import { ChatMessages } from "@/components/chat-messages";
import { SettingsBar } from "@/components/settings-bar";
import { useRun } from "@/context/run-context";
import { useSettings } from "@/context/settings-context";
import { apiUrl } from "@/lib/api";
import type { RunStatus, SessionSummary } from "@/lib/types";

const STATUS: Record<RunStatus, { label: string; dot: string }> = {
  idle: { label: "Idle", dot: "bg-zinc-600" },
  queued: { label: "Waiting for a browser", dot: "bg-zinc-400 animate-pulse" },
  starting: { label: "Launching identity", dot: "bg-yellow-400 animate-pulse" },
  running: { label: "Running", dot: "bg-green-400 animate-pulse" },
  paused: { label: "Paused", dot: "bg-zinc-400" },
  needs_human: { label: "Needs you", dot: "bg-amber-400 animate-pulse" },
  stopping: { label: "Stopping", dot: "bg-orange-400 animate-pulse" },
  reviewing: { label: "Reviewing the run", dot: "bg-blue-400 animate-pulse" },
};

function ErrorLine() {
  const { error, setError } = useRun();
  const { configError } = useSettings();
  const msg = error || (configError ? `Can't reach the veil server (${configError}). Is \`uv run veil ui\` running?` : null);
  if (!msg) return null;
  return (
    <p className="text-sm text-red-400 text-center mt-2 max-w-lg mx-auto break-words">
      {msg}
      {error && (
        <button onClick={() => setError(null)} className="ml-2 text-zinc-500 hover:text-zinc-300 underline">
          dismiss
        </button>
      )}
    </p>
  );
}

type Panels = { onAccounts: () => void; onProxies: () => void };

function PanelButtons({ onAccounts, onProxies, compact }: Panels & { compact?: boolean }) {
  const cls = `inline-flex items-center gap-1.5 rounded-md text-[12px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 ${
    compact ? "h-7 px-2" : "h-8 px-3 border border-zinc-800"
  }`;
  return (
    <>
      <button onClick={onProxies} title="Where the browser's traffic exits" className={cls}>
        <Globe className="w-3.5 h-3.5" /> Proxies
      </button>
      <button onClick={onAccounts} title="Your sign-up profiles and accounts" className={cls}>
        <UserRound className="w-3.5 h-3.5" /> Accounts
      </button>
    </>
  );
}

function sessionDot(s: SessionSummary): string {
  if (s.needs_human) return "bg-amber-400 animate-pulse";
  if (s.busy) return STATUS[s.status]?.dot ?? "bg-green-400 animate-pulse";
  if (s.ok === false) return "bg-red-400";
  if (s.ok) return "bg-green-500";
  return "bg-zinc-600";
}

/** One tab per session when a request was split: the chat and the live browser follow the selected tab. */
function SessionTabs() {
  const { snap, sessionId, selectSession, closeSession } = useRun();
  const sessions = snap.sessions.filter((s) => s.id !== "main" || s.run_id || s.busy);
  if (snap.sessions.length < 2) return null;
  const waiting = snap.sessions.filter((s) => s.needs_human && s.id !== sessionId).length;
  return (
    <div className="border-b border-zinc-800 flex items-center gap-1 px-2 py-1.5 overflow-x-auto" role="tablist" aria-label="Sessions">
      {sessions.map((s) => {
        const active = s.id === sessionId;
        return (
          <div
            key={s.id}
            className={`group shrink-0 max-w-[220px] h-7 rounded-md flex items-center gap-1.5 pl-2 pr-1 text-[12px] border ${
              active ? "bg-zinc-800 border-zinc-700 text-zinc-100" : "border-transparent text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
            }`}
          >
            <button role="tab" aria-selected={active} onClick={() => selectSession(s.id)} className="flex items-center gap-1.5 min-w-0" title={s.label}>
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${sessionDot(s)}`} />
              <span className="truncate">{s.id === "main" ? "Chat" : s.label}</span>
              {s.needs_human && <Hand className="w-3 h-3 text-amber-400 shrink-0" aria-label="needs you" />}
              {s.status === "queued" && <Loader2 className="w-3 h-3 animate-spin shrink-0 text-zinc-500" aria-label="waiting for a browser" />}
            </button>
            {s.id !== "main" && !s.busy && (
              <button
                onClick={() => void closeSession(s.id)}
                className="w-5 h-5 rounded flex items-center justify-center text-zinc-500 hover:text-zinc-200 hover:bg-zinc-700 opacity-60 group-hover:opacity-100"
                aria-label={`Close ${s.label}`}
                title="Close (the run stays in the history)"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
        );
      })}
      {waiting > 0 && <span className="ml-auto shrink-0 text-[11px] text-amber-400 px-2">{waiting} need{waiting === 1 ? "s" : ""} you</span>}
    </div>
  );
}

function Home({ onAccounts, onProxies }: Panels) {
  const { start } = useRun();
  const { config } = useSettings();
  const [isStarting, setIsStarting] = useState(false);

  async function handleSend(task: string) {
    if (isStarting) return;
    setIsStarting(true);
    await start(task);
    setIsStarting(false);
  }

  return (
    <div className="relative flex flex-col items-center justify-center min-h-screen px-4">
      <div className="absolute top-4 right-4 flex gap-2">
        <PanelButtons onAccounts={onAccounts} onProxies={onProxies} />
      </div>
      <div className="w-full max-w-2xl mx-auto flex flex-col items-center gap-8">
        <div className="text-center space-y-3">
          <div className="inline-flex items-center gap-2 text-zinc-400">
            <VenetianMask className="w-5 h-5" />
            <span className="text-sm font-medium tracking-wide">veil</span>
          </div>
          <h1 className="text-3xl font-semibold text-zinc-100">What should the agent do?</h1>
          <p className="text-sm text-zinc-500 max-w-md mx-auto">
            It works in a Fortress stealth browser with a fresh, coherent device and exit IP. You can watch it live and take over any time, and
            it hands you the browser when a CAPTCHA or a code needs a person. Say “log in to reddit” or “create 2 accounts on X, one per
            profile” and it uses your details from Accounts, one browser per account, side by side.
          </p>
        </div>

        <div className="w-full">
          <ChatInput
            onSend={handleSend}
            disabled={isStarting || !config}
            placeholder="Describe a task…"
            autoFocus
            footer={<SettingsBar onManageProxies={onProxies} />}
          />
          {isStarting && <p className="text-sm text-zinc-500 text-center mt-2">Starting… (account requests are planned first)</p>}
          <ErrorLine />
        </div>
      </div>
    </div>
  );
}

function SessionView({ onAccounts, onProxies }: Panels) {
  const { snap, turns, start, stop, clear, handBack, steer } = useRun();
  const chatRef = useRef<HTMLDivElement>(null);
  const [mobilePane, setMobilePane] = useState<"chat" | "browser">("chat");
  const status = STATUS[snap.status];

  // Auto-scroll on new content, unless you've scrolled up to read.
  useEffect(() => {
    const el = chatRef.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 200) el.scrollTop = el.scrollHeight;
  }, [snap.version]);

  // On a phone, jump to the browser when you're needed there.
  useEffect(() => {
    if (snap.status === "needs_human") setMobilePane("browser");
  }, [snap.status]);

  const needsHuman = snap.status === "needs_human";

  return (
    <div className="flex h-screen w-full overflow-hidden">
      {/* Chat column */}
      <div className={`flex-1 flex-col min-w-0 ${mobilePane === "chat" ? "flex" : "hidden"} lg:flex`}>
        <header className="h-11 border-b border-zinc-800 flex items-center px-4 gap-3">
          <span className="md:hidden inline-flex items-center gap-1.5 text-zinc-300 text-[13px] font-medium">
            <VenetianMask className="w-4 h-4" /> veil
          </span>
          <span className="inline-flex items-center gap-1.5 text-[12px] text-zinc-400">
            <span className={`w-1.5 h-1.5 rounded-full ${status.dot}`} />
            {status.label}
          </span>
          <div className="ml-auto flex items-center gap-1">
            <PanelButtons onAccounts={onAccounts} onProxies={onProxies} compact />
            <button
              onClick={() => setMobilePane("browser")}
              className="lg:hidden h-7 px-2 rounded-md text-[12px] text-zinc-400 hover:bg-zinc-800 inline-flex items-center gap-1"
            >
              <MonitorPlay className="w-3.5 h-3.5" /> Browser
            </button>
            <button
              onClick={clear}
              disabled={snap.busy}
              title={snap.busy ? "Stop the run first" : snap.session === "main" ? "Clear the chat and start over" : "Close this session (the run stays in the history)"}
              className="h-7 px-2 rounded-md text-[12px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 inline-flex items-center gap-1 disabled:opacity-40 disabled:hover:bg-transparent"
            >
              {snap.session === "main" ? <Plus className="w-3.5 h-3.5" /> : <X className="w-3.5 h-3.5" />} {snap.session === "main" ? "New chat" : "Close"}
            </button>
          </div>
        </header>
        <SessionTabs />

        <div ref={chatRef} className="flex-1 overflow-y-auto px-6 py-6">
          <div className="max-w-2xl mx-auto">
            <ChatMessages turns={turns} snap={snap} onHandBack={handBack} />

            {(snap.gif || snap.result_file) && !snap.busy && (
              <div className="mt-6 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 flex flex-wrap gap-4">
                {snap.gif && (
                  <a href={apiUrl(snap.gif)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 text-sm text-blue-400 hover:underline">
                    <Download className="h-4 w-4" /> Recording (GIF)
                  </a>
                )}
                {snap.result_file && (
                  <a href={apiUrl(snap.result_file)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 text-sm text-blue-400 hover:underline">
                    <FileJson className="h-4 w-4" /> result.json
                  </a>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="max-w-2xl w-full mx-auto">
          <ErrorLine />
          <ChatInput
            onSend={(text) => (needsHuman ? handBack(text) : snap.busy ? steer(text) : start(text))}
            isProcessing={snap.busy && !needsHuman}
            disabled={snap.status === "stopping" || snap.status === "reviewing" || snap.status === "queued"}
            autoFocus
            onStop={stop}
            placeholder={
              needsHuman
                ? "Note or code for the agent, then Enter to hand back…"
                : snap.status === "queued"
                  ? "Waiting for a free browser…"
                  : snap.status === "reviewing"
                  ? "Reviewing the run…"
                  : snap.busy
                    ? "Steer the agent, e.g. \"use the search box instead\"…"
                    : "Send a new task…"
            }
            footer={!snap.busy ? <SettingsBar onManageProxies={onProxies} /> : undefined}
          />
        </div>
      </div>

      {/* Browser panel: always visible on desktop */}
      <div className={`${mobilePane === "browser" ? "flex" : "hidden"} lg:flex flex-col w-full lg:w-[55%] shrink-0 h-full`}>
        <div className="lg:hidden h-10 border-b border-zinc-800 flex items-center px-3">
          <button onClick={() => setMobilePane("chat")} className="h-7 px-2 rounded-md text-[12px] text-zinc-400 hover:bg-zinc-800 inline-flex items-center gap-1">
            <MessageSquare className="w-3.5 h-3.5" /> Chat
          </button>
        </div>
        <div className="flex-1 min-h-0">
          <BrowserPanel />
        </div>
      </div>
    </div>
  );
}

export default function Page() {
  const { snap, connected, clear, sessionId, selectSession } = useRun();
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [proxiesOpen, setProxiesOpen] = useState(false);
  const [viewing, setViewing] = useState<string | null>(null); // a past run; null = the live chat
  if (!connected && snap.version < 0 && sessionId === "main") {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center">
        <p className="text-sm text-zinc-500">Connecting to veil…</p>
        <ErrorLine />
      </div>
    );
  }
  const panels: Panels = { onAccounts: () => setAccountsOpen(true), onProxies: () => setProxiesOpen(true) };
  const reviews = snap.events.filter((e) => e.type === "judgement").length;
  const liveRunIds = snap.sessions.filter((s) => s.busy && s.run_id).map((s) => s.run_id as string);
  const sessionRuns = new Map(snap.sessions.filter((s) => s.run_id).map((s) => [s.run_id as string, s.id]));
  const past = viewing && viewing !== snap.run_id ? viewing : null;
  return (
    <div className="flex h-screen overflow-hidden">
      <RunsSidebar
        selectedId={past}
        liveRunIds={liveRunIds}
        currentRunId={snap.run_id}
        refreshKey={`${snap.sessions.map((s) => `${s.run_id}-${s.busy}`).join(",")}-${reviews}`}
        onSelect={(id) => {
          // A run that's open in a session (live or just finished): show that session instead of the read-only view.
          const sid = id ? sessionRuns.get(id) : undefined;
          if (sid) {
            selectSession(sid);
            setViewing(null);
          } else setViewing(id);
        }}
        onNew={() => {
          setViewing(null);
          selectSession("main");
          const main = snap.sessions.find((s) => s.id === "main");
          if (!main?.busy) void clear(); // the finished runs stay in the sidebar
        }}
        onDeleted={(id) => viewing === id && setViewing(null)}
      />
      <main className="flex-1 min-w-0 h-screen overflow-y-auto">
        {past ? (
          <RunView runId={past} liveBusy={snap.busy} onBack={() => setViewing(null)} />
        ) : snap.events.length || snap.session !== "main" ? (
          <SessionView {...panels} />
        ) : (
          <Home {...panels} />
        )}
      </main>
      <AccountsPanel open={accountsOpen} onClose={() => setAccountsOpen(false)} />
      <ProxiesPanel open={proxiesOpen} onClose={() => setProxiesOpen(false)} />
    </div>
  );
}
