// Shapes of the local veil server's API (src/veil/webui/runner.py snapshot() and server.py /api/config).

export type RunStatus = "idle" | "queued" | "starting" | "running" | "paused" | "needs_human" | "stopping" | "reviewing";

export interface ExitInfo {
  ip: string;
  country?: string | null;
  city?: string | null;
  timezone?: string | null;
}

export interface DeviceInfo {
  windows: string;
  machine_class: string;
  gpu: string;
  cores: number;
  memory: number;
  screen: string;
  timezone: string;
  languages: string;
  canvas_seed: number;
  audio_seed: number;
}

export interface Identity {
  attempt: number;
  total: number;
  engine: string;
  headless: boolean;
  proxy: string | null;
  sticky: string | null;
  exit?: ExitInfo;
  device?: DeviceInfo;
}

export interface Attempt {
  proxy: string | null;
  exit_ip: string | null;
  persona: string | null;
  outcome: "done" | "blocked" | "failed" | "stopped" | "error";
  detail: string;
}

export interface StepAction {
  name: string;
  args: Record<string, unknown>;
}

interface BaseEvent {
  id: number;
  time: number;
}

export type RunEvent = BaseEvent &
  (
    | { type: "task"; text: string; run: string; kind?: "task" | "signup" | "login" }
    | { type: "attempt"; identity: Identity }
    | {
        type: "step";
        n: number;
        url: string;
        title: string;
        evaluation?: string | null;
        memory?: string | null;
        next_goal?: string | null;
        actions: StepAction[];
        screenshot?: string | null;
      }
    | { type: "notice"; level: "blocked" | "error" | "info"; text: string }
    | {
        type: "human";
        reason: string;
        source: "user" | "agent" | "detector";
        vendor?: string;
        kind?: string;
        url?: string;
        resolved: boolean;
        note?: string;
        outcome?: string;
      }
    | { type: "result"; ok: boolean; stopped: boolean; output: string | null; attempts: Attempt[] }
    | { type: "steer"; text: string; delivered: boolean; undelivered?: boolean; followup?: boolean }
    | { type: "judgement"; judgement: Judgement; file?: string | null }
  );

export interface Judgement {
  verdict: "success" | "partial" | "failure";
  summary: string;
  what_went_wrong: string[];
  fixes: string[];
  advice_for_next_run: string;
}

export interface RunSummary {
  id: string;
  title: string;
  kind: string;
  started: number | null;
  state: string | null;
  ok: boolean | null;
  has_gif: boolean;
  verdict: Judgement["verdict"] | null;
  gif: string | null;
  group: string | null;
}

export interface RunDetail {
  id: string;
  title: string;
  task: string;
  kind: string;
  started: number | null;
  ended: number | null;
  state: string | null;
  ok: boolean | null;
  events: RunEvent[];
  identity: Identity | null;
  gif: string | null;
  result_file: string | null;
  judgement: Judgement | null;
  judgement_file: string | null;
}

export type EventOf<T extends RunEvent["type"]> = Extract<RunEvent, { type: T }>;

export interface HumanRequest {
  reason: string;
  source: "user" | "agent" | "detector";
  vendor?: string;
  url?: string;
  since: number;
}

/** One session in the app: the chat ("main") or a job split off a request, running side by side. */
export interface SessionSummary {
  id: string;
  group: string | null;
  label: string;
  status: RunStatus;
  busy: boolean;
  run_id: string | null;
  ok: boolean | null;
  needs_human: boolean;
}

export interface Snapshot {
  session: string;
  group: string | null;
  label: string;
  sessions: SessionSummary[];
  skipped?: string[]; // jobs of a split request that couldn't start, and why
  version: number;
  status: RunStatus;
  busy: boolean;
  events: RunEvent[];
  identity: Identity | null;
  human: HumanRequest | null;
  browser_open: boolean;
  gif: string | null;
  result_file: string | null;
  run_id: string | null;
  has_review: boolean;
}

export interface VeilConfig {
  llm_provider: string;
  llm_model: string;
  llm_effort: string | null;
  has_api_key: boolean;
  keys: Record<string, boolean>; // provider -> a key is set (never the key itself)
  providers: string[];
  efforts: Record<string, string[]>; // provider -> effort levels, lowest first
  models: Record<string, string[]>; // provider -> suggested model names
  max_parallel: number;
  headless: boolean;
  geo_match: boolean;
  max_attempts: number;
  max_steps: number;
  identities: string[];
  platforms: Platform[]; // the "create accounts" platform picker
  max_bulk: number; // most accounts one request may ask for
  proxy: "gateway" | "list" | null;
  proxy_detail: string | null;
}

/** A site the "create accounts" control offers: a starting page for its sign-up flow. */
export interface Platform {
  name: string;
  url: string;
}

// ── Chat rendering (adapted from chat-ui-example's turn model) ──

export type ToolStatus = "pending" | "running" | "completed" | "error";

export type ToolCallType =
  | "browse"
  | "click"
  | "type"
  | "scroll"
  | "search"
  | "wait"
  | "screenshot"
  | "extract"
  | "tab"
  | "keys"
  | "file"
  | "done"
  | "human"
  | "unknown";

export interface UIToolCall {
  id: string;
  name: string;
  displayName: string;
  displayValue: string;
  args: Record<string, unknown>;
  status: ToolStatus;
  type: ToolCallType;
}

export interface ConversationTurn {
  id: string;
  task: EventOf<"task">;
  items: RunEvent[]; // everything after the task, in order
  isComplete: boolean;
}

// ── Accounts (GET /api/accounts): your own sign-up details; passwords never come to the page ──

export interface AccountProfile {
  id: string;
  email: string;
  username?: string;
  first_name?: string;
  last_name?: string;
  dob?: string;
  gender?: string;
  phone?: string;
  address?: string;
  city?: string;
  state?: string;
  postcode?: string;
  country?: string;
  has_password: boolean;
}

export interface SignupRow {
  site: string;
  profile_id: string;
  email: string;
  status: string;
  identity: string;
  updated: string;
  note: string;
  has_password: boolean;
}

export interface AccountsState {
  profiles_file: string;
  identity_api: string | null; // set: people come from the company identity server, not profiles_file
  signups_file: string;
  error: string | null;
  columns: string[];
  statuses: string[];
  done_statuses: string[];
  profiles: AccountProfile[];
  signups: SignupRow[];
}

/** A profile being edited. `password` undefined = keep the saved one; "" = clear it. */
export type ProfileDraft = Omit<AccountProfile, "has_password"> & {
  original_id?: string;
  password?: string;
  has_password?: boolean;
  key: string;
};

// ── Settings saved to .env (GET /api/keys, /api/proxies): secrets go in, only masked values come out ──

export interface KeyInfo {
  var: string;
  set: boolean;
  masked: string | null;
  source: string | null;
  in_file: boolean;
}

export interface KeysState {
  env_file: string;
  keys: Record<string, KeyInfo>;
}

export interface ProxyTest {
  ok: boolean;
  ip?: string;
  country?: string;
  city?: string | null;
  timezone?: string;
  error?: string;
  ms: number;
  at: number;
}

export interface ProxyItem {
  id: string;
  server: string; // credentials redacted
  scheme: string;
  host: string;
  port: number;
  auth: boolean;
  status: ProxyTest | null;
}

export interface ProxiesState {
  mode: "direct" | "list" | "gateway";
  file: string;
  strategy: string;
  strategies: string[];
  gateway: { server: string | null; sticky: boolean; error?: string; status: ProxyTest | null } | null;
  proxies: ProxyItem[];
  errors: string[];
  note: string | null;
  env_file: string;
}
