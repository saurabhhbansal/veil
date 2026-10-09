// Shapes of the local veil server's API (src/veil/webui/runner.py snapshot() and server.py /api/config).

export type RunStatus = "idle" | "starting" | "running" | "paused" | "needs_human" | "stopping";

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
    | { type: "task"; text: string; run: string }
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
  );

export type EventOf<T extends RunEvent["type"]> = Extract<RunEvent, { type: T }>;

export interface HumanRequest {
  reason: string;
  source: "user" | "agent" | "detector";
  vendor?: string;
  url?: string;
  since: number;
}

export interface Snapshot {
  version: number;
  status: RunStatus;
  busy: boolean;
  events: RunEvent[];
  identity: Identity | null;
  human: HumanRequest | null;
  browser_open: boolean;
  gif: string | null;
  result_file: string | null;
}

export interface VeilConfig {
  llm_provider: string;
  llm_model: string;
  has_api_key: boolean;
  providers: string[];
  engine: string;
  engines: string[];
  headless: boolean;
  geo_match: boolean;
  max_attempts: number;
  max_steps: number;
  identities: string[];
  proxy: "gateway" | "list" | null;
  proxy_detail: string | null;
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
