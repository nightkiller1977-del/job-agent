# Dashboard operational telemetry

The Dashboard is a separate runtime from the Job Agent CLI. Its instrumented
entry point is `observed:app`, not the legacy `main:app` application module.
`render.yaml` starts it with:

```sh
cd dashboard
uvicorn observed:app --host 0.0.0.0 --port "$PORT"
```

From the repository root use `uvicorn dashboard.observed:app` instead. The wrapper
preserves the original application routes and database startup handler, and
observes HTTP and ASGI lifecycle outcomes without reading request bodies,
headers, query strings, job records, mailbox contents or credentials.

The runtime consumes the existing `LOKI_URL_REMOTE` and `LOKI_REMOTE_AUTH` process
environment variables populated by the authorized platform/SOPS workflow. No
new Grafana key is required. Both variables are resolved **atomically** by the
repo-shared `src/loki_config.resolve_loki_config()` (also used by the CLI's
`src/telemetry.py`): export is enabled only when both are present from one
authority and valid — `LOKI_REMOTE_AUTH` must be `Basic <base64(user:password)>`
with non-empty user/password. A partial or malformed pair disables export with
a single warning (values are never logged) and never starts the export worker.
The legacy split-auth names `LOKI_USER`/`LOKI_API_KEY` are retired.

Default policy (ACES-293): remote export auto-enables in production — detected
via the `RENDER` env var, which Render sets on every service — and is off in
dev/test unless `OBSERVABILITY_REMOTE=1`. `OBSERVABILITY_REMOTE=0` opts out
even in production. Local logging is independent and unchanged.

The Dashboard exporter has one worker and a queue of 64 events. Overflow is
best-effort loss, not additional threads or blocked application requests. HTTP
export requires HTTPS, rejects redirects and has a finite socket timeout.
Shutdown flushing has a 1.75-second deadline. Failed delivery is not reported as
successful ingestion. A full/slow exporter can drop events; this is operational
telemetry, not an audit ledger.

A deployed service with a manually overridden Render start command must use
`uvicorn observed:app ...` as well. Changing a repository blueprint does not prove
that an existing service adopted it. Validate its deployed command/revision and
query a fresh Dashboard event in Grafana before declaring the cutover complete.
Launching `main:app` directly bypasses this wrapper.

Run focused offline tests from the repository root:

```sh
python tests/test_observability_delivery.py
python tests/test_observability_wiring.py
```

The tests use synthetic credentials and mock the SOPS executable/HTTP transport.
They do not validate the actual encrypted secret values or live Grafana access.
