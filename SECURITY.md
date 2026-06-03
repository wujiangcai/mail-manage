# Security Notes

Mail Code Helper stores mailbox credentials and access-grant state locally. Treat the deployment as sensitive infrastructure.

## Secrets

Required production secrets:

- `MAIL_CODE_ADMIN_PASSWORD`
- `MAIL_CODE_SESSION_SECRET`
- `MAIL_CODE_API_KEYS`
- Optional `MAIL_CODE_WEBHOOK_SECRET`

Guidelines:

- Use long, unique random values.
- Do not reuse the admin password as an API key or session secret.
- Do not commit `.env` or SQLite data.
- Rotate secrets after accidental exposure.
- Restart the service after changing `.env`.

The app rejects `.env.example` placeholders beginning with `change-this` at startup.

## Admin access

`/admin` is protected by the admin password and signed browser session tokens. For production, add an outer access-control layer as well:

- IP allowlist in Nginx/Caddy
- VPN-only access
- Cloudflare Access or similar identity-aware proxy
- Firewall rules that avoid exposing the backend port directly

`MAIL_CODE_API_KEYS` does not protect `/admin`.

## User access codes

User `mc_...` access codes are stored hashed in SQLite and are displayed only when created or regenerated.

Operational recommendations:

- Set expirations for customer/order-specific codes.
- Set `maxReads` for limited-use codes.
- Disable or delete grants after delivery is complete.
- Regenerate a code if it may have been exposed.

## Mailbox credentials

The database stores mailbox material required to fetch mail, including OAuth refresh tokens or custom IMAP passwords. Protect the SQLite volume accordingly.

Recommendations:

- Restrict server shell access.
- Back up SQLite securely and encrypt backups where possible.
- Avoid copying production databases to less trusted machines.
- Delete disabled/unused mailboxes when no longer needed.

## Network exposure

Recommended production shape:

```text
Internet -> HTTPS reverse proxy -> 127.0.0.1:17373 -> mail-code-helper container
```

Do not expose the container port directly to the public internet unless you have equivalent network controls in place.

Set `MAIL_CODE_ALLOWED_ORIGINS` to your HTTPS origin rather than `*` when browser integrations are known.

## Webhooks

If webhooks are enabled:

- Use HTTPS URLs.
- Set `MAIL_CODE_WEBHOOK_SECRET`.
- Verify `X-Mail-Code-Signature: sha256=<hmac>` on the receiver.
- Keep receivers idempotent because delivery may be retried by operators manually.

Webhook payloads include message metadata and detected codes. Send them only to trusted systems.

## Logging

The service logs operational errors and may print Python tracebacks for unexpected server errors. Avoid sending secrets in fields that are not intended to hold secrets.

Before sharing logs, review them for:

- Email addresses
- API keys
- OAuth refresh tokens
- IMAP usernames/passwords
- Access codes
- Webhook URLs

## CPA / Codex refresh operations

CPA features are admin-only and are designed for account inspection and legitimate refresh flows. The app does not bypass provider login, SMS, or OTP verification. If a usable refresh token is unavailable or invalid, the account remains marked as needing real login/OAuth verification.

Protect CPA management keys as secrets and use CPA base URLs reachable only from trusted networks.

## Incident response

If you suspect exposure:

1. Stop public access at the reverse proxy or firewall.
2. Rotate `MAIL_CODE_ADMIN_PASSWORD`, `MAIL_CODE_SESSION_SECRET`, `MAIL_CODE_API_KEYS`, and webhook secrets.
3. Regenerate affected user Access Codes.
4. Rotate mailbox app passwords or OAuth credentials where needed.
5. Review logs and database records for unexpected access.
6. Restart the service and verify `/health`, `/admin`, and user flows.
