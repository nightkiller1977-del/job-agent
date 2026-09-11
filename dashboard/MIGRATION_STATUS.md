# Migration status

Implementation branch: `feat/mongodb-dashboard-consolidation`

Implemented in code:

- MongoDB Atlas is the dashboard cloud persistence backend.
- Render blueprint no longer declares or injects a Postgres database.
- Existing dashboard API routes and `job_id` upsert/status semantics are preserved.
- Atlas indexes are created at startup.
- `job_relationships` is reserved for graph-style job/company/skill/application edges.
- Credential writes fail closed without encryption instead of storing plaintext.
- Local SQLite remains unchanged for offline/journal behavior.

Operational cutover still requires data backfill/parity validation before deleting the legacy Render Postgres instance.
