import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { Button, Code, ErrorBox } from "../components/ui";
import { useAuth } from "../lib/auth";

export function LoginPage() {
  const { status, signInWithKey } = useAuth();
  const navigate = useNavigate();
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  if (status === "signed-in") return <Navigate to="/traces" replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    const key = apiKey.trim();
    setApiKey(""); // the key leaves this component's state immediately
    setBusy(true);
    setError(null);
    try {
      await signInWithKey(key);
      navigate("/traces");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-lg px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight">Sign in to Rootsignal</h1>
      <p className="mt-2 text-sm text-slate-600">
        A session covers one project environment. Your browser keeps only a session cookie
        that page scripts can't read; it ends when you sign out, after 2 hours idle, or
        after 12 hours.
      </p>

      <section className="mt-8 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-800">Recommended: one-time login link</h2>
        <p className="mt-1 text-sm text-slate-600">Run this and open the link it prints (single use, valid 5 minutes):</p>
        <pre className="mt-2 overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
{`uv run rootsignal-server dashboard-link --org <org> --project <project> \\
    --environment development --scopes read,triage`}
        </pre>
      </section>

      <section className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-800">Or: paste an API key once</h2>
        <p className="mt-1 text-sm text-slate-600">
          The key is exchanged for a session and not kept by the dashboard. It needs the{" "}
          <Code>read</Code> scope (<Code>triage</Code> to change issues).
        </p>
        <form onSubmit={submit} className="mt-3 space-y-3">
          <label className="block text-sm font-medium text-slate-700" htmlFor="api-key">API key</label>
          <input id="api-key" type="password" autoComplete="off" spellCheck={false} value={apiKey}
            onChange={(e) => setApiKey(e.target.value)} placeholder="rs_…"
            className="w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm focus:border-slate-500 focus:outline-none" />
          {error !== null && <ErrorBox error={error} />}
          <Button type="submit" variant="primary" disabled={busy || !apiKey.trim()}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </section>
    </div>
  );
}
