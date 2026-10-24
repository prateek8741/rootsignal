import type { CaseSummary, ReplayBrief } from "../api/client";

export type Verification = { tone: "green" | "red" | "amber" | "gray"; text: string };

const agent = (run: ReplayBrief | null | undefined) => (run?.agent_version ? ` on agent ${run.agent_version}` : "");

/** One line on what replay has shown for a case. Never says "verified" unless the case is passing. */
export function caseVerification(c: CaseSummary): Verification {
  if (c.status === "passing" && c.verified_by) {
    return { tone: "green", text: `Fix verified by replay${agent(c.verified_by)} (${c.verified_by.mode} mode)` };
  }
  if (c.status === "failing") return { tone: "red", text: `Still failing on replay${agent(c.latest_replay)}: fix not verified` };
  if (c.status === "reproduced") return { tone: "amber", text: `Reproduced by replay${agent(c.reproduced_by)}: fix not verified yet` };
  if (c.status === "not_reproduced") return { tone: "amber", text: "Replay didn't reproduce the failure: can't verify a fix" };
  if (c.latest_replay) return { tone: "gray", text: `Latest replay ${c.latest_replay.verdict}: nothing concluded` };
  return { tone: "gray", text: "Not replayed yet" };
}

/** The first passing case's verified run, if any: the issue's fix is verified by replay. */
export function verifiedFix(cases: CaseSummary[]): { caseId: string; run: ReplayBrief } | null {
  const c = cases.find((x) => x.status === "passing" && x.verified_by);
  return c?.verified_by ? { caseId: c.id, run: c.verified_by } : null;
}
