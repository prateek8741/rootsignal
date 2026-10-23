import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import { issueSummary, span, traceSummary } from "../test/fixtures";
import { CONTEXT, connectAs, mockApi, renderWithProviders } from "../test/render";

afterEach(() => vi.unstubAllGlobals());

describe("navigation", () => {
  it("sends visitors without a key to the connect page", async () => {
    renderWithProviders(<AppRoutes />, { route: "/traces" });
    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
  });

  it("connects with a valid key and shows the environment", async () => {
    mockApi((url) => {
      if (url.pathname === "/v1/context") return { body: CONTEXT };
      if (url.pathname === "/v1/traces") return { body: { items: [], next_cursor: null } };
      return undefined;
    });
    renderWithProviders(<AppRoutes />, { route: "/connect" });
    await userEvent.type(screen.getByLabelText("API key"), "rs_" + "k".repeat(43));
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("heading", { name: "Traces" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Environment" })).toHaveDisplayValue(
      "acme / support / production");
  });

  it("shows the API's error for a bad key", async () => {
    mockApi(() => ({ status: 401, body: { error: { code: "unauthenticated", message: "invalid API key" } } }));
    renderWithProviders(<AppRoutes />, { route: "/connect" });
    await userEvent.type(screen.getByLabelText("API key"), "rs_wrong");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("wasn't accepted");
  });
});

describe("traces page", () => {
  it("shows onboarding when there are no traces at all", async () => {
    connectAs();
    mockApi(() => ({ body: { items: [], next_cursor: null } }));
    renderWithProviders(<AppRoutes />, { route: "/traces" });
    expect(await screen.findByText("No traces yet")).toBeInTheDocument();
    expect(screen.getByText(/rs\.init/)).toBeInTheDocument();
  });

  it("distinguishes an empty filter result from an empty project", async () => {
    connectAs();
    mockApi(() => ({ body: { items: [], next_cursor: null } }));
    renderWithProviders(<AppRoutes />, { route: "/traces?status=error" });
    expect(await screen.findByText("No traces match these filters")).toBeInTheDocument();
  });

  it("lists traces with findings, cost and filters sent to the API", async () => {
    connectAs();
    const fetchMock = mockApi((url) => url.pathname === "/v1/traces"
      ? { body: { items: [traceSummary({ unpriced_llm_call_count: 1, cost_usd: "0.50" })], next_cursor: null } }
      : undefined);
    renderWithProviders(<AppRoutes />, { route: "/traces?agent=support-agent" });
    const row = (await screen.findByRole("link", { name: "0af76519" })).closest("tr");
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement);
    expect(cells.getByText("≥ $0.50")).toBeInTheDocument();
    expect(cells.getByText("2")).toBeInTheDocument(); // findings
    const calledUrl = new URL(String(fetchMock.mock.calls[0]?.[0]), "http://x");
    expect(calledUrl.searchParams.get("agent_name")).toBe("support-agent");
  });
});

describe("trace detail", () => {
  it("highlights failed and repeated spans and shows evidence for a finding", async () => {
    connectAs();
    const root = span({ span_id: "a".repeat(16), kind: "agent", name: "invoke_agent support-agent" });
    const failed = span({ span_id: "b".repeat(16), parent_span_id: root.span_id, tool_name: "get_account",
      status: "error", error_type: "UpstreamTimeout", repeat_index: 2, call_index: 2 });
    mockApi((url) => url.pathname.startsWith("/v1/traces/") ? {
      body: {
        trace: traceSummary(), spans: [root, failed],
        findings: [{ id: "f1", detector: "repeated_tool_call", detector_version: 1, finding_key: "k",
          severity: "medium", title: "'get_account' called 3 times with identical arguments",
          summary: "3 calls", evidence_span_ids: [failed.span_id], facts: {}, tool_name: "get_account",
          error_type: null, created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-23T10:00:00Z",
          issue_id: "6b1f0c1e-0000-4000-8000-000000000001" }],
      },
    } : undefined);
    renderWithProviders(<AppRoutes />, { route: `/traces/${traceSummary().trace_id}` });
    const failedRow = await screen.findByTestId(`span-${failed.span_id}`);
    expect(within(failedRow).getByText("UpstreamTimeout")).toBeInTheDocument();
    expect(within(failedRow).getByText("repeat #2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View issue →" })).toHaveAttribute(
      "href", "/issues/6b1f0c1e-0000-4000-8000-000000000001");

    await userEvent.click(screen.getByRole("button", { name: /called 3 times/ }));
    expect(within(failedRow).getByText("(evidence)")).toBeInTheDocument();
    await userEvent.click(failedRow);
    expect(await screen.findByText("Side effect")).toBeInTheDocument();
  });
});

describe("issues", () => {
  it("shows an empty state for no open issues", async () => {
    connectAs();
    mockApi(() => ({ body: { items: [], next_cursor: null } }));
    renderWithProviders(<AppRoutes />, { route: "/issues" });
    expect(await screen.findByText("No open issues")).toBeInTheDocument();
  });

  it("disables status changes for keys without the triage scope", async () => {
    connectAs({ ...CONTEXT, scopes: ["read"] });
    mockApi((url) => url.pathname.endsWith("/occurrences")
      ? { body: { items: [], next_cursor: null } }
      : { body: { issue: issueSummary(), versions: [], events: [], recent_occurrences: [] } });
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    expect(await screen.findByRole("button", { name: "Resolve…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ignore" })).toBeDisabled();
    expect(screen.getByText(/Read-only key/)).toBeInTheDocument();
  });

  it("resolves with a fixed-in version", async () => {
    connectAs();
    const fetchMock = mockApi((url, init) => {
      if (url.pathname.endsWith("/status") && init?.method === "POST") {
        return { body: issueSummary({ status: "resolved", fixed_in_version: "v2" }) };
      }
      if (url.pathname.endsWith("/occurrences")) return { body: { items: [], next_cursor: null } };
      return { body: { issue: issueSummary(), versions: [], events: [], recent_occurrences: [] } };
    });
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Resolve…" }));
    await userEvent.type(screen.getByPlaceholderText("v2"), "v2");
    await userEvent.click(screen.getByRole("button", { name: "Mark resolved" }));
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
      expect(post).toBeDefined();
      expect(JSON.parse(String(post?.[1]?.body))).toEqual({ status: "resolved", fixed_in_version: "v2", note: null });
    });
  });
});
