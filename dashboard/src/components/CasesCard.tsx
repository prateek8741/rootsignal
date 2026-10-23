import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useSession } from "../lib/auth";
import { formatRelative } from "../lib/format";
import { Badge, Button, Card, ErrorBox, Loading } from "./ui";

/** Regression cases built from this issue, and creating one from its worst trace. */
export function CasesCard({ issueId }: { issueId: string }) {
  const context = useSession();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const canTriage = context.scopes.includes("triage");
  const cases = useQuery({ queryKey: ["issue", issueId, "cases"], queryFn: () => api.issueCases(issueId) });
  const create = useMutation({
    mutationFn: () => api.createCase(issueId),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["issue", issueId, "cases"] });
      navigate(`/cases/${created.id}`);
    },
  });
  return (
    <Card title="Regression cases" actions={
      <Button disabled={!canTriage || create.isPending} title={canTriage ? undefined : "Needs the 'triage' scope"}
        onClick={() => create.mutate()}>
        {create.isPending ? "Creating…" : "Create from worst trace"}
      </Button>
    }>
      {create.isError && <ErrorBox error={create.error} />}
      {cases.isPending ? <Loading /> : cases.isError ? <ErrorBox error={cases.error} /> :
        cases.data.items.length === 0 ? (
          <p className="text-sm text-slate-600">
            A regression case captures a failing run (input, recorded tool results and model
            outputs) plus assertions that fail on it, so a fix can be checked by replaying it.
          </p>
        ) : (
          <ul className="divide-y divide-slate-100 text-sm">
            {cases.data.items.map((c) => (
              <li key={c.id} className="flex flex-wrap items-center gap-2 py-2">
                <Link to={`/cases/${c.id}`} className="font-medium text-sky-700 hover:underline">{c.name}</Link>
                <Badge>{c.status}</Badge>
                <Badge>v{c.current_version}</Badge>
                <span className="text-xs text-slate-500">{c.assertion_count} assertion(s)</span>
                {c.replayable_recorded ? <Badge tone="green">replayable</Badge>
                  : <Badge tone="amber" title="See the case's warnings">needs attention</Badge>}
                <span className="ml-auto text-xs text-slate-500">{formatRelative(c.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
    </Card>
  );
}
