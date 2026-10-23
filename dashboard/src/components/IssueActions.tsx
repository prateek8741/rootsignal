import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type IssueSummary, type StatusChange } from "../api/client";
import { useSession } from "../lib/auth";
import { Button, ErrorBox } from "./ui";

/** Resolve / ignore / reopen. Requires the key's `triage` scope (enforced by the API). */
export function IssueActions({ issue }: { issue: IssueSummary }) {
  const context = useSession();
  const queryClient = useQueryClient();
  const canTriage = context.scopes.includes("triage");
  const [resolving, setResolving] = useState(false);
  const [fixedIn, setFixedIn] = useState("");
  const [note, setNote] = useState("");

  const mutation = useMutation({
    mutationFn: (change: StatusChange) => api.setStatus(issue.id, change),
    onSuccess: () => {
      setResolving(false);
      setFixedIn("");
      setNote("");
      void queryClient.invalidateQueries({ queryKey: ["issue", issue.id] });
      void queryClient.invalidateQueries({ queryKey: ["issues"] });
    },
  });

  const disabled = !canTriage || mutation.isPending;
  const why = canTriage ? undefined : "This API key lacks the 'triage' scope";
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {issue.status !== "resolved" && (
          <Button variant="primary" disabled={disabled} title={why} onClick={() => setResolving(!resolving)}>
            Resolve…
          </Button>
        )}
        {issue.status !== "ignored" && (
          <Button disabled={disabled} title={why} onClick={() => mutation.mutate({ status: "ignored", note: note || null })}>
            Ignore
          </Button>
        )}
        {issue.status !== "open" && (
          <Button disabled={disabled} title={why} onClick={() => mutation.mutate({ status: "open", note: note || null })}>
            Reopen
          </Button>
        )}
      </div>
      {!canTriage && <p className="text-xs text-slate-500">Read-only key: add the <code>triage</code> scope to change status.</p>}
      {resolving && (
        <form className="space-y-2 rounded-md border border-slate-200 p-3 text-sm"
          onSubmit={(e) => {
            e.preventDefault();
            mutation.mutate({ status: "resolved", fixed_in_version: fixedIn.trim() || null, note: note.trim() || null });
          }}>
          <label className="block">
            <span className="text-xs text-slate-500">Fixed in version (optional)</span>
            <input value={fixedIn} onChange={(e) => setFixedIn(e.target.value)} placeholder="v2"
              className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1" />
          </label>
          <p className="text-xs text-slate-500">
            With a version, failures from older affected versions count as stragglers and won't reopen
            the issue; a failure from this or a new version reopens it as a regression.
          </p>
          <label className="block">
            <span className="text-xs text-slate-500">Note (optional)</span>
            <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={2000}
              className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1" />
          </label>
          <Button type="submit" variant="primary" disabled={mutation.isPending}>Mark resolved</Button>
        </form>
      )}
      {mutation.isError && <ErrorBox error={mutation.error} />}
    </div>
  );
}
