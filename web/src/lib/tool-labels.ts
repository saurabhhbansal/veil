// Adapted from chat-ui-example's tool-labels.ts, mapped to browser-use's action names
// (navigate, search, click, input, scroll, extract, done, ...) plus veil's request_human_help.
import type { StepAction, ToolCallType, ToolStatus, UIToolCall } from "./types";

const TYPE_MAP: Record<string, ToolCallType> = {
  navigate: "browse", go_to_url: "browse", go_back: "browse", open_tab: "tab",
  switch: "tab", switch_tab: "tab", close: "tab", close_tab: "tab",
  click: "click", click_element: "click", select_dropdown: "click", dropdown_options: "click", upload_file: "click",
  input: "type", input_text: "type",
  send_keys: "keys",
  scroll: "scroll", scroll_to_text: "scroll", find_text: "scroll",
  search: "search", search_google: "search",
  wait: "wait",
  screenshot: "screenshot",
  extract: "extract", extract_structured_data: "extract", evaluate: "extract", find_elements: "extract",
  write_file: "file", read_file: "file", replace_file: "file", replace_file_str: "file",
  done: "done",
  request_human_help: "human",
};

const LABELS: Record<ToolCallType, [string, string]> = {
  browse: ["Navigating", "Navigated"],
  tab: ["Switching tab", "Switched tab"],
  click: ["Clicking", "Clicked"],
  type: ["Typing", "Typed"],
  keys: ["Pressing keys", "Pressed keys"],
  scroll: ["Scrolling", "Scrolled"],
  search: ["Searching", "Searched"],
  wait: ["Waiting", "Waited"],
  screenshot: ["Taking screenshot", "Took screenshot"],
  extract: ["Reading page", "Read page"],
  file: ["Writing file", "Wrote file"],
  done: ["Finishing", "Finished"],
  human: ["Asking you for help", "Asked you for help"],
  unknown: ["Running", "Ran"],
};

export function getToolType(name: string): ToolCallType {
  return TYPE_MAP[name] ?? "unknown";
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + "…" : s;
}

export function getToolDisplayValue(name: string, args: Record<string, unknown>): string {
  const type = getToolType(name);
  switch (type) {
    case "browse":
      return name === "go_back" ? "" : truncate(String(args.url ?? ""), 60);
    case "search":
      return truncate(String(args.query ?? ""), 60);
    case "type":
      // sensitive_data placeholders (<secret>name</secret>) are shown as-is; the real value never reaches the UI
      return truncate(String(args.text ?? ""), 40);
    case "keys":
      return truncate(String(args.keys ?? ""), 30);
    case "click":
      return args.index !== undefined ? `element ${args.index}` : args.coordinate_x !== undefined ? `${args.coordinate_x}, ${args.coordinate_y}` : "";
    case "scroll":
      return args.text ? truncate(String(args.text), 40) : args.down === false ? "up" : "down";
    case "extract":
      return truncate(String(args.query ?? ""), 50);
    case "file":
      return truncate(String(args.file_name ?? args.path ?? ""), 50);
    case "human":
      return truncate(String(args.reason ?? ""), 60);
    case "wait":
      return args.seconds !== undefined ? `${args.seconds}s` : "";
    default:
      return "";
  }
}

export function toToolCall(action: StepAction, id: string, status: ToolStatus): UIToolCall {
  const type = getToolType(action.name);
  const pair = LABELS[type];
  const label = type === "unknown" ? action.name.replace(/_/g, " ") : status === "completed" || status === "error" ? pair[1] : pair[0];
  return {
    id,
    name: action.name,
    displayName: label.charAt(0).toUpperCase() + label.slice(1),
    displayValue: getToolDisplayValue(action.name, action.args),
    args: action.args,
    status,
    type,
  };
}
