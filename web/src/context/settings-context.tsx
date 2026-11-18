"use client";

// Adapted from chat-ui-example's settings-context: instead of Browser Use Cloud models, profiles,
// workspaces and proxy countries, these are veil's own run settings. Defaults come from the server's
// .env (GET /api/config); a choice made here is remembered in localStorage. API keys never pass through here
// (they're saved to .env from the model menu, straight to the server).
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import type { VeilConfig } from "@/lib/types";

export interface VeilSettings {
  provider: string;
  model: string;
  effort: string; // "" = the provider's default
  identity: string; // "" = a fresh device and exit IP every run
  geoMatch: boolean;
  maxAttempts: number;
  maxSteps: number;
  useVision: boolean;
  review: boolean; // judge each run afterwards and pass the review to the next run
}

interface SettingsContextType {
  config: VeilConfig | null;
  configError: string | null;
  settings: VeilSettings | null;
  update: (patch: Partial<VeilSettings>) => void;
  reset: () => void;
  uiValues: () => Record<string, unknown>;
  refreshConfig: () => Promise<void>;
}

const STORAGE_KEY = "veil-settings";
const SettingsContext = createContext<SettingsContextType | null>(null);

function fromConfig(c: VeilConfig): VeilSettings {
  return {
    provider: c.llm_provider,
    model: c.llm_model,
    effort: c.llm_effort ?? "",
    identity: "",
    geoMatch: c.geo_match,
    maxAttempts: c.max_attempts,
    maxSteps: c.max_steps,
    useVision: true,
    review: false,
  };
}

/** A remembered effort the current provider doesn't have falls back to its default. */
function withValidEffort(s: VeilSettings, c: VeilConfig): VeilSettings {
  return s.effort && !(c.efforts[s.provider] ?? []).includes(s.effort) ? { ...s, effort: "" } : s;
}

function load(): Partial<VeilSettings> {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<VeilConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Partial<VeilSettings>>({});

  useEffect(() => {
    setOverrides(load());
    let cancelled = false;
    const fetchConfig = () =>
      api
        .config()
        .then((c) => {
          if (!cancelled) {
            setConfig(c);
            setConfigError(null);
          }
        })
        .catch((e) => {
          if (cancelled) return;
          setConfigError(e instanceof Error ? e.message : String(e));
          setTimeout(fetchConfig, 2000);
        });
    fetchConfig();
    return () => {
      cancelled = true;
    };
  }, []);

  const settings = config ? withValidEffort({ ...fromConfig(config), ...overrides }, config) : null;

  const update = useCallback((patch: Partial<VeilSettings>) => {
    setOverrides((prev) => {
      const next = { ...prev, ...patch };
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* private window: settings just aren't remembered */
      }
      return next;
    });
  }, []);

  const refreshConfig = useCallback(async () => {
    try {
      setConfig(await api.config());
    } catch {
      /* keep the last one */
    }
  }, []);

  const reset = useCallback(() => {
    setOverrides({});
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }, []);

  // The ids settings_from_ui() reads on the server.
  const uiValues = useCallback((): Record<string, unknown> => {
    if (!settings) return {};
    return {
      "agent_settings.llm_provider": settings.provider,
      "agent_settings.llm_model": settings.model,
      "agent_settings.llm_effort": settings.effort,
      "agent_settings.max_steps": settings.maxSteps,
      "agent_settings.use_vision": settings.useVision,
      "stealth_settings.identity": settings.identity,
      "stealth_settings.max_attempts": settings.maxAttempts,
      "stealth_settings.geo_match": settings.geoMatch,
    };
  }, [settings]);

  return (
    <SettingsContext.Provider value={{ config, configError, settings, update, reset, uiValues, refreshConfig }}>
      {children}
    </SettingsContext.Provider>
  );
}

export function useSettings() {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be inside SettingsProvider");
  return ctx;
}
