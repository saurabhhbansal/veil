"use client";

import type { ReactNode } from "react";
import { RunProvider } from "@/context/run-context";
import { SettingsProvider } from "@/context/settings-context";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <SettingsProvider>
      <RunProvider>{children}</RunProvider>
    </SettingsProvider>
  );
}
