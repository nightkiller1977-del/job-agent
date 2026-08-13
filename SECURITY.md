# Security Policy

## Notice: Git History Cleanup (August 2026)

A `.env` file was previously committed to git history in this private repository. It was removed using `git filter-repo` before the repository was made public. If you forked or cloned the repo before August 13, 2026, rotate any credentials that were in your `.env` file.

---

## Reporting Vulnerabilities

**Do not open a public issue for security vulnerabilities.**

If you discover a security vulnerability in job-agent, please email security details to the maintainers rather than using the public issue tracker.

### What to Include
- Description of the vulnerability
- Steps to reproduce (if applicable)
- Potential impact
- Suggested remediation (if you have one)

---

## Security Best Practices

### Environment Configuration
- **Never commit `.env` files** to the repository. Use `.env.example` as a template only.
- All sensitive configuration (API keys, passwords, tokens, encryption keys) must be provided via environment variables at runtime.
- In development, use a local `.env` file (ignored by `.gitignore`).
- In production, use your deployment platform's secrets management (Render environment variables, etc.).
- In Phase 2+, use encrypted secrets via `secrets.enc.env` and `aicc-secrets` CLI.

### Credentials Management
- Never hardcode credentials in source code, test files, or documentation.
- Use the centralized secrets resolver in `src/secret_store.py`.
- Follow the resolution order: env vars → local `.env` → central store → cloud (Phase 4).
- All credentials are rotated per the security incident response plan.

### Code Security
- All external inputs are validated before processing.
- No sensitive data (passwords, API keys, tokens, PII) should be logged.
- Use proper error handling that doesn't expose implementation details.
- Dependencies are regularly audited for vulnerabilities.

### Testing
- Test credentials should never be hardcoded in source files.
- Use environment variable overrides or pytest fixtures for test-specific credentials.
- Never document real or example credentials in README or reports.

### Deployment Security
- Container images use minimal base images (Python slim).
- Health checks and proper startup sequences prevent bad state propagation.
- Logging levels are set to INFO or above in production (never DEBUG).
- Deployment manifests use environment variable substitution for secrets.
- All credentials stored in Render environment variables use "Sync: false" for sensitive values.

### Encryption
- Phase 2+: All stored credentials encrypted with age/sops
- Encryption keys stored in macOS Keychain (with 0600 file fallback for headless runs)
- Credential encryption key (Fernet) rotated per incident response

---

## Security Architecture (Phases)

### Phase 0 (Legacy) ✅ Complete
- Per-app `.env` files with presence logging

### Phase 1 (Current) 🔄 In Progress
- Centralized secrets resolver in `src/secret_store.py`
- Shared across job-agent and email-agent
- Run `scripts/consolidate-secrets.sh` to populate central store

### Phase 2 (Planned)
- Encrypt at rest using SOPS + age
- Private key stored in macOS Keychain with 0600 fallback
- Generate age key: `age-keygen -o ~/.config/aicc/age.key`

### Phase 3 (Planned)
- `aicc-secrets` CLI shipped by AI Commander
- Agents prefer this over file-based resolution

### Phase 4 (Optional)
- Azure Key Vault for cloud deployments
- Gated by `USE_KEYVAULT=1`

---

## Security Audit Checklist

- [ ] No hardcoded API keys in source code
- [ ] No hardcoded passwords in test fixtures or documentation
- [ ] `.env.example` uses placeholders only
- [ ] All credentials come from environment variables or secret store
- [ ] `.gitignore` properly excludes `.env` files
- [ ] No sensitive data in README or documentation
- [ ] Dependencies are minimal and audited
- [ ] Logging doesn't capture credentials or sensitive data
- [ ] All exposed credentials have been rotated
- [ ] Secrets resolver uses correct resolution order

---

## Dependency Management

Regularly audit Python dependencies for vulnerabilities:

```bash
pip install pip-audit
pip-audit
```

Review `requirements.txt` and `requirements-dev.txt` for suspicious or outdated packages.

---

## Questions?

For security-related questions or clarifications, please reach out to the maintainers privately.

---

## Dependency Vulnerabilities and Python Version

Several transitive dependencies (click, filelock, msgpack, starlette, urllib3, requests, pytest) have CVEs whose fix versions require **Python 3.10+**. If you are running Python 3.9, these cannot be patched without upgrading the interpreter.

**Recommended**: Use Python 3.10 or newer for new installations to pick up all available security patches.

Run `pip-audit` after install to confirm the current vulnerability state for your Python version.
