import { NavLink, Navigate, Outlet, useNavigate } from "react-router-dom";
import { connectionLabel, useConnections } from "../lib/connection";

export function RequireConnection() {
  const { active } = useConnections();
  if (!active) return <Navigate to="/connect" replace />;
  return <Layout />;
}

function NavItem({ to, label }: { to: string; label: string }) {
  return (
    <NavLink to={to}
      className={({ isActive }) =>
        `block rounded-md px-3 py-1.5 text-sm ${isActive ? "bg-slate-800 text-white" : "text-slate-300 hover:bg-slate-800/60 hover:text-white"}`}>
      {label}
    </NavLink>
  );
}

function Layout() {
  const { connections, active, select, disconnect } = useConnections();
  const navigate = useNavigate();
  if (!active) return null;
  const { context } = active;
  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col bg-slate-900 px-3 py-4 text-slate-100">
        <div className="px-3 pb-4 text-base font-semibold tracking-tight">Rootsignal</div>
        <nav className="space-y-1" aria-label="Main">
          <NavItem to="/traces" label="Traces" />
          <NavItem to="/issues" label="Issues" />
        </nav>
        <div className="mt-auto space-y-2 border-t border-slate-800 px-1 pt-4 text-xs">
          <label className="block text-slate-400" htmlFor="connection">Environment</label>
          <select id="connection" value={active.id}
            onChange={(e) => {
              select(e.target.value);
              navigate("/traces");
            }}
            className="w-full rounded-md border border-slate-700 bg-slate-800 px-2 py-1.5 text-slate-100">
            {connections.map((c) => (
              <option key={c.id} value={c.id}>{connectionLabel(c.context)}</option>
            ))}
          </select>
          <p className="text-slate-400">
            Key <span className="font-mono">{context.key_prefix}…</span> · {context.scopes.join(", ")}
          </p>
          <div className="flex gap-3">
            <button type="button" className="text-slate-300 underline" onClick={() => navigate("/connect")}>
              Add key
            </button>
            <button type="button" className="text-slate-300 underline" onClick={() => disconnect(active.id)}>
              Disconnect
            </button>
          </div>
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-8 py-6">
        <Outlet />
      </main>
    </div>
  );
}
