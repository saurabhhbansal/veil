// The local veil server (`uv run veil ui`). In the built app it's the same origin; under `npm run dev`
// (port 3000) it's `veil ui --dev` on 7788 unless NEXT_PUBLIC_VEIL_API says otherwise.
import type { Snapshot, VeilConfig } from "./types";

export function apiBase(): string {
  if (process.env.NEXT_PUBLIC_VEIL_API) return process.env.NEXT_PUBLIC_VEIL_API.replace(/\/$/, "");
  if (typeof window !== "undefined" && window.location.port === "3000") {
    return `${window.location.protocol}//${window.location.hostname}:7788`;
  }
  return "";
}

export function apiUrl(path: string): string {
  return `${apiBase()}${path}`;
}

export function liveSocketUrl(): string {
  const base = apiBase() || window.location.origin;
  return base.replace(/^http/, "ws") + "/api/live";
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(apiUrl(path), {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      msg = data.detail || data.error || msg;
    } catch {
      /* not JSON */
    }
    throw new Error(msg);
  }
  return res.json();
}

export const api = {
  config: () => call<VeilConfig>("/api/config"),
  state: () => call<Snapshot>("/api/state"),
  run: (task: string, values: Record<string, unknown>) => call<Snapshot>("/api/run", { task, values }),
  stop: () => call<Snapshot>("/api/stop", {}),
  pause: () => call<Snapshot>("/api/pause", {}),
  takeOver: () => call<Snapshot>("/api/takeover", {}),
  handBack: (note = "") => call<Snapshot>("/api/handback", { note }),
  clear: () => call<Snapshot>("/api/clear", {}),
};
