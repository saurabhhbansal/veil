"use client";

// Replaces chat-ui-example's browser-panel.tsx, which embedded Browser Use Cloud's liveUrl in an iframe.
// Here the agent's Fortress browser runs on this PC: frames arrive over /api/live (a CDP screencast) and
// your mouse, wheel, keyboard and paste go back the same way, so you can use the page directly.
// Your first click or key press pauses the agent before its next step; "Hand back" lets it carry on.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import { ArrowLeft, ArrowRight, Hand, Info, Keyboard, Loader2, Pause, Play, RotateCw, Square, X } from "lucide-react";
import { useRun } from "@/context/run-context";
import { liveSocketUrl } from "@/lib/api";
import { lastStepUrl } from "@/lib/turns";
import { HumanPrompt } from "./human-card";
import { IdentityDetails, identitySummary } from "./identity-card";

type Outgoing = Record<string, unknown>;

const BUTTON_NAMES = ["left", "middle", "right", "back", "forward"];

function modifiers(e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): number {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}

function useLiveSocket(onFrame: (data: string) => void, session: string) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [info, setInfo] = useState({ live: false, url: "", title: "" });

  useEffect(() => {
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const open = () => {
      const ws = new WebSocket(liveSocketUrl(session));
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onmessage = (m) => {
        let msg: { t: string; data?: string; live?: boolean; url?: string; title?: string };
        try {
          msg = JSON.parse(m.data);
        } catch {
          return;
        }
        if (msg.t === "frame" && msg.data) onFrame(msg.data);
        else if (msg.t === "info") setInfo({ live: !!msg.live, url: msg.url ?? "", title: msg.title ?? "" });
      };
      ws.onclose = () => {
        setConnected(false);
        setInfo((i) => ({ ...i, live: false }));
        if (!closed) retry = setTimeout(open, 1000);
      };
    };
    open();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      wsRef.current?.close();
      setInfo({ live: false, url: "", title: "" });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);

  const send = useCallback((ev: Outgoing) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(ev));
  }, []);

  return { connected, info, send };
}

export function BrowserPanel() {
  const { snap, sessionId, takeOver, handBack, pauseResume, stop } = useRun();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const [hasFrame, setHasFrame] = useState(false);
  const [focused, setFocused] = useState(false);
  const [showIdentity, setShowIdentity] = useState(false);
  const [urlDraft, setUrlDraft] = useState<string | null>(null);
  const pendingMove = useRef<Outgoing | null>(null);
  const moveScheduled = useRef(false);
  const drawing = useRef(false);
  const latest = useRef<string | null>(null);

  const draw = useCallback(async () => {
    if (drawing.current) return;
    drawing.current = true;
    try {
      while (latest.current) {
        const b64 = latest.current;
        latest.current = null;
        const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
        const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/jpeg" }));
        const canvas = canvasRef.current;
        if (canvas) {
          if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
          }
          canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
          setHasFrame(true);
        }
        bitmap.close();
      }
    } catch {
      /* a corrupt frame: the next one replaces it */
    } finally {
      drawing.current = false;
    }
  }, []);

  const onFrame = useCallback(
    (data: string) => {
      latest.current = data; // only the newest frame is drawn
      void draw();
    },
    [draw],
  );

  const { connected, info, send } = useLiveSocket(onFrame, sessionId);
  const live = info.live && snap.browser_open;
  const interactive = live && hasFrame;
  const url = info.url || lastStepUrl(snap.events);

  useEffect(() => {
    if (!live) setHasFrame(false);
  }, [live]);

  useEffect(() => setHasFrame(false), [sessionId]); // another session's browser: wait for its first frame

  // ── input ────────────────────────────────────────────────────────────────

  const pos = (e: { clientX: number; clientY: number }) => {
    const r = canvasRef.current!.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  };

  const mouse = (type: string, e: MouseEvent | globalThis.MouseEvent, button?: string) => ({
    t: "mouse",
    type,
    ...pos(e),
    button: button ?? "none",
    buttons: e.buttons,
    clickCount: type === "mouseMoved" ? 0 : Math.max(1, Math.min(3, e.detail || 1)),
    modifiers: modifiers(e),
  });

  const onMouseDown = (e: MouseEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    e.preventDefault();
    surfaceRef.current?.focus();
    const button = BUTTON_NAMES[e.button] ?? "left";
    send(mouse("mousePressed", e, button));
    // Finish the press even if the pointer leaves the view (sliders, drag-to-verify).
    const up = (ev: globalThis.MouseEvent) => {
      send(mouse("mouseReleased", ev, button));
      window.removeEventListener("mouseup", up);
      window.removeEventListener("mousemove", move);
    };
    const move = (ev: globalThis.MouseEvent) => queueMove(mouse("mouseMoved", ev, button));
    window.addEventListener("mouseup", up);
    window.addEventListener("mousemove", move);
  };

  const queueMove = (ev: Outgoing) => {
    pendingMove.current = ev;
    if (moveScheduled.current) return;
    moveScheduled.current = true;
    requestAnimationFrame(() => {
      moveScheduled.current = false;
      if (pendingMove.current) send(pendingMove.current);
      pendingMove.current = null;
    });
  };

  const onMouseMove = (e: MouseEvent<HTMLCanvasElement>) => {
    if (!interactive || e.buttons) return; // while a button is held, the window listener sends moves
    queueMove(mouse("mouseMoved", e));
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (e: WheelEvent) => {
      if (!interactive) return;
      e.preventDefault();
      const scale = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 800 : 1;
      send({ t: "wheel", ...pos(e), dx: e.deltaX * scale, dy: e.deltaY * scale, modifiers: modifiers(e) });
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, send]);

  const onKey = (e: KeyboardEvent<HTMLDivElement>, type: "keyDown" | "keyUp") => {
    if (!interactive || e.nativeEvent.isComposing) return;
    // Ctrl/Cmd+V: let the browser fire a paste event and send its text (your clipboard, not the remote one).
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") return;
    e.preventDefault();
    send({ t: "key", type, key: e.key, code: e.code, keyCode: e.keyCode, modifiers: modifiers(e) });
  };

  const onPaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    if (!interactive) return;
    const text = e.clipboardData.getData("text/plain");
    if (text) {
      e.preventDefault();
      send({ t: "text", text });
    }
  };

  const nav = (action: string, target?: string) => send({ t: "nav", action, ...(target ? { url: target } : {}) });

  // ── render ───────────────────────────────────────────────────────────────

  const st = snap.status;
  const agentDriving = st === "running";
  const placeholder = !connected
    ? "Connecting to veil…"
    : st === "queued"
      ? "Waiting for a free browser: this session starts when another one finishes."
      : st === "starting"
      ? "Launching a fresh identity: proxy → geo → device → Fortress…"
      : snap.busy
        ? "Waiting for the browser…"
        : snap.events.length
          ? "The browser closed when the run ended."
          : "The agent's browser appears here once a task starts.";

  return (
    <div className="flex flex-col h-full bg-zinc-950 border-l border-zinc-800 min-w-0">
      {/* Header */}
      <div className="h-11 border-b border-zinc-800 flex items-center px-4 gap-3">
        <span className="text-[13px] font-medium text-zinc-300 shrink-0">Agent&apos;s Browser</span>
        <span className={`flex items-center gap-1.5 text-[11px] shrink-0 ${live ? "text-green-400" : "text-zinc-600"}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${live ? "bg-green-400 animate-pulse" : "bg-zinc-700"}`} />
          {live ? "Live" : "Offline"}
        </span>
        {snap.identity && (
          <button
            onClick={() => setShowIdentity(!showIdentity)}
            className="flex items-center gap-1 text-[11px] text-zinc-500 hover:text-zinc-300 truncate min-w-0"
            title="What the site sees"
          >
            <Info className="w-3.5 h-3.5 shrink-0" />
            <span className="truncate">{identitySummary(snap.identity)}</span>
          </button>
        )}
        <div className="ml-auto flex items-center gap-1.5 shrink-0">
          {snap.busy && st !== "stopping" && (
            <button
              onClick={stop}
              className="h-7 px-2 rounded-md border border-red-900/60 text-[12px] text-red-300 hover:bg-red-950/60 inline-flex items-center gap-1"
              title="Stop the run (finishes the current step)"
            >
              <Square className="w-3 h-3" fill="currentColor" /> Stop
            </button>
          )}
          {(st === "running" || st === "paused") && (
            <>
              <button
                onClick={pauseResume}
                className="h-7 px-2 rounded-md border border-zinc-700 text-[12px] text-zinc-300 hover:bg-zinc-800 inline-flex items-center gap-1"
                title={st === "paused" ? "Resume the agent" : "Pause the agent before its next step"}
              >
                {st === "paused" ? <Play className="w-3.5 h-3.5" /> : <Pause className="w-3.5 h-3.5" />}
                {st === "paused" ? "Resume" : "Pause"}
              </button>
              <button
                onClick={takeOver}
                className="h-7 px-2 rounded-md bg-zinc-100 hover:bg-white text-zinc-900 text-[12px] font-medium inline-flex items-center gap-1"
                title="Pause the agent and use the browser yourself"
              >
                <Hand className="w-3.5 h-3.5" /> Take control
              </button>
            </>
          )}
        </div>
      </div>

      {showIdentity && snap.identity && (
        <div className="border-b border-zinc-800 px-4 py-3 bg-zinc-900/60 relative">
          <button onClick={() => setShowIdentity(false)} className="absolute right-3 top-2 text-zinc-500 hover:text-zinc-300" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
          <IdentityDetails identity={snap.identity} />
        </div>
      )}

      {/* URL bar */}
      <div className="h-9 border-b border-zinc-800 flex items-center px-2 gap-1">
        {[
          { icon: ArrowLeft, action: "back", label: "Back" },
          { icon: ArrowRight, action: "forward", label: "Forward" },
          { icon: RotateCw, action: "reload", label: "Reload" },
        ].map(({ icon: Icon, action, label }) => (
          <button
            key={action}
            onClick={() => nav(action)}
            disabled={!interactive}
            title={`${label} (takes control)`}
            aria-label={label}
            className="w-7 h-7 rounded flex items-center justify-center text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800 disabled:opacity-30 disabled:hover:bg-transparent"
          >
            <Icon className="w-3.5 h-3.5" />
          </button>
        ))}
        <form
          className="flex-1 min-w-0"
          onSubmit={(e) => {
            e.preventDefault();
            if (urlDraft && urlDraft.trim()) nav("goto", urlDraft.trim());
            setUrlDraft(null);
            surfaceRef.current?.focus();
          }}
        >
          <input
            value={urlDraft ?? (st === "idle" && !live ? "" : url)}
            onChange={(e) => setUrlDraft(e.target.value)}
            onFocus={(e) => {
              setUrlDraft(url);
              e.target.select();
            }}
            onBlur={() => setUrlDraft(null)}
            disabled={!interactive}
            placeholder={snap.busy ? "Waiting…" : "No page"}
            spellCheck={false}
            className="w-full h-7 bg-zinc-900 rounded-md px-2.5 text-[12px] text-zinc-300 outline-none border border-transparent focus:border-zinc-600 disabled:text-zinc-500 font-mono"
          />
        </form>
      </div>

      {st === "needs_human" && snap.human && (
        <div className="px-3 pt-3">
          <HumanPrompt human={snap.human} onContinue={handBack} compact />
        </div>
      )}

      {/* Browser view */}
      <div
        ref={surfaceRef}
        tabIndex={interactive ? 0 : -1}
        onKeyDown={(e) => onKey(e, "keyDown")}
        onKeyUp={(e) => onKey(e, "keyUp")}
        onPaste={onPaste}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        className="flex-1 min-h-0 bg-zinc-900 relative overflow-hidden flex items-center justify-center p-3 outline-none"
        aria-label="Agent's browser. Click to use it; keys you press go to the page."
      >
        <canvas
          ref={canvasRef}
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
          onContextMenu={(e) => e.preventDefault()}
          className={`max-w-full max-h-full rounded-md shadow-2xl ring-1 transition-shadow ${
            focused && interactive ? "ring-2 ring-amber-400/70" : "ring-zinc-800"
          } ${interactive ? "cursor-default" : ""} ${live && hasFrame ? "" : "hidden"}`}
        />
        {!(live && hasFrame) && (
          <div className="flex flex-col items-center gap-3 text-zinc-500 text-sm text-center px-6">
            {snap.busy && <Loader2 className="w-5 h-5 animate-spin" />}
            {placeholder}
          </div>
        )}
        {interactive && agentDriving && (
          <div className="pointer-events-none absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full bg-zinc-950/85 border border-zinc-700 px-3 py-1 text-[11px] text-zinc-300">
            The agent is driving. Click or type in the page to take over.
          </div>
        )}
        {interactive && focused && (
          <div className="pointer-events-none absolute top-4 right-5 flex items-center gap-1 rounded-md bg-amber-400/90 text-zinc-950 px-2 py-0.5 text-[11px] font-medium">
            <Keyboard className="w-3 h-3" /> Keys go to the page
          </div>
        )}
      </div>
    </div>
  );
}
