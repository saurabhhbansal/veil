"use client";

// Adapted from chat-ui-example's model-selector.tsx (IconDropdown / DropdownRow). The Browser Use Cloud
// pickers (BU models, cloud profiles, workspaces, proxy countries) are replaced by veil's run settings.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Cpu, Fingerprint, Globe, Monitor, ShieldCheck } from "lucide-react";
import { useSettings } from "@/context/settings-context";

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
        <div className={`absolute bottom-full mb-1 right-0 ${width} bg-zinc-900 border border-zinc-700 rounded-lg shadow-xl z-50 flex flex-col max-h-[360px] overflow-y-auto`}>
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

export function SettingsBar({ disabled }: { disabled?: boolean }) {
  const { config, settings, update } = useSettings();
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const [newIdentity, setNewIdentity] = useState("");

  const toggle = useCallback((name: string) => setOpenDropdown((prev) => (prev === name ? null : name)), []);
  const close = useCallback(() => setOpenDropdown(null), []);

  if (!config || !settings) {
    return <span className="text-[11px] text-zinc-600 px-1">Connecting to veil…</span>;
  }

  const identities = Array.from(new Set([...config.identities, ...(settings.identity ? [settings.identity] : [])]));
  const identityOk = /^[A-Za-z0-9_.-]{1,64}$/.test(newIdentity);

  return (
    <div className={`flex items-center gap-1 ${disabled ? "opacity-50 pointer-events-none" : ""}`}>
      {/* LLM */}
      <IconDropdown
        icon={<Cpu size={16} />}
        title={`LLM: ${settings.provider} / ${settings.model || "(model?)"}`}
        activeColor="amber"
        isActive
        open={openDropdown === "llm"}
        onToggle={() => toggle("llm")}
        onClose={close}
      >
        {config.providers.map((p) => (
          <DropdownRow
            key={p}
            label={p}
            hint={p === config.llm_provider ? `from .env${config.has_api_key ? " · key set" : ""}` : undefined}
            selected={settings.provider === p}
            onSelect={() => update({ provider: p, model: p === config.llm_provider ? config.llm_model : "" })}
          />
        ))}
        <div className="p-2 border-t border-zinc-800">
          <input
            value={settings.model}
            onChange={(e) => update({ model: e.target.value })}
            placeholder="Model name"
            className="w-full bg-zinc-800 text-zinc-200 text-xs rounded px-2 py-1.5 outline-none placeholder-zinc-500"
          />
          {settings.provider !== config.llm_provider && (
            <p className="mt-1.5 text-[10px] text-zinc-500">Uses that provider&apos;s own key from .env (e.g. GROQ_API_KEY).</p>
          )}
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
            ? "Proxy gateway from .env (VEIL_PROXY_GATEWAY)"
            : config.proxy === "list"
              ? `Proxy list: ${config.proxy_detail}`
              : "No proxy configured: the browser uses this PC's own connection."}
          <span className="block text-[10px] text-zinc-500 mt-0.5">Proxies are set in .env; credentials never reach this page.</span>
        </div>
        <Toggle label="Match timezone & language to exit IP" checked={settings.geoMatch} onChange={(v) => update({ geoMatch: v })} />
        <NumberRow label="Identities to try on a block" value={settings.maxAttempts} min={1} max={10} onChange={(v) => update({ maxAttempts: v })} />
      </IconDropdown>

      {/* Window */}
      <IconDropdown
        icon={<Monitor size={16} />}
        title={settings.headful ? "Window: shown on this PC" : "Window: hidden (headless)"}
        activeColor="purple"
        isActive={settings.headful}
        open={openDropdown === "window"}
        onToggle={() => toggle("window")}
        onClose={close}
        width="w-[260px]"
      >
        <Toggle
          label="Also show the real browser window"
          hint="The live view on the right works either way"
          checked={settings.headful}
          onChange={(v) => update({ headful: v })}
        />
      </IconDropdown>
    </div>
  );
}
