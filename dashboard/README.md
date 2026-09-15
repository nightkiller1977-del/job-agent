# Job Agent Dashboard

The Render-hosted dashboard is a FastAPI review/control surface for Job Agent.

Cloud persistence is MongoDB Atlas via `MONGODB_URI`; `JOB_AGENT_DB` defaults to `job_agent`. The dashboard no longer requires a Render Postgres database. Local Job Agent SQLite state remains separate as a zero-cost offline/journal store.

Required runtime configuration:

- `MONGODB_URI`
- `JOB_AGENT_DB=job_agent`
- `SYNC_SECRET`
- `CREDENTIAL_ENCRYPTION_KEY` if dashboard credential saving is enabled

Credential writes fail closed when the encryption key is missing; plaintext password persistence is not permitted.

## Authentication

The dashboard is **fail-closed**. `SYNC_SECRET` gates every route except the app's
own `/health` and `/metrics` probes and the `/login` exchange:

- **Browsers** sign in once at `/login` with the sync secret. The server sets an
  `HttpOnly; SameSite=Strict; Secure` session cookie, so the secret never appears
  in a URL, browser history, or access log, and page scripts cannot read it.
- **Machine callers** (the orchestrator's sync calls in `src/orchestrator.py`)
  keep sending the secret as the `X-Sync-Secret` header.
- A browser navigation without a session is redirected to `/login`; API calls
  without a valid session or header get `403`.

If `SYNC_SECRET` is unset the dashboard returns `503` for every non-probe route
instead of serving data unauthenticated. `render.yaml` sets `generateValue: true`
for this key, so a Render deploy is protected by default. Rotating the secret
invalidates all existing sessions.
