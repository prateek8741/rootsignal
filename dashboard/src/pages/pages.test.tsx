import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import type { CaseSummary, Investigation, ReplayBrief } from "../api/client";
import { issueSummary, span, traceSummary } from "../test/fixtures";
import { CONTEXT, mockApi, renderWithProviders } from "../test/render";

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

const KEY = "rs_" + "k".repeat(43);

function calls(fetchMock: ReturnType<typeof mockApi>, method: string, path: string) {
  return fetchMock.mock.calls.filter(([input, init]) =>
    new URL(String(input), "http://x").pathname === path && (init?.method ?? "GET") === method);
}

describe("signing in", () => {
  it("sends signed-out visitors to the login page", async () => {
    mockApi(undefined, { session: null });
    renderWithProviders(<AppRoutes />, { route: "/traces" });
    expect(await screen.findByRole("heading", { name: "Sign in to Rootsignal" })).toBeInTheDocument();
    expect(screen.getByText(/dashboard-link/)).toBeInTheDocument();
  });

  it("exchanges a pasted key for a session and keeps nothing in the browser", async () => {
    let signedIn = false;
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v1/auth/session" && init?.method === "POST") {
        signedIn = true;
        return { body: CONTEXT };
      }
      if (url.pathname === "/v1/auth/session") {
        return signedIn ? { body: CONTEXT } : { status: 401, body: { error: { code: "unauthenticated", message: "x" } } };
      }
      if (url.pathname === "/v1/traces") return { body: { items: [], next_cursor: null } };
      return undefined;
    });
    renderWithProviders(<AppRoutes />, { route: "/login" });
    const input = await screen.findByLabelText("API key");
    await userEvent.type(input, KEY);
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Traces" })).toBeInTheDocument();

    const [[, init]] = calls(fetchMock, "POST", "/v1/auth/session") as [[unknown, RequestInit]];
    expect(JSON.parse(String(init.body))).toEqual({ api_key: KEY });
    expect((init.headers as Record<string, string>)["X-Rootsignal-CSRF"]).toBe("1");
    expect(screen.getByTestId("environment")).toHaveTextContent("acme / support / production");
    // The key went to the API in exactly one request (the sign-in) and nowhere else.
    expect(sessionStorage.length + localStorage.length).toBe(0);
    const carryingKey = fetchMock.mock.calls.filter(([input, init]) =>
      `${String(input)} ${JSON.stringify(init ?? {})}`.includes(KEY));
    expect(carryingKey).toHaveLength(1);
  });

  it("shows the API's message for a rejected key", async () => {
    mockApi((url, init) => url.pathname === "/v1/auth/session" && init?.method === "POST"
      ? { status: 401, body: { error: { code: "unauthenticated", message: "that key wasn't accepted" } } }
      : undefined, { session: null });
    renderWithProviders(<AppRoutes />, { route: "/login" });
    await userEvent.type(await screen.findByLabelText("API key"), "rs_wrong_key_123");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("that key wasn't accepted");
    expect(screen.getByLabelText("API key")).toHaveValue(""); // not kept around after use
  });

  it("redeems a login link from the URL fragment and erases it", async () => {
    window.history.replaceState(null, "", "/login/link#token=one-time-token-abcdefghijklmnop");
    let signedIn = false;
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v1/auth/link") {
        signedIn = true;
        return { body: CONTEXT };
      }
      if (url.pathname === "/v1/auth/session" && !init?.method) {
        return signedIn ? { body: CONTEXT } : { status: 401, body: {} };
      }
      if (url.pathname === "/v1/traces") return { body: { items: [], next_cursor: null } };
      return undefined;
    });
    renderWithProviders(<AppRoutes />, { route: "/login/link" });
    expect(await screen.findByRole("heading", { name: "Traces" })).toBeInTheDocument();
    const redeemed = calls(fetchMock, "POST", "/v1/auth/link");
    expect(redeemed).toHaveLength(1); // single use: never redeemed twice
    expect(JSON.parse(String(redeemed[0]?.[1]?.body))).toEqual({ token: "one-time-token-abcdefghijklmnop" });
    expect(window.location.hash).toBe("");
  });

  it("explains a link without a token", async () => {
    window.history.replaceState(null, "", "/login/link");
    mockApi(undefined, { session: null });
    renderWithProviders(<AppRoutes />, { route: "/login/link" });
    expect(await screen.findByRole("alert")).toHaveTextContent("no login token");
  });

  it("signs out server-side", async () => {
    let signedOut = false;
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v1/auth/session" && init?.method === "DELETE") {
        signedOut = true; // the server revokes the session and clears the cookie
        return { status: 204 };
      }
      if (url.pathname === "/v1/auth/session" && signedOut) {
        return { status: 401, body: { error: { code: "unauthenticated", message: "x" } } };
      }
      if (url.pathname === "/v1/traces") return { body: { items: [], next_cursor: null } };
      return undefined;
    });
    renderWithProviders(<AppRoutes />, { route: "/traces" });
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in to Rootsignal" })).toBeInTheDocument();
    expect(calls(fetchMock, "DELETE", "/v1/auth/session")).toHaveLength(1);
  });
});

describe("traces page", () => {
  it("shows onboarding when there are no traces at all", async () => {
    mockApi((url) => url.pathname === "/v1/traces" ? { body: { items: [], next_cursor: null } } : undefined);
    renderWithProviders(<AppRoutes />, { route: "/traces" });
    expect(await screen.findByText("No traces yet")).toBeInTheDocument();
  });

  it("distinguishes an empty filter result from an empty project", async () => {
    mockApi((url) => url.pathname === "/v1/traces" ? { body: { items: [], next_cursor: null } } : undefined);
    renderWithProviders(<AppRoutes />, { route: "/traces?status=error" });
    expect(await screen.findByText("No traces match these filters")).toBeInTheDocument();
  });

  it("lists traces with findings, cost and filters sent to the API", async () => {
    const fetchMock = mockApi((url) => url.pathname === "/v1/traces"
      ? { body: { items: [traceSummary({ unpriced_llm_call_count: 1, cost_usd: "0.50" })], next_cursor: null } }
      : undefined);
    renderWithProviders(<AppRoutes />, { route: "/traces?agent=support-agent" });
    const row = (await screen.findByRole("link", { name: "0af76519" })).closest("tr") as HTMLElement;
    expect(within(row).getByText("≥ $0.50")).toBeInTheDocument();
    const [request] = calls(fetchMock, "GET", "/v1/traces");
    expect(new URL(String(request?.[0]), "http://x").searchParams.get("agent_name")).toBe("support-agent");
  });

  it("shows production traces by default and replays on request", async () => {
    const fetchMock = mockApi((url) => url.pathname === "/v1/traces" ? { body: { items: [], next_cursor: null } } : undefined);
    renderWithProviders(<AppRoutes />, { route: "/traces" });
    await userEvent.selectOptions(await screen.findByLabelText("Source"), "Replays");
    await waitFor(() => expect(calls(fetchMock, "GET", "/v1/traces")).toHaveLength(2));
    const [first, second] = calls(fetchMock, "GET", "/v1/traces").map(([input]) => new URL(String(input), "http://x"));
    expect(first?.searchParams.has("source")).toBe(false); // the API defaults to production
    expect(second?.searchParams.get("source")).toBe("replay");
  });
});

describe("trace detail", () => {
  const root = span({ span_id: "a".repeat(16), kind: "agent", name: "invoke_agent support-agent" });
  const failed = span({ span_id: "b".repeat(16), parent_span_id: root.span_id, tool_name: "get_account",
    status: "error", error_type: "UpstreamTimeout", repeat_index: 2, call_index: 2 });
  const detail = {
    trace: traceSummary(), spans: [root, failed],
    findings: [{ id: "f1", detector: "repeated_tool_call", detector_version: 1, finding_key: "k",
      severity: "medium", title: "'get_account' called 3 times with identical arguments",
      summary: "3 calls", evidence_span_ids: [failed.span_id], facts: {}, tool_name: "get_account",
      error_type: null, created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-23T10:00:00Z",
      issue_id: "6b1f0c1e-0000-4000-8000-000000000001" }],
  };

  it("highlights failed and repeated spans and shows evidence for a finding", async () => {
    mockApi((url) => url.pathname.startsWith("/v1/traces/") ? { body: detail } : undefined);
    renderWithProviders(<AppRoutes />, { route: `/traces/${traceSummary().trace_id}` });
    const failedRow = await screen.findByTestId(`span-${failed.span_id}`);
    expect(within(failedRow).getByText("repeat #2")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /called 3 times/ }));
    expect(within(failedRow).getByText("(evidence)")).toBeInTheDocument();
  });

  it("marks traces produced by a replay", async () => {
    mockApi((url) => url.pathname.startsWith("/v1/traces/")
      ? { body: { ...detail, trace: traceSummary({ source: "replay" }) } } : undefined);
    renderWithProviders(<AppRoutes />, { route: `/traces/${traceSummary().trace_id}` });
    expect(await screen.findByText("replay")).toHaveAttribute("title", expect.stringMatching(/rootsignal replay/));
  });

  it("preselects the span named in ?span=", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    mockApi((url) => url.pathname.startsWith("/v1/traces/") ? { body: detail } : undefined);
    renderWithProviders(<AppRoutes />, { route: `/traces/${traceSummary().trace_id}?span=${failed.span_id}` });
    expect(await screen.findByText("Side effect")).toBeInTheDocument();
    expect(screen.getByTestId(`span-${failed.span_id}`)).toHaveAttribute("aria-selected", "true");
  });
});

describe("issues", () => {
  const investigation: Investigation = {
    id: "inv-1", issue_id: issueSummary().id, status: "completed", requested_by: "session:s_1a2b3c4d",
    use_llm: true, llm_status: "completed", llm_provider: "ollama", llm_model: "llama3.1:8b",
    llm_input_tokens: 900, llm_output_tokens: 100, llm_error: null, summary: "s",
    evidence_trace_ids: ["c".repeat(32)], error: null,
    rejected_claims: [{ claim: { statement: "Root cause confirmed" }, reason: "source 'llm' may not assert 'verified'" }],
    created_at: "2026-10-23T10:00:00Z", started_at: "2026-10-23T10:00:00Z", completed_at: "2026-10-23T10:00:05Z",
    claims: [
      { kind: "observed", source: "rules", statement: "get_account failed 4 times",
        refs: [{ type: "span", trace_id: "c".repeat(32), span_id: "d".repeat(16) }] },
      { kind: "hypothesis", source: "llm", statement: "Enterprise accounts time out",
        refs: [{ type: "stat", name: "tool_callers" }] },
      { kind: "next_step", source: "rules", statement: "Cap retries", refs: [] },
    ],
  };

  function issueRoutes(extra?: (url: URL, init?: RequestInit) => { status?: number; body?: unknown } | undefined) {
    return (url: URL, init?: RequestInit) => {
      const custom = extra?.(url, init);
      if (custom) return custom;
      if (url.pathname.endsWith("/occurrences")) return { body: { items: [], next_cursor: null } };
      if (url.pathname.endsWith("/investigations") && !init?.method) return { body: { items: [] } };
      if (url.pathname.endsWith("/regression-cases") && !init?.method) return { body: { items: [] } };
      if (url.pathname.startsWith("/v1/issues/")) return { body: { issue: issueSummary(), versions: [], events: [], recent_occurrences: [] } };
      return undefined;
    };
  }

  it("shows an empty state for no open issues", async () => {
    mockApi((url) => url.pathname === "/v1/issues" ? { body: { items: [], next_cursor: null } } : undefined);
    renderWithProviders(<AppRoutes />, { route: "/issues" });
    expect(await screen.findByText("No open issues")).toBeInTheDocument();
  });

  it("disables triage actions for read-only sessions", async () => {
    mockApi(issueRoutes(), { session: { ...CONTEXT, scopes: ["read"] } });
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    expect(await screen.findByRole("button", { name: "Resolve…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run investigation" })).toBeDisabled();
  });

  it("resolves with a fixed-in version", async () => {
    const fetchMock = mockApi(issueRoutes((url, init) => url.pathname.endsWith("/status") && init?.method === "POST"
      ? { body: issueSummary({ status: "resolved", fixed_in_version: "v2" }) } : undefined));
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Resolve…" }));
    await userEvent.type(screen.getByPlaceholderText("v2"), "v2");
    await userEvent.click(screen.getByRole("button", { name: "Mark resolved" }));
    await waitFor(() => {
      const [post] = calls(fetchMock, "POST", `/v1/issues/${issueSummary().id}/status`);
      expect(JSON.parse(String(post?.[1]?.body))).toEqual({ status: "resolved", fixed_in_version: "v2", note: null });
    });
  });

  it("shows an investigation grouped by kind with linked evidence", async () => {
    mockApi(issueRoutes((url) => {
      if (url.pathname.endsWith("/investigations")) return { body: { items: [investigation] } };
      if (url.pathname === "/v1/investigations/inv-1") return { body: investigation };
      return undefined;
    }), { session: { ...CONTEXT, llm_available: true } });
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    const observed = await screen.findByRole("region", { name: "Observed" });
    expect(within(observed).getByRole("link", { name: "span dddddddd" })).toHaveAttribute(
      "href", `/traces/${"c".repeat(32)}?span=${"d".repeat(16)}`);
    const hypotheses = screen.getByRole("region", { name: "Hypotheses" });
    expect(within(hypotheses).getByText("AI")).toBeInTheDocument();
    expect(screen.getByText("1 suggestion(s) discarded by validation")).toBeInTheDocument();
    expect(screen.getByText(/Hypotheses aren't confirmed causes/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Verified" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Also ask the AI model")).toBeInTheDocument();
  });

  const CASE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
  const run = (overrides: Partial<ReplayBrief>): ReplayBrief => ({
    run_id: "r-1", purpose: "verify", mode: "recorded", verdict: "passing", trace_id: "e".repeat(32),
    evaluated_at: "2026-10-23T10:10:00Z", agent_version: "v2", ...overrides,
  });
  const caseSummary = (overrides: Partial<CaseSummary>): CaseSummary => ({
    id: CASE_ID, issue_id: issueSummary().id, source_trace_id: "c".repeat(32), name: "retry loop",
    status: "draft", current_version: 1, entrypoint: "agent:handle", replayable_recorded: true,
    assertion_count: 3, warning_count: 0, created_by: "key:rs_abc", created_at: "2026-10-23T10:00:00Z",
    updated_at: "2026-10-23T10:00:00Z", latest_replay: null, reproduced_by: null, verified_by: null, ...overrides,
  });
  const reproduced = run({ purpose: "reproduce", verdict: "failing", agent_version: "v1" });

  it("closes the loop: a verified fix, its replay history and a pre-filled resolve", async () => {
    const passing = caseSummary({ status: "passing", latest_replay: run({}), reproduced_by: reproduced, verified_by: run({}) });
    const events = [{ kind: "replayed", actor: "key:rs_abc", from_status: null, to_status: null, created_at: "2026-10-23T10:10:00Z",
      details: { purpose: "verify", verdict: "passing", case_id: CASE_ID, trace_id: "e".repeat(32) } }];
    const fetchMock = mockApi(issueRoutes((url, init) => {
      if (url.pathname.endsWith("/regression-cases") && !init?.method) return { body: { items: [passing] } };
      if (url.pathname.endsWith("/status") && init?.method === "POST") return { body: issueSummary({ status: "resolved" }) };
      if (url.pathname === `/v1/issues/${issueSummary().id}`) return { body: { issue: issueSummary(), versions: [], events, recent_occurrences: [] } };
      return undefined;
    }));
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    expect(await screen.findByRole("status", { name: "Fix verification" })).toHaveTextContent("Fix verified by replay (recorded mode, agent v2)");
    expect(screen.getByText("Fix verified by replay on agent v2 (recorded mode)")).toBeInTheDocument();
    const replayed = screen.getByText("Replayed").closest("li") as HTMLElement;
    expect(replayed).toHaveTextContent("verify → passing");
    expect(within(replayed).getByRole("link", { name: "case" })).toHaveAttribute("href", `/cases/${CASE_ID}`);

    await userEvent.click(screen.getByRole("button", { name: "Resolve…" }));
    expect(screen.getByPlaceholderText("v2")).toHaveValue("v2");
    expect(screen.getByText(/replay verified the fix on agent v2/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Mark resolved" }));
    await waitFor(() => {
      const [post] = calls(fetchMock, "POST", `/v1/issues/${issueSummary().id}/status`);
      expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ status: "resolved", fixed_in_version: "v2" });
    });
  });

  it.each([
    [caseSummary({ status: "reproduced", latest_replay: reproduced, reproduced_by: reproduced }), "Reproduced by replay on agent v1: fix not verified yet"],
    [caseSummary({ status: "failing", latest_replay: run({ verdict: "failing", agent_version: "v3" }), reproduced_by: reproduced }), "Still failing on replay on agent v3: fix not verified"],
    [caseSummary({ status: "not_reproduced", latest_replay: run({ purpose: "reproduce" }) }), "Replay didn't reproduce the failure: can't verify a fix"],
    [caseSummary({ latest_replay: run({ verdict: "diverged" }) }), "Latest replay diverged: nothing concluded"],
    [caseSummary({}), "Not replayed yet"],
  ])("never claims a verified fix unless the case is passing (%#)", async (item, text) => {
    mockApi(issueRoutes((url, init) => url.pathname.endsWith("/regression-cases") && !init?.method ? { body: { items: [item] } } : undefined));
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Fix verification" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resolve…" }));
    expect(screen.getByPlaceholderText("v2")).toHaveValue("");
  });

  it("shows verified claims from replay with a link to the case", async () => {
    const withVerified: Investigation = { ...investigation, claims: [...investigation.claims,
      { kind: "verified", source: "replay", statement: "Fix verified by replay: regression case 'retry loop' reproduced the failure on agent v1 and passes on agent v2 (recorded replay).",
        refs: [{ type: "trace", trace_id: "e".repeat(32) }, { type: "case", case_id: CASE_ID }] }] };
    mockApi(issueRoutes((url) => {
      if (url.pathname.endsWith("/investigations")) return { body: { items: [withVerified] } };
      if (url.pathname === "/v1/investigations/inv-1") return { body: withVerified };
      return undefined;
    }));
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    const section = await screen.findByRole("region", { name: "Verified" });
    expect(within(section).getByText("replay")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "regression case" })).toHaveAttribute("href", `/cases/${CASE_ID}`);
  });

  it("starts an investigation, hiding the AI option when it isn't available", async () => {
    let started = false;
    const fetchMock = mockApi(issueRoutes((url, init) => {
      if (url.pathname.endsWith("/investigations") && init?.method === "POST") {
        started = true;
        return { status: 202, body: { ...investigation, status: "queued", claims: [] } };
      }
      if (url.pathname.endsWith("/investigations")) return { body: { items: started ? [investigation] : [] } };
      if (url.pathname === "/v1/investigations/inv-1") return { body: investigation };
      return undefined;
    }));
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    expect(await screen.findByText(/rules only/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Also ask the AI model")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run investigation" }));
    expect(await screen.findByRole("region", { name: "Observed" })).toBeInTheDocument();
    const [post] = calls(fetchMock, "POST", `/v1/issues/${issueSummary().id}/investigations`);
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ use_llm: false });
  });
});
