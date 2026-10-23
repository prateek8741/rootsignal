import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { Context } from "../api/client";
import { AuthProvider } from "../lib/auth";

export const CONTEXT: Context = {
  org_slug: "acme", org_name: "Acme", project_slug: "support", project_name: "Support",
  environment: "production", scopes: ["read", "triage"], key_prefix: "s_1a2b3c4d",
  principal: "session:s_1a2b3c4d", llm_available: false,
};

type Hit = { status?: number; body?: unknown } | undefined;
type Route = (url: URL, init?: RequestInit) => Hit;

/**
 * Replace fetch with canned API responses. ``session`` is what GET /v1/auth/session
 * returns (null = signed out); other paths go to ``route``.
 */
export function mockApi(route: Route = () => undefined, { session = CONTEXT as Context | null } = {}) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://dashboard.test");
    let hit: Hit = route(url, init);
    if (!hit && url.pathname === "/v1/auth/session" && (!init?.method || init.method === "GET")) {
      hit = session ? { body: session } : { status: 401, body: { error: { code: "unauthenticated", message: "missing credentials" } } };
    }
    if (!hit) return new Response(JSON.stringify({ error: { code: "not_found", message: "not found" } }), { status: 404 });
    if (hit.status === 204) return new Response(null, { status: 204 });
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
      <AuthProvider>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}
