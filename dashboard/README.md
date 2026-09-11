# Job Agent Dashboard

The Render-hosted dashboard is a FastAPI review/control surface for Job Agent.

Cloud persistence is MongoDB Atlas via `MONGODB_URI`; `JOB_AGENT_DB` defaults to `job_agent`. The dashboard no longer requires a Render Postgres database. Local Job Agent SQLite state remains separate as a zero-cost offline/journal store.

Required runtime configuration:

- `MONGODB_URI`
- `JOB_AGENT_DB=job_agent`
- `SYNC_SECRET`
- `CREDENTIAL_ENCRYPTION_KEY` if dashboard credential saving is enabled

Credential writes fail closed when the encryption key is missing; plaintext password persistence is not permitted.
