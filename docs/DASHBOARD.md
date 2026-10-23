# Dashboard

Status: v0, implemented in increment 5. Code: `dashboard/` (React 19, TypeScript 5.9 strict, Vite 8, TanStack Query, React Router, Tailwind CSS 4).

## Run it

```bash
make serve          # API on http://127.0.0.1:8000
make worker         # detection + issue grouping
make seed-demo      # optional: ~40 demo runs from four agents; prints a read+triage key
make dashboard      # http://127.0.0.1:5173
```

Open the dashboard and paste a key with the `read` scope. Add `triage` to resolve, ignore or reopen issues. Create one with:

```bash
uv run rootsignal-server create-key --org <org> --project <project> --name dashboard --scopes read,triage
```

The Vite dev server forwards `/v1` to the API (`ROOTSIGNAL_API` overrides the target), so the browser only ever talks to its own origin and the API needs no CORS. The dev server binds to `127.0.0.1` only.

## Screens

| Screen | What it shows |
|---|---|
| Connect | Validate a key via `GET /v1/context`; saved keys in this tab; how to get a key |
| Traces | Filter by status, agent, version and time range; finding count, errored spans, duration, spans, tokens, cost (`≥` when some model calls were unpriced); cursor paging. Separate empty states for "no traces yet" (with an SDK snippet) and "no matches" |
| Trace | Summary, findings (click to highlight evidence spans; link to the issue), execution waterfall (tree + timeline; errors in red; "repeat #n" for identical calls; write side effects; orphans), span details (model/tokens/cost, side effect, args hash, redacted exception, attributes) with payloads fetched on demand, showing truncation and redaction state |
| Issues | Status tabs (open default), severity filter, sort by recency, frequency or severity; regression badge; affected versions |
| Issue | Description, counts, first/latest/worst trace links, resolve (with optional fixed-in version and note), ignore or reopen (disabled without `triage`), version breakdown, history with actors, occurrences |

## Security model (v0) and its limits

- **Keys stay in `sessionStorage`,** not `localStorage`. They survive a reload, not closing the tab, and are only sent to the dashboard's own origin. Any script running on that origin could still read them. That's acceptable for a loopback dev tool, but **not for a hosted deployment**, which needs real user sessions (Phase 2: user accounts, httpOnly cookies, CSRF protection).
- **The UI hides actions a key can't perform; the API is the enforcement point.** Every scope and tenant check is server-side.
- **Payload bodies are rendered as text.** React escapes them, and no HTML or markdown from traces is ever rendered. Trace content is untrusted.

## Contracts and tests

- **API types are generated, not hand-written.** `npm run gen:api` exports the OpenAPI document (`scripts/export_openapi.py`) and runs `openapi-typescript`. A server test (`test_openapi_sync.py`) fails if the committed schema is stale.
- **`make test-dashboard`** runs `tsc --noEmit` and Vitest, 22 tests:
  - the span tree (ordering, ties, timeline maths, orphans, cycles);
  - formatting (lower-bound costs);
  - pages against a mocked API: connect flow, bad key, both empty states, filters reaching the API, evidence highlighting, triage-scope gating, the resolve request body.
- **`npm run e2e`** is a Playwright smoke test in the locally installed Chrome against the real stack. It needs `DASHBOARD_KEY` from the seed script. It isn't part of `make check` because it needs running servers. Last run: passed on 2026-10-23, with screenshots reviewed by hand. Those screenshots led to fixes for waterfall badge overflow, the "unset" status wording, and a test assertion that passed on the wrong page.

## Not yet

- Charts and trends.
- Live updates (polling or streaming).
- Search inside payloads.
- Production serving of `dashboard/dist`.
- User accounts and SSO.
- Dark mode.
- Accessibility audit beyond semantic roles and labels.
