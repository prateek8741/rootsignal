# Dashboard

Status: v1. Built in increment 5; sign-in replaced and investigation panel added in increment 6. Code: `dashboard/` (React 19, TypeScript 5.9 strict, Vite 8, TanStack Query, React Router, Tailwind CSS 4).

## Run it

```bash
make serve          # API on http://127.0.0.1:8000
make worker         # detection + issue grouping
make seed-demo      # optional: ~40 demo runs from four agents; prints a one-time login link
make dashboard      # http://127.0.0.1:5173
```

Open the login link printed by the seed script or by `rootsignal-server dashboard-link` (see below).

The Vite dev server forwards `/v1` to the API (`ROOTSIGNAL_API` overrides the target), so the browser only ever talks to its own origin and the API needs no CORS. The dev server binds to `127.0.0.1` only.

## Screens

| Screen | What it shows |
|---|---|
| Sign in | One-time link (`/login/link#token=…`) or paste a key once; both become an httpOnly session |
| Traces | Filter by status, agent, version and time range; finding count, errored spans, duration, spans, tokens, cost (`≥` when some model calls were unpriced); cursor paging. Separate empty states for "no traces yet" (with an SDK snippet) and "no matches" |
| Trace | Summary, findings (click to highlight evidence spans; link to the issue), execution waterfall (tree + timeline; errors in red; "repeat #n" for identical calls; write side effects; orphans), span details (model/tokens/cost, side effect, args hash, redacted exception, attributes) with payloads fetched on demand, showing truncation and redaction state |
| Issues | Status tabs (open default), severity filter, sort by recency, frequency or severity; regression badge; affected versions |
| Issue | Description, counts, first/latest/worst trace links, resolve (with optional fixed-in version and note), ignore or reopen (disabled without `triage`), **investigation panel** (claims by kind, AI suggestions marked, evidence chips linking to spans, discarded suggestions with reasons), version breakdown, history with actors, occurrences |

## Signing in and security

Since increment 6 the dashboard **never holds an API key or any other credential in JavaScript**. Increment 5 kept keys in `sessionStorage`; that's gone, and a test fails if `localStorage`, `sessionStorage`, `indexedDB` or `document.cookie` appear anywhere in dashboard code.

- **One-time login link (recommended):** `uv run rootsignal-server dashboard-link --org <org> --project <project> [--environment …] [--scopes read,triage]` prints `http://127.0.0.1:5173/login/link#token=…`. The link is valid once, for 5 minutes. The token sits in the URL *fragment*, which browsers never send to servers or proxies, and the page erases it from the address bar and history before using it. `make seed-demo` prints one too.
- **Paste a key once:** the login page sends it to `POST /v1/auth/session`, gets a session, and clears the field. The key isn't stored anywhere in the browser.
- **The session** is an `HttpOnly; SameSite=Strict` cookie scoped to `/v1` that page scripts can't read. Every request also sends `X-Rootsignal-CSRF: 1`, which other sites can't add.
- **Sessions end** after 12 h, or 2 h idle, on *Sign out*, on `rootsignal-server revoke-sessions …`, or when the issuing key is revoked. Each session is one environment; to switch, sign out and use that environment's link.
- **Not yet:** user accounts, SSO and per-user permissions (Phase 2). Over HTTPS, set `ROOTSIGNAL_SESSION_COOKIE_SECURE=true`.
- **Trace content is untrusted.** React escapes everything, and no HTML or markdown from traces is rendered.

## Contracts and tests

- **API types are generated, not hand-written.** `npm run gen:api` exports the OpenAPI document (`scripts/export_openapi.py`) and runs `openapi-typescript`. A server test (`test_openapi_sync.py`) fails if the committed schema is stale.
- **`make test-dashboard`** runs `tsc --noEmit` and Vitest, 29 tests. Increment 6 added: link and key sign-in, sign-out, the storage guard, the investigation panel and `?span=` deep links. The increment 5 tests cover:
  - the span tree (ordering, ties, timeline maths, orphans, cycles);
  - formatting (lower-bound costs);
  - pages against a mocked API: connect flow, bad key, both empty states, filters reaching the API, evidence highlighting, triage-scope gating, the resolve request body.
- **`npm run e2e`** is a Playwright smoke test in the locally installed Chrome against the real stack: sign in with a link, check the cookie is `HttpOnly`/`Strict` and invisible to scripts, open a failing trace, run an investigation, follow an evidence link to its span, sign out, and confirm the session is gone. It needs `DASHBOARD_LOGIN_URL` from `rootsignal-server dashboard-link`. It isn't part of `make check` because it needs running servers. Last run: passed on 2026-10-23, with screenshots reviewed by hand. Those screenshots led to fixes for waterfall badge overflow, the "unset" status wording, and a test assertion that passed on the wrong page.

## Not yet

- Charts and trends.
- Live updates (polling or streaming).
- Search inside payloads.
- Production serving of `dashboard/dist`.
- User accounts and SSO.
- Dark mode.
- Accessibility audit beyond semantic roles and labels.
