"use client";

// chat-ui-example's home page and session page in one: veil runs one task at a time on this PC, so there's
// no session id to route to. With no task yet it's the home screen; after that, chat on the left and the
// agent's live browser on the right.
import { useEffect, useRef, useState } from "react";
import { Download, FileJson, MessageSquare, MonitorPlay, Plus, VenetianMask } from "lucide-react";
import { BrowserPanel } from "@/components/browser-panel";
import { ChatInput } from "@/components/chat-input";
import { ChatMessages } from "@/components/chat-messages";
import { SettingsBar } from "@/components/settings-bar";
import { useRun } from "@/context/run-context";
import { useSettings } from "@/context/settings-context";
import { apiUrl } from "@/lib/api";
import type { RunStatus } from "@/lib/types";

const SUGGESTIONS = [
  "Find the cheapest 1TB NVMe SSD on amazon.in and return its price",
  "Open bot.sannysoft.com and list any checks that fail",
  "What IP address and city does whatismyipaddress.com show?",
  "List the top 3 stories on Hacker News right now",
];

const STATUS: Record<RunStatus, { label: string; dot: string }> = {
  idle: { label: "Idle", dot: "bg-zinc-600" },
  starting: { label: "Launching identity", dot: "bg-yellow-400 animate-pulse" },
  running: { label: "Running", dot: "bg-green-400 animate-pulse" },
  paused: { label: "Paused", dot: "bg-zinc-400" },
  needs_human: { label: "Needs you", dot: "bg-amber-400 animate-pulse" },
  stopping: { label: "Stopping", dot: "bg-orange-400 animate-pulse" },
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

function Home() {
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
    <div className="flex flex-col items-center justify-center min-h-screen px-4">
      <div className="w-full max-w-2xl mx-auto flex flex-col items-center gap-8">
        <div className="text-center space-y-3">
          <div className="inline-flex items-center gap-2 text-zinc-400">
            <VenetianMask className="w-5 h-5" />
            <span className="text-sm font-medium tracking-wide">veil</span>
          </div>
          <h1 className="text-3xl font-semibold text-zinc-100">What should the agent do?</h1>
          <p className="text-sm text-zinc-500 max-w-md mx-auto">
            It works in a Fortress stealth browser with a fresh, coherent device and exit IP. You can watch it live and take over any time, and
            it hands you the browser when a CAPTCHA needs a person.
          </p>
        </div>

        <div className="w-full">
          <ChatInput onSend={handleSend} disabled={isStarting || !config} placeholder="Describe a task…" footer={<SettingsBar />} />
          {isStarting && <p className="text-sm text-zinc-500 text-center mt-2">Starting…</p>}
          <ErrorLine />
        </div>

        <div className="flex flex-wrap justify-center gap-2">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => handleSend(s)}
              disabled={isStarting || !config}
              className="px-3 py-1.5 text-[13px] text-zinc-400 bg-zinc-900 border border-zinc-800 rounded-full hover:bg-zinc-800 hover:text-zinc-300 transition-colors disabled:opacity-50"
            >
              {s}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function SessionView() {
  const { snap, turns, start, stop, clear, handBack } = useRun();
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
          <span className="inline-flex items-center gap-1.5 text-zinc-300 text-[13px] font-medium">
            <VenetianMask className="w-4 h-4" /> veil
          </span>
          <span className="inline-flex items-center gap-1.5 text-[12px] text-zinc-400">
            <span className={`w-1.5 h-1.5 rounded-full ${status.dot}`} />
            {status.label}
          </span>
          <div className="ml-auto flex items-center gap-1">
            <button
              onClick={() => setMobilePane("browser")}
              className="lg:hidden h-7 px-2 rounded-md text-[12px] text-zinc-400 hover:bg-zinc-800 inline-flex items-center gap-1"
            >
              <MonitorPlay className="w-3.5 h-3.5" /> Browser
            </button>
            <button
              onClick={clear}
              disabled={snap.busy}
              title={snap.busy ? "Stop the run first" : "Clear the chat and start over"}
              className="h-7 px-2 rounded-md text-[12px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 inline-flex items-center gap-1 disabled:opacity-40 disabled:hover:bg-transparent"
            >
              <Plus className="w-3.5 h-3.5" /> New chat
            </button>
          </div>
        </header>

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
            onSend={(text) => (needsHuman ? handBack(text) : start(text))}
            isProcessing={snap.busy && !needsHuman}
            disabled={snap.busy && !needsHuman}
            onStop={stop}
            placeholder={
              needsHuman
                ? "Note for the agent, then Enter to hand back…"
                : snap.busy
                  ? "The agent is working…"
                  : "Send a new task…"
            }
            footer={!snap.busy ? <SettingsBar /> : undefined}
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
  const { snap, connected } = useRun();
  if (!connected && snap.version < 0) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center">
        <p className="text-sm text-zinc-500">Connecting to veil…</p>
        <ErrorLine />
      </div>
    );
  }
  return snap.events.length ? <SessionView /> : <Home />;
}
