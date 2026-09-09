# Dashboard persistence architecture

```text
Local Job Agent
  └─ SQLite (offline/journal, no cloud cost)
       │ sync/action API
       ▼
Render Job Dashboard (compute only)
       │
       ▼
MongoDB Atlas / Cluster0 / job_agent
  ├─ jobs
  ├─ sync_events
  ├─ credentials (encrypted only)
  └─ job_relationships (graph-style edges)
```

This removes the need for a dedicated Render Postgres database while retaining local resilience.
