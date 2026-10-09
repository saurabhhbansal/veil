"use client";

// Adapted from chat-ui-example's model-selector.tsx (IconDropdown / DropdownRow). The Browser Use Cloud
// pickers (BU models, cloud profiles, workspaces, proxy countries) are replaced by veil's run settings.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Cpu, Fingerprint, Globe, KeyRound, Loader2, Scale, ShieldCheck, SlidersHorizontal } from "lucide-react";
import { useSettings } from "@/context/settings-context";
import { api } from "@/lib/api";
import type { KeysState } from "@/lib/types";

const ENGINE_NOTES: Record<string, string> = {
  fortress: "Stealth Chromium (default)",
  "fortress-docker": "Fortress in Docker",
  cdp: "A browser you started (VEIL_CDP_URL)",
  chromium: "Stock Chromium: NOT stealthy, testing only",
};

interface IconDropdownProps {
  icon: ReactNode;
  title: string;
  activeColor: "amber" | "green" | "purple" | "blue" | "red";
  isActive: boolean;
  children: ReactNode;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  width?: string;
}

function IconDropdown({ icon, title, activeColor, isActive, children, open, onToggle, onClose, width = "w-[240px]" }: IconDropdownProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open, onClose]);

  const colorMap = {
    amber: "bg-amber-500/10 text-amber-400",
    green: "bg-green-500/10 text-green-400",
    purple: "bg-purple-500/10 text-purple-400",
    blue: "bg-blue-500/10 text-blue-400",
    red: "bg-red-500/10 text-red-400",
  };

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={onToggle}
        title={title}
        aria-label={title}
        className={`h-8 w-8 rounded-lg border flex items-center justify-center transition-colors ${
          isActive ? `${colorMap[activeColor]} border-transparent` : "bg-transparent hover:bg-zinc-800 text-zinc-500 border-zinc-700"
        }`}
      >
        {icon}
      </button>
      {open && (
        <div className={`absolute bottom-full mb-1 right-0 ${width} max-w-[calc(100vw-2rem)] bg-zinc-900 border border-zinc-700 rounded-lg shadow-xl z-50 flex flex-col max-h-[70vh] overflow-y-auto`}>
          <div className="px-3 pt-2 pb-1 text-[10px] text-zinc-500 uppercase tracking-wider">{title.split(":")[0]}</div>
          {children}
        </div>
      )}
    </div>
  );
}

function DropdownRow({ label, hint, selected, onSelect }: { label: string; hint?: string; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full text-left px-3 py-1.5 text-xs hover:bg-zinc-800 flex items-center gap-2 ${selected ? "text-white font-medium" : "text-zinc-400"}`}
    >
      <span className="flex-1 min-w-0">
        <span className="block truncate">{label}</span>
        {hint && <span className="block truncate text-[10px] text-zinc-500 font-normal">{hint}</span>}
      </span>
      {selected && <Check size={12} className="text-zinc-400 flex-shrink-0" />}
    </button>
  );
}

function Toggle({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800 cursor-pointer">
      <span className="flex-1 min-w-0">
        <span className="block">{label}</span>
        {hint && <span className="block text-[10px] text-zinc-500">{hint}</span>}
      </span>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="accent-zinc-300" />
    </label>
  );
}

function NumberRow({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (v: number) => void }) {
  return (
    <label className="flex items-center gap-2 px-3 py-1.5 text-xs text-zinc-300">
      <span className="flex-1">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => {
          const n = parseInt(e.target.value, 10);
          if (!Number.isNaN(n)) onChange(Math.min(max, Math.max(min, n)));
        }}
        className="w-16 bg-zinc-800 text-zinc-200 rounded px-2 py-1 outline-none text-right"
      />
    </label>
  );
}

const EFFORT_HINT: Record<string, string> = {
  "": "The provider's default",
  minimal: "Fastest, least thinking",
  low: "Quick and cheap",
  medium: "Balanced",
  high: "Thorough",
  xhigh: "Very thorough: slower, more tokens",
  max: "Everything it has: slowest, most tokens",
};

/** Default + the provider's levels on one slider. */
function EffortSlider({ levels, value, onChange }: { levels: string[]; value: string; onChange: (v: string) => void }) {
  const stops = ["", ...levels];
  const i = Math.max(0, stops.indexOf(value));
  return (
    <div className="px-3 py-2 space-y-1.5">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-zinc-300">Effort</span>
        <span className="text-zinc-200 font-medium">{value || "default"}</span>
      </div>
      <input
        type="range"
        min={0}
        max={stops.length - 1}
        step={1}
        value={i}
        onChange={(e) => onChange(stops[parseInt(e.target.value, 10)] ?? "")}
        className="w-full accent-amber-400"
        aria-label="Reasoning effort"
        aria-valuetext={value || "default"}
      />
      <div className="flex justify-between text-[9px] text-zinc-500 select-none">
        {stops.map((s) => (
          <span key={s || "default"}>{s || "def"}</span>
        ))}
      </div>
      <p className="text-[10px] text-zinc-500">{EFFORT_HINT[value] ?? ""}</p>
    </div>
  );
}

/** The provider's API key: whether one is set (masked), and a box to save a new one to .env. */
function KeyRow({ provider, keys, onSaved }: { provider: string; keys: KeysState | null; onSaved: (k: KeysState) => void }) {
  const info = keys?.keys[provider];
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    setDraft("");
    setMsg(null);
  }, [provider]);
  if (!info) {
    return provider === "openai-compatible" ? (
      <p className="px-3 py-1.5 text-[10px] text-zinc-500">Key and URL come from VEIL_LLM_API_KEY / VEIL_LLM_BASE_URL in .env.</p>
    ) : null;
  }

  async function save(value: string) {
    setBusy(true);
    setMsg(null);
    try {
      onSaved(await api.saveKey(provider, value));
      setDraft("");
      setMsg({ ok: true, text: value ? `Saved to .env as ${info!.var}` : "Removed from .env" });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="px-3 py-2 space-y-1.5 border-t border-zinc-800"
      onSubmit={(e) => {
        e.preventDefault();
        if (draft.trim()) void save(draft.trim());
      }}
    >
      <div className="flex items-center gap-1.5 text-xs text-zinc-300">
        <KeyRound size={12} className="text-zinc-500" />
        <span className="flex-1">API key</span>
        <span className={`text-[10px] ${info.set ? "text-green-400" : "text-amber-400"}`}>
          {info.set ? `set ${info.masked ?? ""}${info.source && info.source !== info.var ? ` (${info.source})` : ""}` : "not set"}
        </span>
      </div>
      <div className="flex gap-1">
        <input
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={info.set ? "Paste a new key to replace it" : `Paste your ${provider} key`}
          className="flex-1 min-w-0 bg-zinc-800 text-zinc-200 text-xs rounded px-2 py-1.5 outline-none placeholder-zinc-500"
        />
        <button type="submit" disabled={!draft.trim() || busy} className="px-2 text-xs rounded bg-zinc-700 text-zinc-200 disabled:opacity-40">
          {busy ? <Loader2 size={12} className="animate-spin" /> : "Save"}
        </button>
      </div>
      <p className={`text-[10px] ${msg ? (msg.ok ? "text-green-400" : "text-red-400") : "text-zinc-500"}`}>
        {msg?.text ?? `Saves to your .env as ${info.var}. Keys never come back to this page.`}
        {!msg && info.set && info.in_file && (
          <button type="button" onClick={() => void save("")} className="ml-1 underline hover:text-zinc-300">
            Remove
          </button>
        )}
      </p>
    </form>
  );
}

export function SettingsBar({ disabled, onManageProxies }: { disabled?: boolean; onManageProxies?: () => void }) {
  const { config, settings, update, refreshConfig } = useSettings();
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const [newIdentity, setNewIdentity] = useState("");
  const [keys, setKeys] = useState<KeysState | null>(null);
  const [defaultMsg, setDefaultMsg] = useState<string | null>(null);

  useEffect(() => {
    if (openDropdown !== "llm") return;
    setDefaultMsg(null);
    api.keys().then(setKeys).catch(() => setKeys(null));
  }, [openDropdown]);

  const toggle = useCallback((name: string) => setOpenDropdown((prev) => (prev === name ? null : name)), []);
  const close = useCallback(() => setOpenDropdown(null), []);

  if (!config || !settings) {
    return <span className="text-[11px] text-zinc-600 px-1">Connecting to veil…</span>;
  }

  const identities = Array.from(new Set([...config.identities, ...(settings.identity ? [settings.identity] : [])]));
  const identityOk = /^[A-Za-z0-9_.-]{1,64}$/.test(newIdentity);

  return (
    <div className={`flex items-center gap-1 ${disabled ? "opacity-50 pointer-events-none" : ""}`}>
      {/* Model: provider, model, effort, API key */}
      <IconDropdown
        icon={<Cpu size={16} />}
        title={`Model: ${settings.provider} / ${settings.model || "(model?)"}${settings.effort ? ` · ${settings.effort}` : ""}`}
        activeColor={config.keys[settings.provider] === false ? "red" : "amber"}
        isActive
        open={openDropdown === "llm"}
        onToggle={() => toggle("llm")}
        onClose={close}
        width="w-[330px]"
      >
        {config.providers.map((p) => (
          <DropdownRow
            key={p}
            label={p}
            hint={[p === config.llm_provider ? "default" : "", config.keys[p] === undefined ? "" : config.keys[p] ? "key set" : "no key"].filter(Boolean).join(" · ") || undefined}
            selected={settings.provider === p}
            onSelect={() =>
              update({ provider: p, model: p === config.llm_provider ? config.llm_model : config.models[p]?.[0] ?? "", effort: p === config.llm_provider ? config.llm_effort ?? "" : "" })
            }
          />
        ))}
        <div className="p-2 border-t border-zinc-800 space-y-1">
          <input
            value={settings.model}
            onChange={(e) => update({ model: e.target.value })}
            placeholder="Model name"
            list="veil-models"
            className="w-full bg-zinc-800 text-zinc-200 text-xs rounded px-2 py-1.5 outline-none placeholder-zinc-500"
          />
          <datalist id="veil-models">
            {(config.models[settings.provider] ?? []).map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
          {(config.models[settings.provider] ?? []).length > 0 && (
            <div className="flex flex-wrap gap-1">
              {config.models[settings.provider].map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => update({ model: m })}
                  className={`px-1.5 py-0.5 rounded text-[10px] border ${settings.model === m ? "border-amber-400/60 text-amber-300" : "border-zinc-700 text-zinc-400 hover:text-zinc-200"}`}
                >
                  {m}
                </button>
              ))}
            </div>
          )}
        </div>
        {(config.efforts[settings.provider] ?? []).length > 0 ? (
          <EffortSlider levels={config.efforts[settings.provider]} value={settings.effort} onChange={(v) => update({ effort: v })} />
        ) : (
          <p className="px-3 py-1.5 text-[10px] text-zinc-500">No effort setting for {settings.provider}.</p>
        )}
        <KeyRow
          provider={settings.provider}
          keys={keys}
          onSaved={(k) => {
            setKeys(k);
            void refreshConfig();
          }}
        />
        <div className="px-3 py-2 border-t border-zinc-800 flex items-center gap-2">
          <button
            type="button"
            onClick={async () => {
              try {
                await api.saveLlmDefault(settings.provider, settings.model, settings.effort);
                await refreshConfig();
                setDefaultMsg("Saved as the default in .env");
              } catch (e) {
                setDefaultMsg(e instanceof Error ? e.message : String(e));
              }
            }}
            disabled={!settings.model.trim()}
            className="text-[11px] px-2 py-1 rounded border border-zinc-700 text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            title="Write provider, model and effort to .env (VEIL_LLM_PROVIDER / _MODEL / _EFFORT)"
          >
            Make default
          </button>
          <span className="text-[10px] text-zinc-500 truncate">{defaultMsg ?? "This run uses what's picked here."}</span>
        </div>
        <Toggle label="Use vision" hint="Send page screenshots to the model" checked={settings.useVision} onChange={(v) => update({ useVision: v })} />
        <NumberRow label="Max steps per attempt" value={settings.maxSteps} min={1} max={200} onChange={(v) => update({ maxSteps: v })} />
      </IconDropdown>

      {/* Identity */}
      <IconDropdown
        icon={<Fingerprint size={16} />}
        title={settings.identity ? `Identity: ${settings.identity} (sticky)` : "Identity: fresh every run"}
        activeColor="green"
        isActive={!!settings.identity}
        open={openDropdown === "identity"}
        onToggle={() => toggle("identity")}
        onClose={close}
      >
        <DropdownRow
          label="Fresh identity"
          hint="New device, exit IP and empty profile"
          selected={!settings.identity}
          onSelect={() => {
            update({ identity: "" });
            close();
          }}
        />
        {identities.map((name) => (
          <DropdownRow
            key={name}
            label={name}
            hint="Same device, IP and cookies"
            selected={settings.identity === name}
            onSelect={() => {
              update({ identity: name });
              close();
            }}
          />
        ))}
        <form
          className="p-2 border-t border-zinc-800 flex gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            if (!identityOk) return;
            update({ identity: newIdentity });
            setNewIdentity("");
            close();
          }}
        >
          <input
            value={newIdentity}
            onChange={(e) => setNewIdentity(e.target.value)}
            placeholder="New sticky identity…"
            className="flex-1 min-w-0 bg-zinc-800 text-zinc-200 text-xs rounded px-2 py-1.5 outline-none placeholder-zinc-500"
          />
          <button type="submit" disabled={!identityOk} className="px-2 text-xs rounded bg-zinc-700 text-zinc-200 disabled:opacity-40">
            Use
          </button>
        </form>
      </IconDropdown>

      {/* Engine */}
      <IconDropdown
        icon={<ShieldCheck size={16} />}
        title={`Engine: ${settings.engine}`}
        activeColor={settings.engine === "chromium" ? "red" : "purple"}
        isActive
        open={openDropdown === "engine"}
        onToggle={() => toggle("engine")}
        onClose={close}
        width="w-[260px]"
      >
        {config.engines.map((e) => (
          <DropdownRow
            key={e}
            label={e}
            hint={ENGINE_NOTES[e]}
            selected={settings.engine === e}
            onSelect={() => {
              update({ engine: e });
              close();
            }}
          />
        ))}
      </IconDropdown>

      {/* Network */}
      <IconDropdown
        icon={<Globe size={16} />}
        title={`Network: ${config.proxy ? `proxy ${config.proxy}` : "direct (no proxy)"}`}
        activeColor="blue"
        isActive={!!config.proxy}
        open={openDropdown === "network"}
        onToggle={() => toggle("network")}
        onClose={close}
        width="w-[260px]"
      >
        <div className="px-3 py-1.5 text-xs text-zinc-300">
          {config.proxy === "gateway"
            ? "Rotating gateway: every session gets its own exit IP."
            : config.proxy === "list"
              ? "Proxy list: every session takes the next proxy."
              : "Direct: the browser uses this PC's own connection."}
          {onManageProxies && (
            <button
              type="button"
              onClick={() => {
                close();
                onManageProxies();
              }}
              className="mt-1.5 w-full inline-flex items-center justify-center gap-1.5 rounded border border-zinc-700 py-1 text-[11px] text-zinc-300 hover:bg-zinc-800"
            >
              <SlidersHorizontal size={12} /> Manage proxies…
            </button>
          )}
        </div>
        <Toggle label="Match timezone & language to exit IP" checked={settings.geoMatch} onChange={(v) => update({ geoMatch: v })} />
        <NumberRow label="Identities to try on a block" value={settings.maxAttempts} min={1} max={10} onChange={(v) => update({ maxAttempts: v })} />
      </IconDropdown>

      {/* Review */}
      <IconDropdown
        icon={<Scale size={16} />}
        title={settings.review ? "Review: on (each run is judged)" : "Review: off"}
        activeColor="green"
        isActive={settings.review}
        open={openDropdown === "review"}
        onToggle={() => toggle("review")}
        onClose={close}
        width="w-[280px]"
      >
        <Toggle
          label="Review each run"
          hint="After a run, an LLM judges what went wrong and how to fix it, saves it with the run, and hands it to the next run. One extra LLM call per run."
          checked={settings.review}
          onChange={(v) => update({ review: v })}
        />
      </IconDropdown>
    </div>
  );
}
