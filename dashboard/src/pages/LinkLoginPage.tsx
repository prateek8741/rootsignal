import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ErrorBox, Loading } from "../components/ui";
import { useAuth } from "../lib/auth";

/** Read the token from the URL fragment (never sent to servers) and erase it at once. */
function takeTokenFromFragment(): string | null {
  const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
  window.history.replaceState(null, "", window.location.pathname);
  return token;
}

export function LinkLoginPage() {
  const { signInWithLink } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<unknown>(null);
  const started = useRef(false); // links are single use: never redeem twice (StrictMode)

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = takeTokenFromFragment();
    if (!token) {
      setError(new Error("This link has no login token. Generate a new one with the CLI."));
      return;
    }
    signInWithLink(token)
      .then(() => navigate("/traces", { replace: true }))
      .catch(setError);
  }, [signInWithLink, navigate]);

  return (
    <div className="mx-auto max-w-lg px-6 py-16">
      {error === null ? <Loading label="Signing you in…" /> : (
        <div className="space-y-3">
          <ErrorBox error={error} />
          <Link to="/login" className="text-sm text-sky-700 underline">Other ways to sign in</Link>
        </div>
      )}
    </div>
  );
}
