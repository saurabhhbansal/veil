import type { ConversationTurn, RunEvent } from "./types";

/** Group the run's events into turns: one per task, holding everything that happened after it. */
export function groupIntoTurns(events: RunEvent[]): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  for (const ev of events) {
    if (ev.type === "task") {
      turns.push({ id: `turn-${ev.id}`, task: ev, items: [], isComplete: false });
    } else if (turns.length) {
      const turn = turns[turns.length - 1];
      turn.items.push(ev);
      if (ev.type === "result") turn.isComplete = true;
    }
  }
  return turns;
}

export function lastStepUrl(events: RunEvent[]): string {
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.type === "step" && ev.url) return ev.url;
  }
  return "";
}
