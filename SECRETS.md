# Single-Source Secrets Store

One authoritative store, one resolution order, shared by every consumer:

- **job-agent** (Python) — `src/secret_store.py`
- **email-agent** (Node) — `src/lib/secrets.js`
- **AI Commander** (Electron) — owns the store and, in Phase 3, ships the `aicc-secrets` CLI.

This replaces the fragmented per-app credential loading that caused scheduled
reauth failures (see job-agent commit `55e1e55`, the `TODO(secrets)` band-aids).

## Resolution order

A secret is resolved in this order; **fill-missing, empty string treated as absent**:

1. **process / shell env** — a non-empty value always wins.
2. **project `.env`** — each app loads its own as it does today (job-agent with
   `override=True`; email-agent via its config). Authoritative over the central store.
3. **central store** — `~/Library/Application Support/ai-command-center/`, tried in order:
   1. `aicc-secrets get <NAME>` CLI, if on `PATH` — *Phase 3 target*
   2. `secrets.enc.env` decrypted via `sops -d` — *Phase 2 target*
   3. `.env` plaintext — *Phase 1 / today*
4. **Azure Key Vault** — *Phase 4*, gated by `USE_KEYVAULT=1`; **local/scheduled runs
   never take a network dependency at cred-load time** (that was the failure mode we removed).

"Empty treated as absent" is load-bearing: Claude Code sets `ANTHROPIC_API_KEY=""` in the
shell, and a naive `override=False`/`??` load would treat that as "present" and refuse to fill.

Override `AICC_SECRETS_DIR` to relocate the store. **This is how the encrypted
store is reached in practice:** `secrets.enc.env` lives in the `aicc-secrets`
git repo (ciphertext only — see its README), not in the platform-default
`Application Support` dir, so every consumer must have `AICC_SECRETS_DIR`
pointing at that checkout. For launchd runs `scripts/manage-autopilot.sh install`
writes it into the plists (exported value → sibling `aicc-secrets` checkout →
empty/default). Without it the resolver silently reads only the plaintext `.env`.

### Store-authoritative keys (ACES-282)

Steps 1–2 do **not** apply to shared AI-service credentials — `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `AICC_OPENROUTER_API_KEY`, `OPENROUTER_GATEWAY_URL`
(`secret_store.STORE_AUTHORITATIVE_KEYS`). Those are owned by the central store and
rotated in one place; when the store holds a value it **replaces** whatever the process
env or project `.env` carried, and the resolver logs a warning naming the key. A local
copy of one of these is a misconfiguration to remove, not a fallback: with fill-missing
alone, every local copy of `ANTHROPIC_API_KEY` masked the store — on 2026-09-04 all three
copies were identical and the key was already rejected by Anthropic (HTTP 401), and a
rotation in the store would never have reached the agent. **Every agent** (job-agent,
email-agent `src/lib/secrets.js`, the desktop app) is expected to apply the same rule.

## Canonical key catalog

| Key | job-agent | email-agent |
|-----|:---------:|:-----------:|
| `ANTHROPIC_API_KEY` | ✓ | ✓ |
| `OPENAI_API_KEY` | ✓ | ✓ |
| `JOBRIGHT_EMAIL` / `JOBRIGHT_PASSWORD` | ✓ | |
| `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` | ✓ | |
| `INDEED_EMAIL` / `INDEED_PASSWORD` | ✓ | |
| `USAJOBS_EMAIL` / `USAJOBS_PASSWORD` / `USAJOBS_2FA_SECRET` | ✓ | |
| `IMAP_PASSWORD` / `ICLOUD_APP_PASSWORD_PERSONAL` / `ICLOUD_APP_PASSWORD` / `ICLOUD_APP_PASSWORD_ICLOUD` / `ICLOUD_APP_PASSWORD_MAC` / `EMAIL_2FA_ADDRESS` / `IMAP_USER` — IMAP *app-specific* password (+ address) for email-code 2FA and confirmation tracking; resolved by `email_helper.resolve_imap_credentials`, which never falls back to a site login password | ✓ | |
| `COMPANY_EMAIL(_ALT)` / `COMPANY_PASSWORD(_ALT)` | ✓ | |
| `DASHBOARD_URL` / `SYNC_SECRET` / `CREDENTIAL_ENCRYPTION_KEY` | ✓ | |
| `NOTIFY_PHONE` / `TWILIO_*` / `APPROVAL_*` | ✓ | |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | | ✓ |
| `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` / `MICROSOFT_TENANT_ID` | | ✓ |
| `ENCRYPTION_KEY` / `SESSION_SECRET` | | ✓ |
| `APP_PASSWORD` / `EMAIL_AGENT_PASSWORD` | | ✓ |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | ✓ | ✓ |

Keep the lists in `CANONICAL_KEYS` (both repos) in sync for the shared keys.

## Migration phases

- **Phase 0 — done.** `.env` authoritative + presence logging (band-aids).
- **Phase 1 — this change.** Shared resolver in both agents; every entry path
  (job-agent `main.load_env` + `Orchestrator.__init__`; email-agent `cli.js` +
  `server.js` via `loadSecrets.js`) fills from the central store. Readers already
  understand `secrets.enc.env` and the `aicc-secrets` CLI, so Phases 2–3 need no
  further wiring in the agents. **Run `scripts/consolidate-secrets.sh` to populate
  the central store** from the existing per-app `.env` files.
- **Phase 2 — encrypt at rest (SOPS + age). Done, in the `aicc-secrets` repo.**
  The age key is a `0600` file at `~/.config/aicc/age.key` so launchd/headless runs
  decrypt offline; `secrets.enc.env` is committed (ciphertext) to `aicc-secrets`.
  What was missing until ACES-65 was the *wiring*: consumers must set
  `AICC_SECRETS_DIR` to that checkout (see above), and launchd needs both
  `SOPS_AGE_KEY_FILE` and `AICC_SECRETS_DIR` in the plist env. Rotate a secret with
  `sops secrets.enc.env` in that repo, commit, push.
- **Cloud-dashboard credential fetch — retired (ACES-65).** `GET /api/credentials`
  (which returned decrypted passwords to any `SYNC_SECRET` holder) and
  `Orchestrator.load_credentials_from_dashboard()` (a per-run network call at
  cred-load time that always ended in "Kept local .env") are deleted. The
  dashboard still stores credentials for its own UI via `POST /api/credentials`;
  the agent never reads them.
- **Phase 3 — `aicc-secrets` CLI** and **Phase 4 — Azure Key Vault**: dropped from
  scope. No consumer needs them; the resolver keeps the `aicc-secrets get` and
  `USE_KEYVAULT` hooks only so nothing breaks if either ever appears.

## Security notes

- The store holds live secrets. Until Phase 2 it is plaintext — `chmod 600` it
  (the consolidation script does this).
- Values are never logged; both resolvers log **key names only**.
- The store lives under `Application Support`, outside any git repo. Never commit it.
