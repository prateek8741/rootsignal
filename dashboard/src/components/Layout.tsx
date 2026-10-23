import { useState } from "react";
import { NavLink, Navigate, Outlet, useNavigate } from "react-router-dom";
import { sessionLabel, useAuth } from "../lib/auth";
import { ErrorBox, Loading } from "./ui";

export function RequireAuth() {
  const { status, error } = useAuth();
  if (status === "loading") return <Loading label="Checking your session…" />;
  if (status === "error") return <div className="p-8"><ErrorBox error={error} /></div>;
  if (status === "signed-out") return <Navigate to="/login" replace />;
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
  const { context, signOut } = useAuth();
  const navigate = useNavigate();
  const [signingOut, setSigningOut] = useState(false);
  if (!context) return null;
  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col bg-slate-900 px-3 py-4 text-slate-100">
        <div className="px-3 pb-4 text-base font-semibold tracking-tight">Rootsignal</div>
        <nav className="space-y-1" aria-label="Main">
          <NavItem to="/traces" label="Traces" />
          <NavItem to="/issues" label="Issues" />
        </nav>
        <div className="mt-auto space-y-2 border-t border-slate-800 px-2 pt-4 text-xs">
          <p className="text-slate-400">Environment</p>
          <p className="font-medium text-slate-100" data-testid="environment">{sessionLabel(context)}</p>
          <p className="text-slate-400">{context.scopes.join(", ")} · {context.principal}</p>
          <button type="button" disabled={signingOut} className="text-slate-300 underline disabled:opacity-50"
            onClick={() => {
              setSigningOut(true);
              void signOut().finally(() => navigate("/login", { replace: true }));
            }}>
            Sign out
          </button>
          <p className="text-slate-500">To switch environment, sign out and use that environment's login link.</p>
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-8 py-6">
        <Outlet />
      </main>
    </div>
  );
}
