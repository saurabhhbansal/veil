"use client";

import { Fingerprint } from "lucide-react";
import type { Identity } from "@/lib/types";

export function identitySummary(id: Identity): string {
  const parts = [id.exit?.ip ?? "no exit lookup", [id.exit?.country, id.exit?.city].filter(Boolean).join(" ")];
  if (id.device) parts.push(`${id.device.windows} · ${id.device.gpu}`);
  return parts.filter(Boolean).join(" · ");
}

/** One identity (what the site sees this attempt): inline in the chat. */
export function IdentityLine({ identity }: { identity: Identity }) {
  return (
    <div className="flex items-start gap-2 text-[13px] text-zinc-400 py-1">
      <Fingerprint className="w-4 h-4 text-green-500/80 shrink-0 mt-0.5" />
      <span>
        <span className="text-zinc-200 font-medium">
          Identity {identity.attempt}/{identity.total}
        </span>{" "}
        · {identitySummary(identity)}
        {identity.sticky && <span className="text-green-400"> · sticky “{identity.sticky}”</span>}
        {identity.engine === "chromium" && <span className="text-red-400"> · stock Chromium (not stealthy)</span>}
      </span>
    </div>
  );
}

/** The full device and network detail, for the browser panel. */
export function IdentityDetails({ identity }: { identity: Identity }) {
  const d = identity.device;
  const rows: [string, string][] = [
    ["Attempt", `${identity.attempt} of ${identity.total} · engine ${identity.engine}${identity.headless ? " · headless" : " · window shown"}`],
    ["Proxy", identity.proxy ?? "none (direct)"],
    ["Exit IP", identity.exit ? `${identity.exit.ip} · ${[identity.exit.country, identity.exit.city].filter(Boolean).join(" ")} · ${identity.exit.timezone ?? ""}` : "not looked up"],
  ];
  if (d) {
    rows.push(
      ["Device", `${d.windows} ${d.machine_class} · ${d.cores} cores / ${d.memory} GB · ${d.screen}`],
      ["GPU", d.gpu],
      ["Locale", `${d.timezone} · ${d.languages}`],
      ["Seeds", `canvas ${d.canvas_seed} · audio ${d.audio_seed}`],
    );
  }
  if (identity.sticky) rows.push(["Sticky", `${identity.sticky}: same device, IP and cookies next time`]);
  return (
    <dl className="grid grid-cols-[72px_1fr] gap-x-3 gap-y-1 text-[12px]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-zinc-500">{k}</dt>
          <dd className="text-zinc-300 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
