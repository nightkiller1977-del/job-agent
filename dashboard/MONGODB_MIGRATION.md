# MongoDB dashboard migration

The Job Agent dashboard now uses MongoDB Atlas for cloud persistence while the local agent may retain SQLite as a zero-cost offline/journal store.

## Target

- Atlas cluster: existing AI Commander `Cluster0`
- Database: `job_agent`
- Dashboard env: `MONGODB_URI`, `JOB_AGENT_DB=job_agent`
- Render Postgres `job-agent-db`: retire only after data parity and dashboard validation

## Migration sequence

1. Configure `MONGODB_URI` using a least-privilege Atlas user scoped to `job_agent`.
2. Deploy the MongoDB-backed dashboard.
3. Backfill legacy Render Postgres `jobs`, `sync_log`, and encrypted credential records if they are still needed.
4. Validate job counts/statuses, pending/approved/applied views, sync health, and local agent pull/push behavior.
5. Remove the `DATABASE_URL` dependency and delete `job-agent-db` only after validation.

## Collections

- `jobs` — dashboard job state and scoring metadata; unique `job_id`
- `sync_events` — cloud synchronization audit events
- `credentials` — encrypted credential payloads only; writes fail closed if `CREDENTIAL_ENCRYPTION_KEY` is missing
- `job_relationships` — graph-style edges for future job/company/skill/application intelligence

The dashboard API routes are preserved so the local agent and UI do not need a protocol rewrite.
