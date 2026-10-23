import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import { ApiError, api, type Context } from "../api/client";

/**
 * Who is signed in, as reported by the server. The session itself is an
 * httpOnly cookie: nothing here (or anywhere in the dashboard) stores a
 * credential in JavaScript-readable storage.
 */
interface AuthState {
  status: "loading" | "signed-out" | "signed-in" | "error";
  context: Context | null;
  error: unknown;
  signInWithKey: (apiKey: string) => Promise<Context>;
  signInWithLink: (token: string) => Promise<Context>;
  signOut: () => Promise<void>;
}

const SESSION_QUERY = ["auth", "session"] as const;
const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const session = useQuery({
    queryKey: SESSION_QUERY,
    queryFn: async () => {
      try {
        return await api.session();
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    retry: false,
    staleTime: 60_000,
  });

  const adopt = useCallback(
    (context: Context) => {
      queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
      queryClient.setQueryData(SESSION_QUERY, context);
      return context;
    },
    [queryClient],
  );

  const signInWithKey = useCallback(
    async (apiKey: string) => adopt(await api.signInWithKey(apiKey)),
    [adopt],
  );
  const signInWithLink = useCallback(
    async (token: string) => adopt(await api.signInWithLink(token)),
    [adopt],
  );
  const signOut = useCallback(async () => {
    try {
      await api.signOut();
    } finally {
      // Drop all cached data, but keep the session query itself: the auth hook is
      // subscribed to it, and removing it (queryClient.clear()) left the UI showing
      // the old signed-in state.
      queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
      queryClient.setQueryData(SESSION_QUERY, null);
    }
  }, [queryClient]);

  const value = useMemo<AuthState>(() => {
    const status = session.isPending ? "loading"
      : session.isError ? "error"
      : session.data ? "signed-in" : "signed-out";
    return { status, context: session.data ?? null, error: session.error, signInWithKey,
      signInWithLink, signOut };
  }, [session.isPending, session.isError, session.data, session.error, signInWithKey,
    signInWithLink, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const state = useContext(AuthContext);
  if (!state) throw new Error("useAuth must be used inside <AuthProvider>");
  return state;
}

/** The signed-in context; only for pages rendered behind <RequireAuth>. */
export function useSession(): Context {
  const { context } = useAuth();
  if (!context) throw new Error("not signed in");
  return context;
}

export function sessionLabel(c: Context): string {
  return `${c.org_slug} / ${c.project_slug} / ${c.environment}`;
}
