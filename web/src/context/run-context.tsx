"use client";

// Replaces chat-ui-example's session-context (Browser Use Cloud SDK streaming) with the local veil server:
// the run state arrives as server-sent events from /api/events, and actions are plain POSTs.
// veil has sessions: "main" is the chat, and a request split into jobs runs each job in its own session
// side by side. This context follows one of them (`sessionId`) and knows about all of them (`snap.sessions`).
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, apiUrl } from "@/lib/api";
import { groupIntoTurns } from "@/lib/turns";
import type { ConversationTurn, Snapshot } from "@/lib/types";
import { useSettings } from "./settings-context";

const EMPTY: Snapshot = {
  session: "main",
  group: null,
  label: "",
  sessions: [],
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
  sessionId: string;
  selectSession: (id: string) => void;
  closeSession: (id: string) => Promise<void>;
  start: (task: string) => Promise<boolean>;
  steer: (text: string) => Promise<boolean>;
  startAccount: (action: "signup" | "login", url: string, profileId: string) => Promise<boolean>;
  startBulk: (url: string, count: number, prompt: string) => Promise<boolean>;
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
  const [sessionId, setSessionId] = useState("main");
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;

  // Keep only the newest snapshot of the session being followed (a POST reply can land after a newer SSE push).
  const accept = useCallback((s: Snapshot) => {
    if (s.session !== sessionRef.current) return;
    setSnap((prev) => (prev.session !== s.session || s.version >= prev.version || s.status !== prev.status ? s : { ...prev, sessions: s.sessions }));
  }, []);

  useEffect(() => {
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    const open = () => {
      es = new EventSource(apiUrl(`/api/events?session=${encodeURIComponent(sessionId)}`));
      es.onopen = () => setConnected(true);
      es.onmessage = (m) => {
        try {
          const s: Snapshot = JSON.parse(m.data);
          if (s.session !== sessionId) {
            setSessionId(s.session); // that session was closed: the server fell back to the chat
            return;
          }
          setSnap(s);
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
  }, [sessionId]);

  const fail = useCallback((e: unknown) => setError(e instanceof Error ? e.message : String(e)), []);

  /** A reply may put us in another session (a split request, or a run started alongside a busy one). */
  const follow = useCallback(
    (s: Snapshot) => {
      if (s.session !== sessionRef.current) {
        sessionRef.current = s.session;
        setSessionId(s.session);
        setSnap(s);
      } else accept(s);
      if (s.skipped?.length) setError(`Not started: ${s.skipped.join("; ")}`);
    },
    [accept],
  );

  const act = useCallback(
    (fn: (session: string) => Promise<Snapshot>) => async () => {
      try {
        accept(await fn(sessionRef.current));
      } catch (e) {
        fail(e);
      }
    },
    [accept, fail],
  );

  const start = useCallback(
    async (task: string) => {
      setError(null);
      try {
        follow(await api.run(sessionRef.current, task, uiValues(), review));
        return true;
      } catch (e) {
        fail(e);
        return false;
      }
    },
    [follow, fail, uiValues, review],
  );

  const steer = useCallback(
    async (text: string) => {
      setError(null);
      try {
        accept(await api.steer(sessionRef.current, text));
        return true;
      } catch (e) {
        fail(e);
        return false;
      }
    },
    [accept, fail],
  );

  const startAccount = useCallback(
    async (action: "signup" | "login", url: string, profileId: string) => {
      setError(null);
      try {
        follow(await api.account(action, url, profileId, uiValues(), review));
        return true;
      } catch (e) {
        fail(e);
        return false;
      }
    },
    [follow, fail, uiValues, review],
  );

  const startBulk = useCallback(
    async (url: string, count: number, prompt: string) => {
      setError(null);
      try {
        follow(await api.bulkAccounts({ url, count, prompt, values: uiValues(), review }));
        return true;
      } catch (e) {
        fail(e);
        return false;
      }
    },
    [follow, fail, uiValues, review],
  );

  const selectSession = useCallback((id: string) => {
    if (id === sessionRef.current) return;
    sessionRef.current = id;
    setSessionId(id);
    setSnap((prev) => ({ ...EMPTY, session: id, sessions: prev.sessions }));
    setError(null);
  }, []);

  const closeSession = useCallback(
    async (id: string) => {
      try {
        const s = await api.closeSession(id);
        if (id === sessionRef.current) follow(s);
        else setSnap((prev) => ({ ...prev, sessions: s.sessions }));
      } catch (e) {
        fail(e);
      }
    },
    [follow, fail],
  );

  const clear = useCallback(async () => {
    try {
      follow(await api.clear(sessionRef.current));
    } catch (e) {
      fail(e);
    }
  }, [follow, fail]);

  const value = useMemo<RunContextType>(
    () => ({
      snap,
      turns: groupIntoTurns(snap.events),
      connected,
      error,
      setError,
      sessionId,
      selectSession,
      closeSession,
      start,
      steer,
      startAccount,
      startBulk,
      stop: act(api.stop),
      pauseResume: act(api.pause),
      takeOver: act(api.takeOver),
      handBack: (note?: string) => act((s) => api.handBack(s, note))(),
      clear,
    }),
    [snap, connected, error, sessionId, selectSession, closeSession, start, steer, startAccount, startBulk, act, clear],
  );

  return <RunContext.Provider value={value}>{children}</RunContext.Provider>;
}

export function useRun() {
  const ctx = useContext(RunContext);
  if (!ctx) throw new Error("useRun must be inside RunProvider");
  return ctx;
}
