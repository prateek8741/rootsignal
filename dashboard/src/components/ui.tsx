import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api/client";

const TONES = {
  gray: "bg-slate-100 text-slate-700 ring-slate-200",
  red: "bg-red-50 text-red-700 ring-red-200",
  orange: "bg-orange-50 text-orange-700 ring-orange-200",
  amber: "bg-amber-50 text-amber-800 ring-amber-200",
  blue: "bg-sky-50 text-sky-700 ring-sky-200",
  green: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  violet: "bg-violet-50 text-violet-700 ring-violet-200",
} as const;
export type Tone = keyof typeof TONES;

export function Badge({ tone = "gray", children, title }: {
  tone?: Tone; children: ReactNode; title?: string;
}) {
  return (
    <span title={title}
      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset whitespace-nowrap ${TONES[tone]}`}>
      {children}
    </span>
  );
}

const SEVERITY_TONE: Record<string, Tone> = { critical: "red", high: "orange", medium: "amber", low: "gray" };

export function SeverityBadge({ severity }: { severity: string }) {
  return <Badge tone={SEVERITY_TONE[severity] ?? "gray"}>{severity}</Badge>;
}

export function TraceStatusBadge({ status }: { status: string }) {
  if (status === "error") return <Badge tone="red">error</Badge>;
  if (status === "ok") return <Badge tone="green">ok</Badge>;
  // "unset" is what successful runs report (instrumentation leaves status unset).
  return <Badge tone="gray" title="No error was reported (span status: unset)">no error</Badge>;
}

export function IssueStatusBadge({ status, regression }: { status: string; regression?: boolean }) {
  const tone: Tone = status === "open" ? "blue" : status === "resolved" ? "green" : "gray";
  return (
    <span className="inline-flex gap-1">
      <Badge tone={tone}>{status}</Badge>
      {regression && <Badge tone="violet" title="Reopened after being resolved">regression</Badge>}
    </span>
  );
}

const KIND_TONE: Record<string, Tone> = {
  agent: "violet", llm: "blue", tool: "amber", mcp: "amber", retrieval: "green",
};

export function KindBadge({ kind }: { kind: string }) {
  return <Badge tone={KIND_TONE[kind] ?? "gray"}>{kind}</Badge>;
}

export function Button({ variant = "secondary", className = "", ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" }) {
  const styles = {
    primary: "bg-slate-900 text-white hover:bg-slate-700 disabled:bg-slate-300",
    secondary: "bg-white text-slate-800 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 disabled:text-slate-400",
    danger: "bg-red-600 text-white hover:bg-red-500 disabled:bg-red-300",
  }[variant];
  return (
    <button type="button" {...props}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed ${styles} ${className}`} />
  );
}

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`rounded-lg border border-slate-200 bg-white ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-slate-200 px-4 py-2.5">
          <h2 className="text-sm font-semibold text-slate-800">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div role="status" className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
      <p className="text-sm font-semibold text-slate-800">{title}</p>
      {children && <div className="mx-auto mt-2 max-w-xl text-sm text-slate-600">{children}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <p role="status" className="py-8 text-center text-sm text-slate-500">{label}</p>;
}

export function ErrorBox({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  const status = error instanceof ApiError ? error.status : null;
  return (
    <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
      <p className="font-medium">{status === 401 ? message || "Your session has ended." : message}</p>
      {status === 401 && <Link to="/login" className="mt-1 inline-block underline">Sign in again</Link>}
    </div>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div title={hint}>
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium text-slate-900">{value}</dd>
    </div>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-xs">{children}</code>;
}
