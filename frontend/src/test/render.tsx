import { render } from "@testing-library/react";
import { App } from "../App";
import { createDemoAdapter } from "../demo/adapter";
import type { DataAdapter } from "../types";

/** Seeded in-memory adapter with every method spied, so tests assert exact calls. */
export function spiedAdapter(overrides: Partial<DataAdapter> = {}): DataAdapter {
  const base = { ...createDemoAdapter(), mode: "live" as const, ...overrides };
  for (const k of Object.keys(base) as (keyof DataAdapter)[]) {
    if (typeof base[k] === "function") (base as Record<string, unknown>)[k] = vi.fn(base[k] as never);
  }
  return base;
}

export function renderAt(path: string, adapter: DataAdapter) {
  window.history.pushState({}, "", path);
  return render(<App adapter={adapter} />);
}
