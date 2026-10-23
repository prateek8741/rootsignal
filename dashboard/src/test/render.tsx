import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { Context } from "../api/client";
import { ConnectionProvider } from "../lib/connection";

export const CONTEXT: Context = {
  org_slug: "acme", org_name: "Acme", project_slug: "support", project_name: "Support",
  environment: "production", scopes: ["read", "triage"], key_prefix: "rs_testkey",
};

/** Seed the session with an active connection, as the Connect page would. */
export function connectAs(context: Context = CONTEXT, apiKey = "rs_" + "k".repeat(43)): void {
  sessionStorage.setItem("rootsignal.connections",
    JSON.stringify([{ id: context.key_prefix, apiKey, context }]));
  sessionStorage.setItem("rootsignal.active", context.key_prefix);
}

type Route = (url: URL, init?: RequestInit) => { status?: number; body: unknown } | undefined;

/** Replace fetch with a router of canned API responses; returns the mock for assertions. */
export function mockApi(route: Route) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://dashboard.test");
    const hit = route(url, init);
    if (!hit) return new Response(JSON.stringify({ error: { code: "not_found", message: "not found" } }), { status: 404 });
    return new Response(JSON.stringify(hit.body), { status: hit.status ?? 200,
      headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

export function renderWithProviders(ui: ReactElement, { route = "/" } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ConnectionProvider>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </ConnectionProvider>
    </QueryClientProvider>,
  );
}
