"use client";

// Adapted from chat-ui-example's model-selector.tsx (IconDropdown / DropdownRow). The Browser Use Cloud
// pickers (BU models, cloud profiles, workspaces, proxy countries) are replaced by veil's run settings.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Cpu, Fingerprint, Globe, KeyRound, Loader2, Scale, ShieldCheck, SlidersHorizontal, Trash2 } from "lucide-react";
import { useSettings } from "@/context/settings-context";
import { api } from "@/lib/api";
import type { CaptchaState, KeyInfo, KeysState } from "@/lib/types";

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

function DropdownRow({
  label,
  hint,
  selected,
  onSelect,
  onDelete,
}: {
  label: string;
  hint?: string;
  selected: boolean;
  onSelect: () => void;
  onDelete?: () => void;
}) {
  return (
    <div className={`w-full flex items-center gap-1 hover:bg-zinc-800 ${selected ? "text-white font-medium" : "text-zinc-400"}`}>
      <button type="button" onClick={onSelect} className="flex-1 min-w-0 text-left pl-3 py-1.5 text-xs flex items-center gap-2">
        <span className="flex-1 min-w-0">
          <span className="block truncate">{label}</span>
          {hint && <span className="block truncate text-[10px] text-zinc-500 font-normal">{hint}</span>}
        </span>
        {selected && <Check size={12} className="text-zinc-400 flex-shrink-0" />}
      </button>
      {onDelete && (
        <button
          type="button"
          onClick={onDelete}
          title={`Delete the saved identity "${label}"`}
          aria-label={`Delete saved identity ${label}`}
          className="shrink-0 px-2 py-2 text-zinc-600 hover:text-red-400"
        >
          <Trash2 size={13} />
        </button>
      )}
    </div>
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

/** The server's rule for an API key: printable, no spaces or quotes, 8-512 characters. */
const KEY_OK = /^[\x21-\x7e]{8,512}$/;
const keyLooksValid = (value: string) => KEY_OK.test(value) && !value.includes('"') && !value.includes("'");

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
function KeyRow({
  provider,
  info,
  draft,
  onDraft,
  onDraftBlur,
  busy,
  msg,
  onSave,
  onRemove,
}: {
  provider: string;
  info: KeyInfo;
  draft: string;
  onDraft: (value: string) => void;
  onDraftBlur: () => void;
  busy: boolean;
  msg: { ok: boolean; text: string } | null;
  onSave: () => void;
  onRemove: () => void;
}) {
  return (
    // Not a <form>: the settings bar sits in the composer's footer, inside the composer's form, and a form
    // nested in a form submits natively (the whole page reloads, losing the key). Enter and Save call onSave.
    <div className="px-3 py-2 space-y-1.5 border-t border-zinc-800">
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
          onChange={(e) => onDraft(e.target.value)}
          onBlur={onDraftBlur}
          onKeyDown={(e) => {
            if (e.key === "Enter" && draft.trim()) {
              e.preventDefault();
              onSave();
            }
          }}
          placeholder={info.set ? "Paste a new key to replace it" : `Paste your ${provider} key`}
          className="flex-1 min-w-0 bg-zinc-800 text-zinc-200 text-xs rounded px-2 py-1.5 outline-none placeholder-zinc-500"
        />
        <button type="button" onClick={onSave} disabled={!draft.trim() || busy} className="px-2 text-xs rounded bg-zinc-700 text-zinc-200 disabled:opacity-40">
          {busy ? <Loader2 size={12} className="animate-spin" /> : "Save"}
        </button>
      </div>
      <p className={`text-[10px] ${msg ? (msg.ok ? "text-green-400" : "text-red-400") : "text-zinc-500"}`}>
        {msg?.text ?? `Written to your .env as ${info.var} when you leave this box (or press Save / Save key & make default). Keys never come back to this page.`}
        {!msg && info.set && info.in_file && (
          <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={onRemove} className="ml-1 underline hover:text-zinc-300">
            Remove
          </button>
        )}
      </p>
    </div>
  );
}

export function SettingsBar({ disabled, onManageProxies }: { disabled?: boolean; onManageProxies?: () => void }) {
  const { config, settings, update, refreshConfig } = useSettings();
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const [newIdentity, setNewIdentity] = useState("");
  const [keys, setKeys] = useState<KeysState | null>(null);
  const [defaultMsg, setDefaultMsg] = useState<string | null>(null);
  const [keyDraft, setKeyDraft] = useState("");
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyMsg, setKeyMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [identityMsg, setIdentityMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [captcha, setCaptcha] = useState<CaptchaState | null>(null);
  const [captchaKey, setCaptchaKey] = useState("");
  const [captchaBusy, setCaptchaBusy] = useState(false);
  const [captchaMsg, setCaptchaMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const keyDraftRef = useRef("");
  const keyBusyRef = useRef(false);
  const keyInFlight = useRef<string | null>(null);
  const saveKeyRef = useRef<(value: string) => Promise<boolean>>(async () => false);
  const modelWasOpen = useRef(false);

  useEffect(() => {
    keyDraftRef.current = keyDraft;
  }, [keyDraft]);

  // Leaving the model menu without pressing Save must not throw a typed key away: it is a setting, like the
  // model name (which is remembered as you type). Saved as soon as the menu closes.
  useEffect(() => {
    if (modelWasOpen.current && openDropdown !== "llm") {
      const draft = keyDraftRef.current.trim();
      if (draft && keyLooksValid(draft) && !keyBusyRef.current) void saveKeyRef.current(draft);
    }
    modelWasOpen.current = openDropdown === "llm";
  }, [openDropdown]);

  useEffect(() => {
    if (openDropdown === "llm") {
      setDefaultMsg(null);
      setKeyDraft("");
      setKeyMsg(null);
      api.keys().then(setKeys).catch(() => setKeys(null));
      return;
    }
    if (openDropdown !== "captcha") return;
    setCaptchaKey("");
    setCaptchaMsg(null);
    api.captcha().then(setCaptcha).catch(() => setCaptcha(null));
  }, [openDropdown]);

  const toggle = useCallback((name: string) => setOpenDropdown((prev) => (prev === name ? null : name)), []);
  const close = useCallback(() => setOpenDropdown(null), []);

  if (!config || !settings) {
    return <span className="text-[11px] text-zinc-600 px-1">Connecting to veil…</span>;
  }

  /** Save a provider key to .env ("" removes it). True when it was written (or is already being saved). */
  const saveKey = async (value: string): Promise<boolean> => {
    if (keyBusyRef.current) return keyInFlight.current === value; // a blur and the Save button can both fire for one key
    keyBusyRef.current = true;
    keyInFlight.current = value;
    setKeyBusy(true);
    setKeyMsg(null);
    try {
      const saved = await api.saveKey(settings.provider, value);
      setKeys(saved);
      setKeyDraft("");
      setKeyMsg({
        ok: true,
        text: value ? `Saved to .env as ${saved.keys[settings.provider]?.var ?? "the provider key"}` : "Removed from .env",
      });
      await refreshConfig();
      return true;
    } catch (e) {
      setKeyMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
      return false;
    } finally {
      keyBusyRef.current = false;
      keyInFlight.current = null;
      setKeyBusy(false);
    }
  };
  saveKeyRef.current = saveKey;

  /** Leaving the key box saves what was typed (the model name never needed a Save button). */
  const saveKeyOnBlur = () => {
    const draft = keyDraftRef.current.trim();
    if (!draft) return;
    if (!keyLooksValid(draft)) {
      setKeyMsg({ ok: false, text: "That doesn't look like an API key (no spaces or quotes, 8-512 characters)." });
      return;
    }
    if (!keyBusyRef.current) void saveKey(draft);
  };

  /** One button for the whole model setup: a key typed into the box above is saved too, not silently dropped. */
  const makeDefault = async () => {
    const draft = keyDraft.trim();
    setDefaultMsg(null);
    if (draft && !(await saveKey(draft))) return; // the key row shows why
    try {
      setKeys(await api.saveLlmDefault(settings.provider, settings.model, settings.effort));
      await refreshConfig();
      setDefaultMsg(draft ? "Saved the key and made this the default in .env" : "Saved as the default in .env");
    } catch (e) {
      setDefaultMsg(e instanceof Error ? e.message : String(e));
    }
  };

  const removeIdentity = async (name: string) => {
    if (!window.confirm(`Delete the saved identity "${name}"? Its device, exit IP and cookies are removed. Accounts it signed up for stay on their sites.`)) {
      return;
    }
    setIdentityMsg(null);
    try {
      await api.deleteIdentity(name);
      if (settings.identity === name) update({ identity: "" });
      await refreshConfig();
      setIdentityMsg({ ok: true, text: `Deleted "${name}".` });
    } catch (e) {
      setIdentityMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  };

  const saveCaptcha = async (provider: string, key?: string) => {
    setCaptchaBusy(true);
    setCaptchaMsg(null);
    try {
      const st = await api.saveCaptcha(provider, key);
      setCaptcha(st);
      setCaptchaKey("");
      await refreshConfig();
      setCaptchaMsg({ ok: true, text: provider ? `${provider} is on: CAPTCHAs are solved automatically.` : "CAPTCHA solving off: CAPTCHAs come to you." });
    } catch (e) {
      setCaptchaMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setCaptchaBusy(false);
    }
  };

  const captchaProvider = captcha?.provider ?? config.captcha.provider;
  const captchaHasKey = captcha?.has_key ?? config.captcha.has_key;

  const identities = Array.from(new Set([...config.identities, ...(settings.identity ? [settings.identity] : [])]));
  const identityOk = /^[A-Za-z0-9_.-]{1,64}$/.test(newIdentity);
  const useNewIdentity = () => {
    if (!identityOk) return;
    update({ identity: newIdentity });
    setNewIdentity("");
    close();
  };

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
            onSelect={() => {
              update({ provider: p, model: p === config.llm_provider ? config.llm_model : config.models[p]?.[0] ?? "", effort: p === config.llm_provider ? config.llm_effort ?? "" : "" });
              setKeyDraft("");
              setKeyMsg(null);
              setDefaultMsg(null);
            }}
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
        {keys?.keys[settings.provider] ? (
          <KeyRow
            provider={settings.provider}
            info={keys.keys[settings.provider]}
            draft={keyDraft}
            onDraft={setKeyDraft}
            onDraftBlur={saveKeyOnBlur}
            busy={keyBusy}
            msg={keyMsg}
            onSave={() => void saveKey(keyDraft.trim())}
            onRemove={() => void saveKey("")}
          />
        ) : keys && settings.provider === "openai-compatible" ? (
          <p className="px-3 py-1.5 text-[10px] text-zinc-500">Key and URL come from VEIL_LLM_API_KEY / VEIL_LLM_BASE_URL in .env.</p>
        ) : null}
        <div className="px-3 py-2 border-t border-zinc-800 flex items-center gap-2">
          <button
            type="button"
            onClick={() => void makeDefault()}
            disabled={!settings.model.trim()}
            className="text-[11px] px-2 py-1 rounded border border-zinc-700 text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            title="Write provider, model, effort and any key you typed to .env (VEIL_LLM_PROVIDER / _MODEL / _EFFORT / <provider>_API_KEY)"
          >
            {keyDraft.trim() ? "Save key & make default" : "Make default"}
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
            onDelete={() => void removeIdentity(name)}
          />
        ))}
        {identityMsg && (
          <p className={`px-3 py-1 text-[10px] ${identityMsg.ok ? "text-green-400" : "text-red-400"}`}>{identityMsg.text}</p>
        )}
        {/* Not a <form>: nested in the composer's form it would submit and reload the page (see KeyRow). */}
        <div className="p-2 border-t border-zinc-800 flex gap-1">
          <input
            value={newIdentity}
            onChange={(e) => setNewIdentity(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && identityOk) {
                e.preventDefault();
                useNewIdentity();
              }
            }}
            placeholder="New sticky identity…"
            className="flex-1 min-w-0 bg-zinc-800 text-zinc-200 text-xs rounded px-2 py-1.5 outline-none placeholder-zinc-500"
          />
          <button type="button" onClick={useNewIdentity} disabled={!identityOk} className="px-2 text-xs rounded bg-zinc-700 text-zinc-200 disabled:opacity-40">
            Use
          </button>
        </div>
      </IconDropdown>

      {/* Human checks: CAPTCHA solving, invisible to the agent */}
      <IconDropdown
        icon={<ShieldCheck size={16} />}
        title={captchaProvider ? `Human checks: solving via ${captchaProvider}` : "Human checks: ask me"}
        activeColor={captchaProvider ? "green" : "purple"}
        isActive={!!captchaProvider}
        open={openDropdown === "captcha"}
        onToggle={() => toggle("captcha")}
        onClose={close}
        width="w-[300px]"
      >
        <p className="px-3 pt-1.5 text-[10px] text-zinc-500">
          CAPTCHAs and bot checks are solved in the background; the agent is never told. Without a solver they pause for you.
        </p>
        <DropdownRow label="Ask me (off)" hint="CAPTCHAs and challenges come to you" selected={!captchaProvider} onSelect={() => void saveCaptcha("")} />
        {(captcha?.providers ?? ["2captcha", "capsolver"]).map((p) => (
          <DropdownRow key={p} label={p} hint="solves CAPTCHAs through its API" selected={captchaProvider === p} onSelect={() => void saveCaptcha(p)} />
        ))}
        {captchaProvider && (
          <>
            <div className="px-3 pt-1.5 text-[10px] text-zinc-500">
              API key {captchaHasKey ? `set ${captcha?.masked ?? ""}` : "not set"}
            </div>
            <div className="px-3 pb-1 flex gap-1">
              <input
                type="password"
                autoComplete="off"
                value={captchaKey}
                onChange={(e) => setCaptchaKey(e.target.value)}
                placeholder={`Paste your ${captchaProvider} key`}
                className="flex-1 min-w-0 bg-zinc-800 text-zinc-200 text-xs rounded px-2 py-1.5 outline-none placeholder-zinc-500"
              />
              <button
                type="button"
                disabled={!captchaKey.trim() || captchaBusy}
                onClick={() => void saveCaptcha(captchaProvider, captchaKey.trim())}
                className="px-2 text-xs rounded bg-zinc-700 text-zinc-200 disabled:opacity-40"
              >
                {captchaBusy ? <Loader2 size={12} className="animate-spin" /> : "Save"}
              </button>
            </div>
            <p className={`px-3 pb-2 text-[10px] ${captchaMsg ? (captchaMsg.ok ? "text-green-400" : "text-red-400") : "text-zinc-500"}`}>
              {captchaMsg?.text ?? "Saved to .env. Keys never come back to this page."}
            </p>
          </>
        )}
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
