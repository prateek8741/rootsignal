import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import type { CaseDetail } from "../api/client";
import { issueSummary } from "../test/fixtures";
import { CONTEXT, mockApi, renderWithProviders } from "../test/render";

afterEach(() => vi.unstubAllGlobals());

const CASE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";

function caseDetail(overrides: Partial<CaseDetail> = {}, specOverrides: Record<string, unknown> = {}): CaseDetail {
  return {
    id: CASE_ID, issue_id: issueSummary().id, source_trace_id: "c".repeat(32),
    name: "'get_account' called repeatedly (trace cccccccc)", status: "draft", current_version: 1,
    entrypoint: null, replayable_recorded: false, assertion_count: 3, warning_count: 2,
    created_by: "session:s_1a2b3c4d", created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-23T10:00:00Z",
    version: 1,
    versions: [{ version: 1, created_by: "session:s_1a2b3c4d", created_at: "2026-10-23T10:00:00Z", change_note: "created from trace cccccccc" }],
    spec: {
      name: "'get_account' called repeatedly (trace cccccccc)", entrypoint: null,
      source: { trace_id: "c".repeat(32), agent_name: "support-agent", agent_version: "v1", started_at: "2026-10-23T10:00:00Z" },
      input: { kind: "missing" },
      fixtures: {
        tools: [
          { call_index: 0, tool: "get_account", side_effect: "read", repeat_index: 0,
            arguments: { captured: true, value: { account_id: "ent-42" } }, outcome: "error",
            error_type: "UpstreamTimeout", error_message: "timed out" },
          { call_index: 1, tool: "create_ticket", side_effect: "write", repeat_index: 0,
            arguments: { captured: true, value: { summary: "x" } }, outcome: "result",
            result: { captured: true, value: "T-1", truncated: true } },
        ],
        llm: [{ call_index: 0, model: "m", output: { captured: true } }], retrievals: [],
      },
      assertions: [
        { id: "a1", kind: "max_identical_tool_calls", params: { tool: "get_account", max: 2 },
          description: "'get_account' is called at most 2 time(s) with identical arguments",
          role: "reproduces", requires: "recorded", enabled: true },
        { id: "a2", kind: "detector_absent", params: { detector: "timeout", tool: "get_account" },
          description: "'get_account' no longer fails by timing out", role: "reproduces",
          requires: "live_tools", enabled: true },
        { id: "a3", kind: "run_completed", params: {}, description: "The run still completes",
          role: "guard", requires: "recorded", enabled: true },
      ],
      quality: { replayable_recorded: false,
        warnings: ["The run's input wasn't captured (capture_user_input is off by default); supply it before replaying."],
        dropped_assertions: [{ description: "No exception escapes the agent run", reason: "duplicate" }] },
      ...specOverrides,
    },
    ...overrides,
  };
}

describe("regression cases", () => {
  it("creates a case from the issue page and opens it", async () => {
    let created = false;
    const fetchMock = mockApi((url, init) => {
      if (url.pathname.endsWith("/regression-cases") && init?.method === "POST") {
        created = true;
        return { status: 201, body: caseDetail() };
      }
      if (url.pathname.endsWith("/regression-cases")) return { body: { items: created ? [caseDetail()] : [] } };
      if (url.pathname === `/v1/regression-cases/${CASE_ID}`) return { body: caseDetail() };
      if (url.pathname.endsWith("/occurrences")) return { body: { items: [], next_cursor: null } };
      if (url.pathname.endsWith("/investigations")) return { body: { items: [] } };
      if (url.pathname.startsWith("/v1/issues/")) return { body: { issue: issueSummary(), versions: [], events: [], recent_occurrences: [] } };
      return undefined;
    });
    renderWithProviders(<AppRoutes />, { route: `/issues/${issueSummary().id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Create from worst trace" }));
    expect(await screen.findByRole("heading", { name: /called repeatedly/ })).toBeInTheDocument();
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(1);
  });

  it("explains assertions, fixtures and gaps", async () => {
    mockApi((url) => url.pathname === `/v1/regression-cases/${CASE_ID}` ? { body: caseDetail() } : undefined);
    renderWithProviders(<AppRoutes />, { route: `/cases/${CASE_ID}` });
    expect(await screen.findByText(/input wasn't captured/)).toBeInTheDocument();
    expect(screen.getByText("needs attention")).toBeInTheDocument();
    const live = screen.getByText("live tools");
    expect(live).toHaveAttribute("title", expect.stringMatching(/can't make the tool succeed/));
    expect(screen.getAllByText("reproduces")).toHaveLength(2);
    expect(screen.getByText("guard")).toBeInTheDocument();
    expect(screen.getByText("write · stubbed")).toBeInTheDocument();
    expect(screen.getByText("truncated")).toBeInTheDocument();
    expect(screen.getByText("1 candidate assertion(s) not used")).toBeInTheDocument();
  });

  it("validates JSON input locally and saves a new version", async () => {
    let saved: unknown = null;
    mockApi((url, init) => {
      if (url.pathname === `/v1/regression-cases/${CASE_ID}` && init?.method === "PATCH") {
        saved = JSON.parse(String(init.body));
        return { body: caseDetail({ current_version: 2, version: 2 }) };
      }
      if (url.pathname === `/v1/regression-cases/${CASE_ID}`) return { body: caseDetail() };
      return undefined;
    });
    renderWithProviders(<AppRoutes />, { route: `/cases/${CASE_ID}` });
    const input = await screen.findByLabelText("Input (JSON)");
    await userEvent.type(input, "not json");
    await userEvent.click(screen.getByRole("button", { name: "Save input" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("valid JSON");
    expect(saved).toBeNull();

    await userEvent.clear(input);
    await userEvent.type(input, '{{"request": "hi"}');
    await userEvent.type(screen.getByLabelText("Change note (optional)"), "from ticket");
    await userEvent.click(screen.getByRole("button", { name: "Save input" }));
    await waitFor(() => expect(saved).toEqual({ input: { request: "hi" }, note: "from ticket" }));
  });

  it("shows older versions read-only and downloads the spec for the viewed version", async () => {
    Object.assign(URL, { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const fetchMock = mockApi((url) => {
      if (url.pathname.endsWith("/spec")) return { body: { case_id: CASE_ID, version: 1 } };
      if (url.pathname === `/v1/regression-cases/${CASE_ID}`) {
        return { body: caseDetail({ current_version: 2, version: 1 }) };
      }
      return undefined;
    });
    renderWithProviders(<AppRoutes />, { route: `/cases/${CASE_ID}?version=1` });
    expect(await screen.findByText(/older version \(read-only\)/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Input (JSON)")).not.toBeInTheDocument();
    expect(within(screen.getByRole("row", { name: /a1/ })).getByRole("checkbox")).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Download JSON" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    const specCall = fetchMock.mock.calls.find(([input]) => String(input).includes("/spec"));
    expect(new URL(String(specCall?.[0]), "http://x").searchParams.get("version")).toBe("1");
  });

  it("is read-only without the triage scope", async () => {
    mockApi((url) => url.pathname === `/v1/regression-cases/${CASE_ID}` ? { body: caseDetail() } : undefined,
      { session: { ...CONTEXT, scopes: ["read"] } });
    renderWithProviders(<AppRoutes />, { route: `/cases/${CASE_ID}` });
    expect(await screen.findByText(/needs the 'triage' scope/)).toBeInTheDocument();
  });
});
