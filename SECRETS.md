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

Override `AICC_SECRETS_DIR` to relocate the store (used by tests).

## Canonical key catalog

| Key | job-agent | email-agent |
|-----|:---------:|:-----------:|
| `ANTHROPIC_API_KEY` | ✓ | ✓ |
| `OPENAI_API_KEY` | ✓ | ✓ |
| `JOBRIGHT_EMAIL` / `JOBRIGHT_PASSWORD` | ✓ | |
| `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` | ✓ | |
| `INDEED_EMAIL` / `INDEED_PASSWORD` | ✓ | |
| `USAJOBS_EMAIL` / `USAJOBS_PASSWORD` / `USAJOBS_2FA_SECRET` / `IMAP_PASSWORD` | ✓ | |
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
- **Phase 2 — encrypt at rest (SOPS + age).** Generate an age key, store the private
  key in macOS Keychain **with a `0600` key-file fallback** so launchd/headless runs
  can still decrypt offline. Encrypt the plaintext `.env` → `secrets.enc.env`:
  ```sh
  age-keygen -o ~/.config/aicc/age.key          # 0600; back up the public key
  export SOPS_AGE_KEY_FILE=~/.config/aicc/age.key
  sops --encrypt --age <public-key> \
    "~/Library/Application Support/ai-command-center/.env" \
    > "~/Library/Application Support/ai-command-center/secrets.enc.env"
  # verify: sops -d secrets.enc.env  → then shred the plaintext .env
  ```
- **Phase 3 — `aicc-secrets` CLI.** AI Commander ships `aicc-secrets get <NAME>`
  (prints value to stdout, exit 0; non-zero if absent). Agents already prefer it.
  Retire the cloud-dashboard credential fetch and the duplicate readers.
- **Phase 4 — Azure Key Vault.** Optional cloud tier for the Render/hybrid deploy,
  gated by `USE_KEYVAULT=1`.

## Security notes

- The store holds live secrets. Until Phase 2 it is plaintext — `chmod 600` it
  (the consolidation script does this).
- Values are never logged; both resolvers log **key names only**.
- The store lives under `Application Support`, outside any git repo. Never commit it.
