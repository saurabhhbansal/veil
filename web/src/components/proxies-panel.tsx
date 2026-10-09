"use client";

// Side panel for proxies: where the agent's traffic exits. Direct (this PC), a list (each session takes the next
// proxy), or a provider's rotating gateway (each session gets its own sticky exit IP). Saved to .env and the
// list file on this PC; credentials go in, and only redacted addresses come back to this page.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { CheckCircle2, CircleX, Globe, Loader2, Plus, Radio, Shuffle, Trash2, Wifi, X } from "lucide-react";
import { useSettings } from "@/context/settings-context";
import { api } from "@/lib/api";
import type { ProxiesState, ProxyTest } from "@/lib/types";

const MODES: { id: ProxiesState["mode"]; label: string; hint: string; icon: ReactNode }[] = [
  { id: "direct", label: "Direct", hint: "This PC's own connection", icon: <Wifi className="w-4 h-4" /> },
  { id: "list", label: "Proxy list", hint: "Each session takes the next one", icon: <Shuffle className="w-4 h-4" /> },
  { id: "gateway", label: "Gateway", hint: "A provider's rotating endpoint", icon: <Radio className="w-4 h-4" /> },
];

const STRATEGY_LABEL: Record<string, string> = { round_robin: "In order", random: "Random", least_used: "Least used" };

const inputCls =
  "w-full bg-zinc-900 border border-zinc-800 rounded-md px-2.5 py-1.5 text-[13px] text-zinc-200 outline-none focus:border-zinc-500 placeholder-zinc-600";

function TestResult({ t }: { t: ProxyTest | null }) {
  if (!t) return <span className="text-[11px] text-zinc-600">not tested</span>;
  if (!t.ok)
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-red-400 min-w-0" title={t.error}>
        <CircleX className="w-3.5 h-3.5 shrink-0" /> <span className="truncate">{t.error}</span>
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1 text-[11px] text-green-400 min-w-0">
      <CheckCircle2 className="w-3.5 h-3.5 shrink-0" />
      <span className="truncate">
        {t.ip} · {[t.city, t.country].filter(Boolean).join(", ")} · {(t.ms / 1000).toFixed(1)}s
      </span>
    </span>
  );
}

export function ProxiesPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { refreshConfig } = useSettings();
  const [state, setState] = useState<ProxiesState | null>(null);
  const [message, setMessage] = useState<{ kind: "error" | "ok"; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // which action is running
  const [paste, setPaste] = useState("");
  const [gateway, setGateway] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const run = useCallback(
    async (what: string, fn: () => Promise<ProxiesState>, ok?: (s: ProxiesState) => string | null) => {
      setBusy(what);
      setMessage(null);
      try {
        const s = await fn();
        setState(s);
        const text = [s.note, ok?.(s), s.errors.length ? `Skipped: ${s.errors.join("; ")}` : null].filter(Boolean).join(" ");
        if (text) setMessage({ kind: s.errors.length ? "error" : "ok", text });
        void refreshConfig();
        return true;
      } catch (e) {
        setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
        return false;
      } finally {
        setBusy(null);
      }
    },
    [refreshConfig],
  );

  useEffect(() => {
    if (!open) return;
    api.proxies().then(setState).catch((e) => setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) }));
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const ids = state?.proxies.map((p) => p.id) ?? [];
  const chosen = ids.filter((id) => selected.has(id));

  return (
    <>
      <div className={`fixed inset-0 bg-black/50 z-40 transition-opacity ${open ? "opacity-100" : "opacity-0 pointer-events-none"}`} onClick={onClose} aria-hidden />
      <aside
        className={`fixed top-0 right-0 h-full w-full sm:w-[480px] bg-zinc-950 border-l border-zinc-800 z-50 flex flex-col transition-transform duration-200 ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
        aria-label="Proxies"
        aria-hidden={!open}
      >
        <header className="h-12 border-b border-zinc-800 flex items-center px-4 gap-2 shrink-0">
          <Globe className="w-4 h-4 text-zinc-400" />
          <span className="text-[14px] font-medium text-zinc-200">Proxies</span>
          <button onClick={onClose} className="ml-auto text-zinc-500 hover:text-zinc-200" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5">
          {!state && !message && (
            <div className="flex items-center gap-2 text-sm text-zinc-500">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          )}

          {state && (
            <>
              {/* Where traffic exits */}
              <section className="space-y-2">
                <p className="text-[11px] uppercase tracking-wider text-zinc-500">Runs use</p>
                <div className="grid grid-cols-3 gap-2">
                  {MODES.map((m) => {
                    const active = state.mode === m.id;
                    return (
                      <button
                        key={m.id}
                        onClick={() => !active && void run(`mode-${m.id}`, () => api.proxyMode({ mode: m.id }))}
                        disabled={!!busy}
                        className={`rounded-xl border p-2.5 text-left transition-colors ${
                          active ? "border-blue-500/60 bg-blue-500/10 text-blue-200" : "border-zinc-800 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                        }`}
                        aria-pressed={active}
                      >
                        <span className="flex items-center gap-1.5 text-[13px] font-medium">
                          {busy === `mode-${m.id}` ? <Loader2 className="w-4 h-4 animate-spin" /> : m.icon} {m.label}
                        </span>
                        <span className="block text-[11px] text-zinc-500 mt-0.5 leading-snug">{m.hint}</span>
                      </button>
                    );
                  })}
                </div>
                <p className="text-[12px] text-zinc-500 leading-relaxed">
                  Sessions that run side by side each get a different exit. Sign-ups and log-ins exit in the country on the person&apos;s
                  profile when a proxy there exists (test list proxies so veil learns their countries). Timezone and language follow each
                  exit IP. Saved to{" "}
                  <code className="text-zinc-400 break-all">{state.env_file}</code>.
                </p>
              </section>

              {/* The list */}
              <section className="space-y-2">
                <div className="flex items-center gap-2">
                  <p className="text-[11px] uppercase tracking-wider text-zinc-500">Proxy list ({state.proxies.length})</p>
                  <label className="ml-auto flex items-center gap-1.5 text-[12px] text-zinc-400">
                    Order
                    <select
                      value={state.strategy}
                      onChange={(e) => void run("strategy", () => api.proxyMode({ strategy: e.target.value }))}
                      className="bg-zinc-900 border border-zinc-800 rounded px-1.5 py-0.5 text-[12px] text-zinc-200 [color-scheme:dark]"
                    >
                      {state.strategies.map((s) => (
                        <option key={s} value={s}>
                          {STRATEGY_LABEL[s] ?? s}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                {state.proxies.length > 0 && (
                  <div className="rounded-xl border border-zinc-800 divide-y divide-zinc-800/80">
                    <div className="flex items-center gap-2 px-3 py-1.5">
                      <input
                        type="checkbox"
                        checked={chosen.length === ids.length && ids.length > 0}
                        onChange={(e) => setSelected(new Set(e.target.checked ? ids : []))}
                        className="accent-zinc-300"
                        aria-label="Select all"
                      />
                      <span className="text-[12px] text-zinc-500">{chosen.length ? `${chosen.length} selected` : "Select to test or remove"}</span>
                      <div className="ml-auto flex items-center gap-2">
                        <button
                          onClick={() => void run("test", () => api.testProxies(chosen.length ? chosen : null, false))}
                          disabled={!!busy}
                          className="text-[12px] text-zinc-300 hover:text-white disabled:opacity-40 inline-flex items-center gap-1"
                        >
                          {busy === "test" && <Loader2 className="w-3 h-3 animate-spin" />} Test {chosen.length ? "selected" : "all"}
                        </button>
                        {chosen.length > 0 && (
                          <button
                            onClick={() =>
                              void run("remove", () => api.removeProxies(chosen)).then((ok) => ok && setSelected(new Set()))
                            }
                            disabled={!!busy}
                            className="text-[12px] text-red-400/90 hover:text-red-300 disabled:opacity-40 inline-flex items-center gap-1"
                          >
                            <Trash2 className="w-3 h-3" /> Remove
                          </button>
                        )}
                      </div>
                    </div>
                    {state.proxies.map((p) => (
                      <label key={p.id} className="flex items-start gap-2 px-3 py-2 hover:bg-zinc-900/60 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={selected.has(p.id)}
                          onChange={(e) => {
                            const next = new Set(selected);
                            if (e.target.checked) next.add(p.id);
                            else next.delete(p.id);
                            setSelected(next);
                          }}
                          className="mt-0.5 accent-zinc-300"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block text-[12px] text-zinc-200 font-mono truncate">{p.server}</span>
                          <TestResult t={p.status} />
                        </span>
                      </label>
                    ))}
                  </div>
                )}

                <form
                  className="space-y-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (paste.trim()) void run("add", () => api.addProxies(paste)).then((ok) => ok && setPaste(""));
                  }}
                >
                  <textarea
                    value={paste}
                    onChange={(e) => setPaste(e.target.value)}
                    rows={3}
                    spellCheck={false}
                    placeholder={"Paste proxies, one per line:\nhost:port:user:pass\nhttp://user:pass@host:port"}
                    className={`${inputCls} font-mono text-[12px] resize-y`}
                  />
                  <div className="flex items-center gap-2">
                    <p className="text-[11px] text-zinc-500 flex-1">
                      Any provider&apos;s export works. Use HTTP proxies: Chromium can&apos;t log in to SOCKS proxies.
                    </p>
                    <button
                      type="submit"
                      disabled={!paste.trim() || !!busy}
                      className="shrink-0 inline-flex items-center gap-1.5 rounded-md bg-zinc-100 hover:bg-white text-zinc-900 text-[13px] font-medium px-3 py-1.5 disabled:opacity-40"
                    >
                      {busy === "add" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />} Add
                    </button>
                  </div>
                </form>
              </section>

              {/* Gateway */}
              <section className="space-y-2">
                <p className="text-[11px] uppercase tracking-wider text-zinc-500">Rotating gateway</p>
                {state.gateway && (
                  <div className="rounded-xl border border-zinc-800 px-3 py-2 space-y-1">
                    <p className="text-[12px] text-zinc-200 font-mono break-all">{state.gateway.server ?? state.gateway.error}</p>
                    <p className={`text-[11px] ${state.gateway.sticky ? "text-zinc-500" : "text-amber-400"}`}>
                      {state.gateway.sticky
                        ? "Sticky: each session keeps its own exit IP."
                        : "Not sticky: add {session} where the provider's session id goes, or IPs change mid-run."}
                      {state.gateway.server?.includes("{country}") || state.gateway.server?.includes("{COUNTRY}")
                        ? " Picks each person's country."
                        : " Add {country} where the provider takes a country code to match each person's country."}
                    </p>
                    <div className="flex items-center gap-3">
                      <TestResult t={state.gateway.status} />
                      <button
                        onClick={() => void run("test-gw", () => api.testProxies(null, true))}
                        disabled={!!busy}
                        className="ml-auto text-[12px] text-zinc-300 hover:text-white disabled:opacity-40 inline-flex items-center gap-1"
                      >
                        {busy === "test-gw" && <Loader2 className="w-3 h-3 animate-spin" />} Test
                      </button>
                    </div>
                  </div>
                )}
                <form
                  className="flex gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (gateway.trim())
                      void run("gateway", () => api.proxyMode({ mode: "gateway", gateway: gateway.trim() }), () => "Gateway saved; runs use it now.").then(
                        (ok) => ok && setGateway(""),
                      );
                  }}
                >
                  <input
                    type="password"
                    autoComplete="off"
                    value={gateway}
                    onChange={(e) => setGateway(e.target.value)}
                    placeholder={state.gateway ? "Replace: http://user-{session}:pass@gw:port" : "http://user-{session}:pass@gw:port"}
                    className={`${inputCls} font-mono text-[12px]`}
                  />
                  <button
                    type="submit"
                    disabled={!gateway.trim() || !!busy}
                    className="shrink-0 rounded-md border border-zinc-700 text-zinc-200 text-[13px] px-3 hover:bg-zinc-800 disabled:opacity-40"
                  >
                    Save
                  </button>
                </form>
              </section>

              {/* Where to get them */}
              <section className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-3 space-y-1.5 text-[12px] text-zinc-400 leading-relaxed">
                <p className="text-zinc-300 font-medium">Where proxies can come from</p>
                <p>
                  <span className="text-zinc-300">Your phone</span>: a proxy app on mobile data gives a real carrier IP for free. Add it to the list as{" "}
                  <code className="text-zinc-300">phone-ip:port</code>.
                </p>
                <p>
                  <span className="text-zinc-300">Residential or mobile provider</span>: paste their list export above, or their gateway with{" "}
                  <code className="text-zinc-300">{"{session}"}</code> in the username, e.g. DataImpulse{" "}
                  <code className="text-zinc-300 break-all">{"http://LOGIN__cr.us;sessid.{session}:PASS@gw.dataimpulse.com:823"}</code>.
                </p>
                <p>Datacenter proxies are cheap but most sign-up pages block them.</p>
              </section>
            </>
          )}

          {message && <p className={`text-[13px] break-words ${message.kind === "error" ? "text-red-400" : "text-green-400"}`}>{message.text}</p>}
        </div>
      </aside>
    </>
  );
}
