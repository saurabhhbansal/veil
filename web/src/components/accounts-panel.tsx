"use client";

// Side panel for `veil signup` / `veil accounts` in the app: edit your own sign-up profiles (profiles.csv)
// and see where each was used (signups.csv). Passwords stay on the server: the page only learns whether one
// is set, and a sign-up's password is fetched on request ("Show password").
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Check, ChevronDown, Copy, Eye, KeyRound, Loader2, LogIn, Plus, RotateCw, Save, Trash2, UserPlus, X,
} from "lucide-react";
import { useRun } from "@/context/run-context";
import { api } from "@/lib/api";
import type { AccountsState, ProfileDraft, SignupRow } from "@/lib/types";

const FIELDS: { key: keyof ProfileDraft; label: string; type?: string; placeholder?: string; wide?: boolean }[] = [
  { key: "id", label: "Profile id", placeholder: "e.g. asha" },
  { key: "email", label: "Email", type: "email", placeholder: "you@example.com" },
  { key: "username", label: "Username", placeholder: "if a site asks for one" },
  { key: "first_name", label: "First name" },
  { key: "last_name", label: "Last name" },
  { key: "dob", label: "Date of birth", type: "date" },
  { key: "gender", label: "Gender", placeholder: "as you'd answer on forms" },
  { key: "phone", label: "Phone", type: "tel", placeholder: "+91 …" },
  { key: "country", label: "Country" },
  { key: "address", label: "Address", wide: true },
  { key: "city", label: "City" },
  { key: "state", label: "State / region" },
  { key: "postcode", label: "Postcode" },
];

const STATUS_STYLE: Record<string, string> = {
  signed_up: "bg-green-500/15 text-green-400",
  logged_in: "bg-green-500/15 text-green-400",
  verify_email: "bg-blue-500/15 text-blue-400",
  already_registered: "bg-zinc-500/20 text-zinc-300",
  needs_human: "bg-amber-500/15 text-amber-400",
  failed: "bg-red-500/15 text-red-400",
};

let keySeq = 0;
const newKey = () => `p${Date.now()}-${keySeq++}`;

function toDrafts(state: AccountsState): ProfileDraft[] {
  return state.profiles.map((p) => ({ ...p, original_id: p.id, key: newKey() }));
}

function Field({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <label className={`flex flex-col gap-1 ${wide ? "col-span-2" : ""}`}>
      <span className="text-[11px] text-zinc-500">{label}</span>
      {children}
    </label>
  );
}

const inputCls =
  "w-full bg-zinc-900 border border-zinc-800 rounded-md px-2.5 py-1.5 text-[13px] text-zinc-200 outline-none focus:border-zinc-500 placeholder-zinc-600";

function ProfileCard({
  draft,
  open,
  onToggle,
  onChange,
  onDelete,
}: {
  draft: ProfileDraft;
  open: boolean;
  onToggle: () => void;
  onChange: (patch: Partial<ProfileDraft>) => void;
  onDelete: () => void;
}) {
  const name = [draft.first_name, draft.last_name].filter(Boolean).join(" ");
  const passwordState =
    draft.password === undefined ? (draft.has_password ? "saved" : "random per site") : draft.password === "" ? "random per site" : "new";
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/40">
      <button type="button" onClick={onToggle} className="w-full flex items-center gap-3 px-3 py-2.5 text-left">
        <div className="w-8 h-8 rounded-full bg-zinc-800 flex items-center justify-center text-[12px] text-zinc-300 font-medium shrink-0">
          {(draft.first_name || draft.id || "?").slice(0, 1).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-[13px] text-zinc-200 font-medium truncate">
            {draft.id || <span className="text-zinc-500">new profile</span>}
            {name && <span className="text-zinc-500 font-normal"> · {name}</span>}
          </p>
          <p className="text-[12px] text-zinc-500 truncate">{draft.email || "no email yet"}</p>
        </div>
        <ChevronDown className={`w-4 h-4 text-zinc-500 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3">
          <div className="grid grid-cols-2 gap-2.5">
            {FIELDS.map((f) => (
              <Field key={f.key} label={f.label} wide={f.wide}>
                <input
                  type={f.type ?? "text"}
                  value={(draft[f.key] as string | undefined) ?? ""}
                  placeholder={f.placeholder}
                  onChange={(e) => onChange({ [f.key]: e.target.value })}
                  className={`${inputCls} [color-scheme:dark]`}
                />
              </Field>
            ))}
            <Field label={`Password (${passwordState})`} wide>
              <div className="flex gap-2">
                <input
                  type="password"
                  autoComplete="new-password"
                  value={draft.password ?? ""}
                  placeholder={draft.has_password && draft.password === undefined ? "•••••••• saved: type to replace" : "blank: a new random password per site"}
                  onChange={(e) => onChange({ password: e.target.value })}
                  className={inputCls}
                />
                {(draft.has_password || draft.password) && (
                  <button
                    type="button"
                    onClick={() => onChange({ password: "", has_password: false })}
                    className="shrink-0 px-2.5 rounded-md border border-zinc-800 text-[12px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800"
                    title="Use a random password per site instead"
                  >
                    Clear
                  </button>
                )}
              </div>
            </Field>
          </div>
          <button type="button" onClick={onDelete} className="inline-flex items-center gap-1.5 text-[12px] text-red-400/80 hover:text-red-300">
            <Trash2 className="w-3.5 h-3.5" /> Remove profile
          </button>
        </div>
      )}
    </div>
  );
}

function SignupItem({
  row,
  isDone,
  onRetry,
  onLogin,
  onForget,
  busy,
}: {
  row: SignupRow;
  isDone: boolean;
  onRetry: () => void;
  onLogin: () => void;
  onForget: () => void;
  busy: boolean;
}) {
  const [secret, setSecret] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function show() {
    try {
      setSecret((await api.reveal(row.site, row.profile_id)).password);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 px-3 py-2.5 space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="text-[13px] text-zinc-200 font-medium truncate">{row.site}</span>
        <span className={`text-[11px] px-1.5 py-0.5 rounded ${STATUS_STYLE[row.status] ?? STATUS_STYLE.failed}`}>{row.status.replace(/_/g, " ")}</span>
        <span className="ml-auto text-[11px] text-zinc-600 shrink-0">{row.updated}</span>
      </div>
      <p className="text-[12px] text-zinc-500 truncate">
        {row.email} · profile {row.profile_id}
      </p>
      {row.note && <p className="text-[12px] text-zinc-400 break-words">{row.note}</p>}
      <div className="flex items-center gap-3 pt-0.5">
        {row.has_password &&
          (secret === null ? (
            <button onClick={show} className="inline-flex items-center gap-1 text-[12px] text-zinc-400 hover:text-zinc-200">
              <Eye className="w-3.5 h-3.5" /> Show password
            </button>
          ) : (
            <span className="inline-flex items-center gap-1.5 text-[12px]">
              <KeyRound className="w-3.5 h-3.5 text-zinc-500" />
              <code className="text-zinc-200 bg-zinc-800 rounded px-1.5 py-0.5 select-all">{secret}</code>
              <button
                onClick={() => {
                  void navigator.clipboard?.writeText(secret);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1200);
                }}
                className="text-zinc-400 hover:text-zinc-200"
                title="Copy"
              >
                {copied ? <Check className="w-3.5 h-3.5 text-green-400" /> : <Copy className="w-3.5 h-3.5" />}
              </button>
              <button onClick={() => setSecret(null)} className="text-zinc-500 hover:text-zinc-300" title="Hide">
                <X className="w-3.5 h-3.5" />
              </button>
            </span>
          ))}
        {isDone && (
          <button
            onClick={onLogin}
            disabled={busy}
            className="inline-flex items-center gap-1 text-[12px] text-zinc-400 hover:text-zinc-200 disabled:opacity-40"
            title="Log in as this account, on its own device and IP"
          >
            <LogIn className="w-3.5 h-3.5" /> Log in
          </button>
        )}
        {!isDone && (
          <button
            onClick={onRetry}
            disabled={busy}
            className="inline-flex items-center gap-1 text-[12px] text-zinc-400 hover:text-zinc-200 disabled:opacity-40"
            title="Try again as the same person (same profile, password, device and IP)"
          >
            <RotateCw className="w-3.5 h-3.5" /> Retry
          </button>
        )}
        {err && <span className="text-[12px] text-red-400">{err}</span>}
        <button
          onClick={onForget}
          disabled={busy}
          className="ml-auto inline-flex items-center gap-1 text-[12px] text-zinc-600 hover:text-red-400 disabled:opacity-40"
          title="Forget this sign-up here (the account on the site is not deleted)"
        >
          <Trash2 className="w-3.5 h-3.5" /> Delete
        </button>
      </div>
    </div>
  );
}

export function AccountsPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { snap, startAccount } = useRun();
  const [tab, setTab] = useState<"profiles" | "signups">("profiles");
  const [state, setState] = useState<AccountsState | null>(null);
  const [drafts, setDrafts] = useState<ProfileDraft[]>([]);
  const [dirty, setDirty] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ kind: "error" | "ok"; text: string } | null>(null);
  const [url, setUrl] = useState("");
  const [profileId, setProfileId] = useState("");
  const [action, setAction] = useState<"signup" | "login">("signup");
  const [starting, setStarting] = useState(false);

  const load = useCallback(async (keepDrafts: boolean) => {
    try {
      const s = await api.accounts();
      setState(s);
      if (!keepDrafts) {
        setDrafts(toDrafts(s));
        setDirty(false);
      }
      if (s.error) setMessage({ kind: "error", text: s.error });
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    }
  }, []);

  useEffect(() => {
    if (open) void load(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, load]);

  // An account run just ended (in any session): its status is in the log now.
  const running = snap.sessions.filter((x) => x.busy).length;
  useEffect(() => {
    if (open) void load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const savedIds = useMemo(() => state?.profiles.map((p) => p.id) ?? [], [state]);

  const change = (key: string, patch: Partial<ProfileDraft>) => {
    setDrafts((ds) => ds.map((d) => (d.key === key ? { ...d, ...patch } : d)));
    setDirty(true);
    setMessage(null);
  };

  async function save() {
    setSaving(true);
    setMessage(null);
    try {
      const payload = drafts.map(({ key: _k, has_password: _h, ...rest }) => rest);
      const s = await api.saveProfiles(payload);
      setState(s);
      setDrafts(toDrafts(s));
      setDirty(false);
      setMessage({ kind: "ok", text: `Saved ${s.profiles.length} profile${s.profiles.length === 1 ? "" : "s"}.` });
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }

  async function signup(target: string, pid: string, what: "signup" | "login" = "signup") {
    setStarting(true);
    setMessage(null);
    const ok = await startAccount(what, target, pid);
    setStarting(false);
    if (ok) {
      setUrl("");
      onClose();
    }
  }

  async function forget(site: string, profileId: string) {
    if (!window.confirm(`Forget the sign-up on ${site} as ${profileId}? The account on the site is not deleted.`)) return;
    try {
      setState(await api.deleteSignup(site, profileId));
      setMessage({ kind: "ok", text: `Forgot the sign-up on ${site} as ${profileId}.` });
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    }
  }

  return (
    <>
      <div
        className={`fixed inset-0 bg-black/50 z-40 transition-opacity ${open ? "opacity-100" : "opacity-0 pointer-events-none"}`}
        onClick={onClose}
        aria-hidden
      />
      <aside
        className={`fixed top-0 right-0 h-full w-full sm:w-[460px] bg-zinc-950 border-l border-zinc-800 z-50 flex flex-col transition-transform duration-200 ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
        aria-label="Accounts"
        aria-hidden={!open}
      >
        <header className="h-12 border-b border-zinc-800 flex items-center px-4 gap-3 shrink-0">
          <span className="text-[14px] font-medium text-zinc-200">Accounts</span>
          <div className="flex items-center bg-zinc-900 rounded-lg p-0.5 ml-2">
            {(["profiles", "signups"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-2.5 py-1 rounded-md text-[12px] ${tab === t ? "bg-zinc-700 text-zinc-100" : "text-zinc-400 hover:text-zinc-200"}`}
              >
                {t === "profiles" ? `Profiles${state ? ` (${state.profiles.length})` : ""}` : `Sign-ups${state ? ` (${state.signups.length})` : ""}`}
              </button>
            ))}
          </div>
          <button onClick={onClose} className="ml-auto text-zinc-500 hover:text-zinc-200" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {!state && !message && (
            <div className="flex items-center gap-2 text-sm text-zinc-500">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          )}

          {state && tab === "profiles" && state.identity_api && (
            <>
              <p className="text-[12px] text-zinc-500 leading-relaxed">
                People come from your company identity server at <code className="text-zinc-400">{state.identity_api}</code> (
                <code className="text-zinc-400">VEIL_IDENTITY_API</code>). Each sign-up asks it for someone; edit them there. See
                docs/IDENTITY_API.md.
              </p>
              {state.profiles.map((p) => (
                <div key={p.id} className="rounded-xl border border-zinc-800 bg-zinc-900/40 px-3 py-2 text-[13px] text-zinc-300">
                  <span className="text-zinc-200 font-medium">{p.id}</span> <span className="text-zinc-500">{p.email}</span>
                </div>
              ))}
              {!state.profiles.length && (
                <p className="text-[12px] text-zinc-500">The server lists nobody here; people are assigned per run.</p>
              )}
            </>
          )}

          {state && tab === "profiles" && !state.identity_api && (
            <>
              <p className="text-[12px] text-zinc-500 leading-relaxed">
                Your own details, one profile per email you own. Saved on this PC in <code className="text-zinc-400">{state.profiles_file}</code>.
                The agent only sees placeholders like <code className="text-zinc-400">x_email</code>; the real values are typed only on the site
                you sign up to.
              </p>
              {drafts.map((d) => (
                <ProfileCard
                  key={d.key}
                  draft={d}
                  open={expanded === d.key}
                  onToggle={() => setExpanded(expanded === d.key ? null : d.key)}
                  onChange={(patch) => change(d.key, patch)}
                  onDelete={() => {
                    setDrafts((ds) => ds.filter((x) => x.key !== d.key));
                    setDirty(true);
                  }}
                />
              ))}
              <button
                onClick={() => {
                  const key = newKey();
                  setDrafts((ds) => [...ds, { key, id: "", email: "" }]);
                  setExpanded(key);
                  setDirty(true);
                }}
                className="w-full rounded-xl border border-dashed border-zinc-700 py-2.5 text-[13px] text-zinc-400 hover:text-zinc-200 hover:border-zinc-500 inline-flex items-center justify-center gap-1.5"
              >
                <Plus className="w-4 h-4" /> Add profile
              </button>
            </>
          )}

          {state && tab === "signups" && (
            <>
              <form
                className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-3 space-y-2.5"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (url.trim()) void signup(url.trim(), profileId, action);
                }}
              >
                <div className="flex items-center gap-2">
                  <p className="text-[13px] text-zinc-200 font-medium">{action === "signup" ? "New sign-up" : "Log in"}</p>
                  <div className="ml-auto flex items-center bg-zinc-900 rounded-lg p-0.5">
                    {(["signup", "login"] as const).map((a) => (
                      <button
                        key={a}
                        type="button"
                        onClick={() => setAction(a)}
                        className={`px-2 py-0.5 rounded-md text-[11px] ${action === a ? "bg-zinc-700 text-zinc-100" : "text-zinc-400 hover:text-zinc-200"}`}
                      >
                        {a === "signup" ? "Sign up" : "Log in"}
                      </button>
                    ))}
                  </div>
                </div>
                <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Site, e.g. example.com/register" className={inputCls} />
                <div className="flex gap-2">
                  <select value={profileId} onChange={(e) => setProfileId(e.target.value)} className={`${inputCls} [color-scheme:dark]`}>
                    <option value="">{action === "signup" ? "Random profile" : "The account veil knows"}</option>
                    {savedIds.map((id) => (
                      <option key={id} value={id}>
                        {id}
                      </option>
                    ))}
                  </select>
                  <button
                    type="submit"
                    disabled={!url.trim() || starting || savedIds.length === 0}
                    className="shrink-0 inline-flex items-center gap-1.5 rounded-md bg-zinc-100 hover:bg-white text-zinc-900 text-[13px] font-medium px-3 disabled:opacity-40"
                  >
                    {starting ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : action === "signup" ? (
                      <UserPlus className="w-3.5 h-3.5" />
                    ) : (
                      <LogIn className="w-3.5 h-3.5" />
                    )}{" "}
                    {action === "signup" ? "Sign up" : "Log in"}
                  </button>
                </div>
                <p className="text-[11px] text-zinc-500">
                  {savedIds.length === 0
                    ? "Add and save a profile first."
                    : action === "signup"
                      ? "One account per profile per site, each on its own sticky device and IP. CAPTCHAs and codes come to you in the browser panel. If a task is running, this runs alongside it."
                      : "Uses the password veil saved for that site, or the profile's own password for an account you made yourself."}
                </p>
              </form>
              {state.signups.length === 0 ? (
                <p className="text-[13px] text-zinc-500 px-1">No sign-ups yet.</p>
              ) : (
                state.signups.map((row) => (
                  <SignupItem
                    key={`${row.site}|${row.profile_id}`}
                    row={row}
                    isDone={state.done_statuses.includes(row.status)}
                    busy={starting}
                    onRetry={() => void signup(row.site, row.profile_id)}
                    onLogin={() => void signup(row.site, row.profile_id, "login")}
                    onForget={() => void forget(row.site, row.profile_id)}
                  />
                ))
              )}
            </>
          )}

          {message && (
            <p className={`text-[13px] break-words ${message.kind === "error" ? "text-red-400" : "text-green-400"}`}>{message.text}</p>
          )}
        </div>

        {tab === "profiles" && state && (
          <footer className="border-t border-zinc-800 px-4 py-3 flex items-center gap-3 shrink-0">
            <span className="text-[12px] text-zinc-500">{dirty ? "Unsaved changes" : "All changes saved"}</span>
            {dirty && (
              <button onClick={() => void load(false)} className="text-[12px] text-zinc-400 hover:text-zinc-200">
                Discard
              </button>
            )}
            <button
              onClick={() => void save()}
              disabled={!dirty || saving}
              className="ml-auto inline-flex items-center gap-1.5 rounded-md bg-zinc-100 hover:bg-white text-zinc-900 text-[13px] font-medium px-3 py-1.5 disabled:opacity-40"
            >
              {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />} Save profiles
            </button>
          </footer>
        )}
      </aside>
    </>
  );
}
