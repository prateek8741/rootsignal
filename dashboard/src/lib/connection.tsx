import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { api, type Context } from "../api/client";

/** A saved API key plus what it gives access to (one org/project/environment). */
export interface Connection {
  id: string;
  apiKey: string;
  context: Context;
}

interface ConnectionState {
  connections: Connection[];
  active: Connection | null;
  connect: (apiKey: string) => Promise<Connection>;
  select: (id: string) => void;
  disconnect: (id: string) => void;
}

// Keys live in sessionStorage: they survive reloads but not closing the tab, and
// are never written to localStorage. Any script on this origin could still read
// them, which is acceptable for a local dev dashboard (see docs/DASHBOARD.md).
const STORAGE_KEY = "rootsignal.connections";
const ACTIVE_KEY = "rootsignal.active";

function load(): Connection[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Connection[]) : [];
  } catch {
    return [];
  }
}

function save(connections: Connection[], activeId: string | null): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(connections));
    if (activeId) sessionStorage.setItem(ACTIVE_KEY, activeId);
    else sessionStorage.removeItem(ACTIVE_KEY);
  } catch {
    /* storage unavailable (private mode): keep state in memory only */
  }
}

export function connectionLabel(c: Context): string {
  return `${c.org_slug} / ${c.project_slug} / ${c.environment}`;
}

const ConnectionContext = createContext<ConnectionState | null>(null);

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [connections, setConnections] = useState<Connection[]>(load);
  const [activeId, setActiveId] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(ACTIVE_KEY);
    } catch {
      return null;
    }
  });

  const connect = useCallback(
    async (apiKey: string) => {
      const context = await api.context(apiKey.trim());
      const connection: Connection = { id: context.key_prefix, apiKey: apiKey.trim(), context };
      const next = [...connections.filter((c) => c.id !== connection.id), connection];
      setConnections(next);
      setActiveId(connection.id);
      save(next, connection.id);
      return connection;
    },
    [connections],
  );

  const select = useCallback(
    (id: string) => {
      setActiveId(id);
      save(connections, id);
    },
    [connections],
  );

  const disconnect = useCallback(
    (id: string) => {
      const next = connections.filter((c) => c.id !== id);
      const nextActive = activeId === id ? (next[0]?.id ?? null) : activeId;
      setConnections(next);
      setActiveId(nextActive);
      save(next, nextActive);
    },
    [connections, activeId],
  );

  const value = useMemo<ConnectionState>(
    () => ({
      connections,
      active: connections.find((c) => c.id === activeId) ?? null,
      connect,
      select,
      disconnect,
    }),
    [connections, activeId, connect, select, disconnect],
  );
  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

export function useConnections(): ConnectionState {
  const state = useContext(ConnectionContext);
  if (!state) throw new Error("useConnections must be used inside <ConnectionProvider>");
  return state;
}

/** The active connection; pages under <RequireConnection> can rely on it. */
export function useActiveConnection(): Connection {
  const { active } = useConnections();
  if (!active) throw new Error("no active connection");
  return active;
}
