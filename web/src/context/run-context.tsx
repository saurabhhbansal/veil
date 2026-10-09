"use client";

// Replaces chat-ui-example's session-context (Browser Use Cloud SDK streaming) with the local veil server:
// the run state arrives as server-sent events from /api/events, and actions are plain POSTs.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, apiUrl } from "@/lib/api";
import { groupIntoTurns } from "@/lib/turns";
import type { ConversationTurn, Snapshot } from "@/lib/types";
import { useSettings } from "./settings-context";

const EMPTY: Snapshot = {
  version: -1,
  status: "idle",
  busy: false,
  events: [],
  identity: null,
  human: null,
  browser_open: false,
  gif: null,
  result_file: null,
  run_id: null,
  has_review: false,
};

interface RunContextType {
  snap: Snapshot;
  turns: ConversationTurn[];
  connected: boolean;
  error: string | null;
  setError: (e: string | null) => void;
  start: (task: string) => Promise<boolean>;
  steer: (text: string) => Promise<boolean>;
  startSignup: (url: string, profileId: string) => Promise<boolean>;
  stop: () => Promise<void>;
  pauseResume: () => Promise<void>;
  takeOver: () => Promise<void>;
  handBack: (note?: string) => Promise<void>;
  clear: () => Promise<void>;
}

const RunContext = createContext<RunContextType | null>(null);

export function RunProvider({ children }: { children: ReactNode }) {
  const { uiValues, settings } = useSettings();
  const review = !!settings?.review;
  const [snap, setSnap] = useState<Snapshot>(EMPTY);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Keep only the newest snapshot (a POST reply can land after a newer SSE push).
  const accept = useCallback((s: Snapshot) => {
    setSnap((prev) => (s.version >= prev.version || s.status !== prev.status ? s : prev));
  }, []);

  useEffect(() => {
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    const open = () => {
      es = new EventSource(apiUrl("/api/events"));
      es.onopen = () => setConnected(true);
      es.onmessage = (m) => {
        try {
          setSnap(JSON.parse(m.data));
        } catch {
          /* ignore a torn message */
        }
      };
      es.onerror = () => {
        setConnected(false);
        es?.close();
        if (!closed) retry = setTimeout(open, 1500);
      };
    };
    open();
    return () => {
      closed = true;
      es?.close();
      if (retry) clearTimeout(retry);
    };
  }, []);

  const wrap = useCallback(
    (fn: () => Promise<Snapshot>) => async () => {
      try {
        accept(await fn());
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [accept],
  );

  const start = useCallback(
    async (task: string) => {
      setError(null);
      try {
        accept(await api.run(task, uiValues(), review));
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return false;
      }
    },
    [accept, uiValues, review],
  );

  const steer = useCallback(
    async (text: string) => {
      setError(null);
      try {
        accept(await api.steer(text));
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return false;
      }
    },
    [accept],
  );

  const startSignup = useCallback(
    async (url: string, profileId: string) => {
      setError(null);
      try {
        accept(await api.signup(url, profileId, uiValues(), review));
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return false;
      }
    },
    [accept, uiValues, review],
  );

  const value = useMemo<RunContextType>(
    () => ({
      snap,
      turns: groupIntoTurns(snap.events),
      connected,
      error,
      setError,
      start,
      steer,
      startSignup,
      stop: wrap(api.stop),
      pauseResume: wrap(api.pause),
      takeOver: wrap(api.takeOver),
      handBack: (note?: string) => wrap(() => api.handBack(note))(),
      clear: wrap(api.clear),
    }),
    [snap, connected, error, start, steer, startSignup, wrap],
  );

  return <RunContext.Provider value={value}>{children}</RunContext.Provider>;
}

export function useRun() {
  const ctx = useContext(RunContext);
  if (!ctx) throw new Error("useRun must be inside RunProvider");
  return ctx;
}
