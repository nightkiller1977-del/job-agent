# Cutover checklist

- [ ] Configure `MONGODB_URI` for `job_agent_svc` in Render/aicc-secrets.
- [ ] Deploy the MongoDB-backed dashboard.
- [ ] Backfill legacy Render Postgres data.
- [ ] Compare counts and statuses.
- [ ] Verify pending/approved/applied views and sync API.
- [ ] Verify local agent approval pull and result sync.
- [ ] Confirm `/health` reports `backend=mongodb` and `database=ok`.
- [ ] Remove Postgres dependency from live Render configuration.
- [ ] Delete `job-agent-db` only after all checks pass.
