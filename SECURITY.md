# Security Policy

## 🚨 CRITICAL: Credential Exposure and Rotation Required

**As of August 13, 2026, the `.env` file containing REAL CREDENTIALS was exposed in the public GitHub repository.**

### Immediate Actions Required:

**ROTATE ALL CREDENTIALS IMMEDIATELY:**

1. **Anthropic API Key** - Revoke the exposed key (sk-ant-api03-WZ1tFuAr7S4QWqt-...), generate a new one
2. **Job Site Credentials** - Change passwords for:
   - JobRight
   - LinkedIn
   - Indeed
   - USAJobs
   - All company portal accounts
3. **Twilio Credentials** - Revoke API credentials, generate new Account SID and Auth Token
4. **Encryption Keys** - Regenerate:
   - Credential encryption key (Fernet)
   - Sync secret
5. **Email Accounts** - Verify no unauthorized access, consider password reset for any connected accounts
6. **Notify relevant services** - Inform Anthropic, job sites, and Twilio of potential credential compromise

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
- Use `AICC_SECRETS_DIR` override or fixtures for test-specific credentials.
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

## Git History Cleanup

**August 13, 2026:** Removed `.env` file from entire git history using `git filter-repo` due to credential exposure. All credentials must be rotated.

```
git filter-repo --invert-paths --path .env
git push origin --force-with-lease main
```

This removes `.env` from all commits in the public repository but does NOT affect your local `.env` file.
