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
own `/health` and `/metrics` probes:

- Browser navigations to `/` pass the secret as `?secret=<value>`.
- The dashboard's own JS sends it as the `X-Sync-Secret` header on every fetch.
- Mutating requests (POST/PUT/etc.) must present the secret in the header; a query
  param is accepted only for safe methods (`GET`/`HEAD`).

If `SYNC_SECRET` is unset the dashboard returns `503` for every non-probe route
instead of serving data unauthenticated. `render.yaml` sets `generateValue: true`
for this key, so a Render deploy is protected by default.
