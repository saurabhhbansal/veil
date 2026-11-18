"use client";

// The composer's "create accounts" control: a platform, how many accounts, and (in the prompt box) anything
// extra each account should do. Every account is its own run, with its own person, a fresh browser identity
// and a single-account instruction: the agent is never told how many accounts the request makes.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Minus, Plus, UserPlus, X } from "lucide-react";
import { useRun } from "@/context/run-context";
import { useSettings } from "@/context/settings-context";
import { api } from "@/lib/api";
import type { AccountsState } from "@/lib/types";

export interface AccountRequest {
  on: boolean;
  url: string;
  count: number;
}

export const NO_ACCOUNT_REQUEST: AccountRequest = { on: false, url: "", count: 1 };

/** The bare site a URL points at, the same way the server's site_of() reads it. */
function siteOf(url: string): string {
  try {
    return new URL(url.includes("://") ? url : `https://${url}`).hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}

const inputCls = "bg-zinc-800 text-zinc-200 text-[11px] rounded-md px-2 py-1 outline-none placeholder-zinc-500 border border-zinc-700";

export function CreateAccounts({
  value,
  onChange,
  disabled,
}: {
  value: AccountRequest;
  onChange: (v: AccountRequest) => void;
  disabled?: boolean;
}) {
  const { config } = useSettings();
  const { snap } = useRun();
  const [accounts, setAccounts] = useState<AccountsState | null>(null);
  const running = snap.sessions.filter((s) => s.busy).length; // an account run just ended: the log changed
  const max = config?.max_bulk ?? 25;
  const platforms = config?.platforms ?? [];

  const load = useCallback(async () => {
    try {
      setAccounts(await api.accounts());
    } catch {
      setAccounts(null); // the hint is optional; the server still validates
    }
  }, []);

  useEffect(() => {
    if (value.on) void load();
  }, [value.on, running, load]);

  const platform = platforms.find((p) => p.url === value.url);
  const site = siteOf(value.url);
  const done = useMemo(() => new Set(accounts?.done_statuses ?? []), [accounts]);
  // People who don't have an account on this site yet: one per new account (the server checks this again).
  const free = useMemo(() => {
    if (!accounts) return null;
    return accounts.profiles.filter((p) => !accounts.signups.some((s) => s.site === site && s.profile_id === p.id && done.has(s.status))).length;
  }, [accounts, site, done]);

  if (!config) return null;

  function setCount(n: number) {
    if (Number.isNaN(n)) return;
    onChange({ ...value, count: Math.max(1, Math.min(max, n)) });
  }

  if (!value.on) {
    return (
      <button
        type="button"
        onClick={() => onChange({ ...value, on: true, url: value.url || platforms[0]?.url || "" })}
        disabled={disabled}
        title="Create one or more accounts on a site, each in its own fresh browser"
        className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700/80 px-2 py-1 text-[11px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40"
      >
        <UserPlus className="w-3.5 h-3.5" /> Create accounts
      </button>
    );
  }

  const identityApi = accounts?.identity_api;
  const noProfiles = !!accounts && !identityApi && accounts.profiles.length === 0;
  const short = free !== null && !identityApi && free < value.count;
  const total = accounts?.profiles.length ?? 0;
  let hint: string;
  if (!site) hint = "Pick the platform, or type the site to create accounts on.";
  else if (identityApi) hint = `People come from the identity server at ${identityApi}: each account is someone new there.`;
  else if (noProfiles) hint = "Add a profile under Accounts first: each account signs up as one of your emails.";
  else if (free === 0) hint = `Every profile already has an account on ${site}: each new account needs a profile without one.`;
  else if (free !== null) {
    hint = `${free} of ${total} profile${total === 1 ? "" : "s"} ${free === 1 ? "is" : "are"} free for ${site}${
      short ? `: only ${free} new account${free === 1 ? "" : "s"} possible there` : " (each account uses a different one)"
    }.`;
  } else hint = "Each account gets its own fresh browser, device and IP.";
  const warn = short || noProfiles;

  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="inline-flex items-center gap-1 rounded-md bg-emerald-500/10 px-2 py-1 text-[11px] text-emerald-300">
          <UserPlus className="w-3.5 h-3.5" /> Create accounts
        </span>
        <select
          aria-label="Platform"
          value={platform?.url ?? "custom"}
          onChange={(e) =>
            onChange({
              ...value,
              // "Other site…" clears a listed platform so there's a box to type in; a typed site is kept.
              url: e.target.value === "custom" ? (platform ? "" : value.url) : e.target.value,
            })
          }
          className={`${inputCls} [color-scheme:dark] max-w-[180px]`}
        >
          {platforms.map((p) => (
            <option key={p.url} value={p.url}>
              {p.name}
            </option>
          ))}
          <option value="custom">Other site…</option>
        </select>
        {!platform && (
          <input
            value={value.url}
            onChange={(e) => onChange({ ...value, url: e.target.value })}
            placeholder="example.com/register"
            aria-label="Site to create accounts on"
            className={`${inputCls} w-[190px]`}
          />
        )}
        <div className="flex items-center rounded-md border border-zinc-700 bg-zinc-800">
          <button
            type="button"
            onClick={() => setCount(value.count - 1)}
            disabled={value.count <= 1}
            aria-label="One fewer account"
            className="px-1.5 py-1 text-zinc-400 hover:text-zinc-100 disabled:opacity-30"
          >
            <Minus className="w-3 h-3" />
          </button>
          <input
            type="number"
            min={1}
            max={max}
            value={value.count}
            onChange={(e) => setCount(parseInt(e.target.value, 10))}
            aria-label="How many accounts"
            className="w-9 bg-transparent text-center text-[12px] text-zinc-100 outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
          />
          <button
            type="button"
            onClick={() => setCount(value.count + 1)}
            disabled={value.count >= max}
            aria-label="One more account"
            className="px-1.5 py-1 text-zinc-400 hover:text-zinc-100 disabled:opacity-30"
          >
            <Plus className="w-3 h-3" />
          </button>
        </div>
        <span className="text-[11px] text-zinc-500">{value.count === 1 ? "account" : "accounts"}</span>
        <button
          type="button"
          onClick={() => onChange({ ...value, on: false })}
          aria-label="Close create accounts"
          title="Back to a single task"
          className="ml-auto text-zinc-500 hover:text-zinc-200"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <p className={`text-[10px] leading-relaxed ${warn ? "text-amber-400" : "text-zinc-500"}`}>
        {hint} Every run is told to create one account: it never learns how many you asked for.
        {value.url ? " Anything in the box goes with each account." : ""}
      </p>
    </div>
  );
}
