// The local veil server (`uv run veil ui`). In the built app it's the same origin; under `npm run dev`
// (port 3000) it's `veil ui --dev` on 7788 unless NEXT_PUBLIC_VEIL_API says otherwise.
import type { AccountsState, KeysState, ProfileDraft, ProxiesState, RunDetail, RunSummary, Snapshot, VeilConfig } from "./types";

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

export function liveSocketUrl(session: string): string {
  const base = apiBase() || window.location.origin;
  return base.replace(/^http/, "ws") + `/api/live?session=${encodeURIComponent(session)}`;
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

const enc = encodeURIComponent;

export const api = {
  config: () => call<VeilConfig>("/api/config"),
  state: (session: string) => call<Snapshot>(`/api/state?session=${enc(session)}`),
  run: (session: string, task: string, values: Record<string, unknown>, review: boolean) =>
    call<Snapshot>("/api/run", { session, task, values, review }),
  steer: (session: string, text: string) => call<Snapshot>("/api/steer", { session, text }),
  stop: (session: string) => call<Snapshot>("/api/stop", { session }),
  pause: (session: string) => call<Snapshot>("/api/pause", { session }),
  takeOver: (session: string) => call<Snapshot>("/api/takeover", { session }),
  handBack: (session: string, note = "") => call<Snapshot>("/api/handback", { session, note }),
  clear: (session: string) => call<Snapshot>("/api/clear", { session }),
  closeSession: (session: string) => call<Snapshot>("/api/sessions/close", { session }),

  runs: () => call<{ runs: RunSummary[] }>("/api/runs"),
  runDetail: (id: string) => call<RunDetail>(`/api/runs/${enc(id)}`),
  reviewRun: (id: string, values: Record<string, unknown>) => call<RunDetail>(`/api/runs/${enc(id)}/review`, { values }),
  renameRun: (id: string, title: string) => call<{ ok: boolean }>(`/api/runs/${enc(id)}/rename`, { title }),
  deleteRun: (id: string) => call<{ ok: boolean }>(`/api/runs/${enc(id)}/delete`, {}),

  accounts: () => call<AccountsState>("/api/accounts"),
  saveProfiles: (profiles: Omit<ProfileDraft, "key" | "has_password">[]) => call<AccountsState>("/api/accounts/profiles", { profiles }),
  reveal: (site: string, profileId: string) =>
    call<{ site: string; email: string; password: string }>("/api/accounts/reveal", { site, profile_id: profileId }),
  account: (action: "signup" | "login", url: string, profileId: string, values: Record<string, unknown>, review: boolean) =>
    call<Snapshot>(`/api/accounts/${action}`, { url, profile_id: profileId || null, values, review }),
  bulkAccounts: (body: { url: string; count: number; prompt: string; values: Record<string, unknown>; review: boolean }) =>
    call<Snapshot>("/api/accounts/bulk", body),

  keys: () => call<KeysState>("/api/keys"),
  saveKey: (provider: string, key: string) => call<KeysState>("/api/keys", { provider, key }),
  saveLlmDefault: (provider: string, model: string, effort: string) => call<KeysState>("/api/llm/default", { provider, model, effort }),

  proxies: () => call<ProxiesState>("/api/proxies"),
  addProxies: (text: string) => call<ProxiesState>("/api/proxies/add", { text }),
  removeProxies: (ids: string[]) => call<ProxiesState>("/api/proxies/remove", { ids }),
  proxyMode: (patch: { mode?: string; gateway?: string; strategy?: string }) => call<ProxiesState>("/api/proxies/mode", patch),
  testProxies: (ids: string[] | null, gateway: boolean) => call<ProxiesState>("/api/proxies/test", { ids, gateway }),
};
