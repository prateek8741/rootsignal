import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Code, ErrorBox } from "../components/ui";
import { connectionLabel, useConnections } from "../lib/connection";

export function ConnectPage() {
  const { connections, connect, select } = useConnections();
  const navigate = useNavigate();
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await connect(apiKey);
      navigate("/traces");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-lg px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight">Rootsignal</h1>
      <p className="mt-2 text-sm text-slate-600">
        Connect with an API key that has the <Code>read</Code> scope (add <Code>triage</Code> to
        resolve and ignore issues). Each key belongs to one project environment.
      </p>
      <form onSubmit={submit} className="mt-6 space-y-3">
        <label className="block text-sm font-medium text-slate-700" htmlFor="api-key">API key</label>
        <input id="api-key" type="password" autoComplete="off" value={apiKey}
          onChange={(e) => setApiKey(e.target.value)} placeholder="rs_…"
          className="w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm focus:border-slate-500 focus:outline-none" />
        {error !== null && <ErrorBox error={error} />}
        <Button type="submit" variant="primary" disabled={busy || !apiKey.trim()}>
          {busy ? "Checking…" : "Connect"}
        </Button>
      </form>
      <div className="mt-8 rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-600">
        <p className="font-medium text-slate-800">No key yet?</p>
        <pre className="mt-2 overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
{`uv run rootsignal-server create-key --org <org> --project <project> \\
    --environment development --name dashboard --scopes read,triage

# or load demo data (prints a dashboard key):
uv run python scripts/seed_demo.py`}
        </pre>
        <p className="mt-2 text-xs text-slate-500">
          The key is kept in this browser tab's session storage and sent only to this server.
        </p>
      </div>
      {connections.length > 0 && (
        <div className="mt-6">
          <p className="text-sm font-medium text-slate-700">Saved in this tab</p>
          <ul className="mt-2 space-y-1">
            {connections.map((c) => (
              <li key={c.id}>
                <button type="button" className="text-sm text-sky-700 underline"
                  onClick={() => {
                    select(c.id);
                    navigate("/traces");
                  }}>
                  {connectionLabel(c.context)} ({c.id}…)
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
