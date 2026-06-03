# Mail Code Helper

Standalone mail verification-code helper extracted from GuJumpgate.

It provides:

- Web UI: `GET /`
- Admin UI: `GET /admin`
- Health check: `GET /health`
- Latest messages: `POST /api/messages`
- Latest verification code: `POST /api/code`
- User access-code login and mailbox-bound code fetching
- Admin bulk mailbox import and access-code management
- Admin unified mail-record view across all imported mailboxes
- Automatic mailbox polling with stored mail records
- Webhook push when a new verification code is detected
- CPA / Codex account 401 inspection, refresh-pool import, RT refresh, and CPA credential update
- Hotmail / Outlook OAuth refresh-token fetching
- Custom domain mailbox fetching over IMAP
- Alias/catch-all mailbox fetching with recipient filtering
- Optional API-key authentication

The admin-managed service stores mailbox credentials in local SQLite so users can
fetch codes with a purchase/access code instead of seeing mailbox secrets.

## Web UI

User page (`/`):

- Login with an `mc_...` Access Code.
- View the latest verification code directly in a large readable panel.
- Wait for a verification code automatically with clear login, fetching, retry,
  timeout, and failure status messages.
- View recent mailbox messages as cards with sender, recipients, subject,
  mailbox, preview, time, and detected code. HTML/CSS-heavy email templates are
  cleaned into readable plain-text previews.
- The user page no longer exposes raw JSON as the primary mail view.

Admin page (`/admin`):

- Import Hotmail/Outlook OAuth or custom IMAP accounts in bulk.
- Edit account label, mailbox folders, filters, and `targetEmail` alias matching.
- Refresh one mailbox or all enabled mailboxes and review stored message records.
- Filter mail records by mailbox; selecting one mailbox shows all stored records
  for that account, while the default view shows the latest records across all
  accounts.
- Review mail records in a compact table with cleaned plain-text previews so
  HTML templates and CSS do not dominate the page.
- Realtime refreshes stored account, grant, and message state while the page is open.
- Create, disable, edit, delete, reset read count, and regenerate Access Codes.
- Empty-state actions are guarded: Access Code generation is disabled until at
  least one mailbox exists, and "refresh all" is disabled until an enabled
  mailbox exists.
- Admins can explicitly log out, which clears the browser session token.
- Inspect CPA Codex/OpenAI auth files, detect 401 accounts, import them into a
  refresh pool, refresh usable OpenAI RTs, and upload refreshed auth JSON back to
  CPA.
- Start a CPA OAuth flow and submit the real localhost callback after completing
  the provider's normal login verification.

CPA login verification is never bypassed. If a CPA auth file has no usable RT, or
the RT is invalid, the account is marked as needing real login/OAuth verification
instead of faking, skipping, or disabling SMS/OTP checks.

## Run Locally

```bash
python app.py --host 127.0.0.1 --port 17373
```

Open:

```text
http://127.0.0.1:17373/
```

## Production Docs

- [Deployment Guide](DEPLOY.md) covers server setup, Docker Compose, HTTPS reverse proxy, backups, upgrades, and troubleshooting.
- [Security Notes](SECURITY.md) covers secret handling, admin protection, mailbox credential storage, webhook signing, and incident response.
- [Release Checklist](RELEASE_CHECKLIST.md) provides a pre-launch checklist for git, local verification, production configuration, Docker, reverse proxy, functional checks, and rollback.

## Docker Deployment

```bash
cp .env.example .env
# edit .env and set admin password, session secret, API keys, and CORS origins
docker compose up -d --build
```

The compose file binds to `127.0.0.1:17373` by default. Put Nginx/Caddy/Cloudflare
Tunnel in front of it for HTTPS.

The example `.env` values that start with `change-this` are rejected at startup;
replace them before deploying. The compose file uses a named volume
`mail-code-data` for SQLite data so the non-root container user can write the
database reliably on Linux hosts.

Do not expose this service directly to the public internet. `MAIL_CODE_API_KEYS`
protect only the direct `/api/code` and `/api/messages` integrations; they do
not protect `/admin` or the Access Code user portal. For public deployments,
set strong admin/session/API secrets, use HTTPS, and restrict `/admin` at the
reverse proxy whenever possible.

## Admin And User Review Checklist

Use this checklist after deployment or when changing mailbox settings:

- Admin login: `/admin` accepts `MAIL_CODE_ADMIN_PASSWORD`; logout returns to
  the login screen, removes the stored admin token, and ignores any in-flight
  admin refresh responses from the previous browser session. Repeated failed
  admin password attempts are rate-limited.
- Empty admin state: before importing mailboxes, Access Code generation and
  bulk refresh controls should be disabled or show a clear "import mailbox
  first" message.
- Mailbox import: Hotmail lines and custom IMAP lines import without exposing
  secrets in the table; blank import submissions and files with no valid account
  lines are rejected with a clear error; imported aliases appear in mailbox and
  grant selectors.
- Mail refresh: refreshing one selected mailbox updates its status and stored
  records; refreshing all only processes enabled mailboxes.
- Access Code lifecycle: generated `mc_...` codes are shown once, can be
  disabled, edited, reset, regenerated, or deleted, and regenerated codes
  invalidate the old code.
- User login: `/` accepts only enabled, unexpired Access Codes bound to enabled
  mailboxes. `maxReads` limits successful verification-code fetches.
- User fetching: while the page auto-waits for a code, fetch buttons stay
  disabled to avoid duplicate mailbox reads; only a successful verification-code
  fetch consumes `maxReads`, while viewing recent messages only updates the last
  used time and remains available until the Access Code is disabled or expired.
- User mailbox scope: user APIs return only messages that match the bound
  mailbox/alias filters, and folder summaries never include raw unfiltered
  messages.
- User results: the latest code appears in the large code panel, recent mail is
  shown as readable cards, and raw HTML/CSS-heavy previews are cleaned.
- Limits and errors: non-numeric values such as `top`, `maxReads`, or expiry
  timestamps return clear 400 responses instead of generic server errors;
  out-of-range values are clamped to the documented bounds.

## Environment Variables

| Name | Default | Description |
| --- | --- | --- |
| `MAIL_CODE_HOST` | `127.0.0.1` local, `0.0.0.0` Docker | Bind host |
| `MAIL_CODE_PORT` | `17373` | Bind port |
| `MAIL_CODE_ADMIN_PASSWORD` | empty | Admin panel password. Required for `/admin`. |
| `MAIL_CODE_SESSION_SECRET` | local fallback | Signing secret for admin/user sessions. Set a long random value. Required when binding publicly; never reuse `MAIL_CODE_API_KEYS`. |
| `MAIL_CODE_DB_PATH` | `./data/mail-code-helper.sqlite3` | SQLite database path. |
| `MAIL_CODE_API_KEYS` | empty | Comma-separated API keys for direct `/api/code` and `/api/messages` calls. Does not protect `/admin`. |
| `MAIL_CODE_ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins |
| `MAIL_CODE_AUTO_POLL_INTERVAL_SECONDS` | `60` | Background polling interval for enabled accounts. Set `0` to disable. |
| `MAIL_CODE_AUTO_POLL_TOP` | `10` | Number of recent messages to read per mailbox during automatic polling. |
| `MAIL_CODE_USER_CODE_POLL_INTERVAL_SECONDS` | `5` | User page retry interval while waiting for a code. |
| `MAIL_CODE_USER_CODE_POLL_TIMEOUT_SECONDS` | `90` | User page max wait time for automatic code polling. |
| `MAIL_CODE_ADMIN_REFRESH_INTERVAL_SECONDS` | `10` | Admin page realtime refresh interval. |
| `MAIL_CODE_ADMIN_MESSAGE_LIMIT` | `500` | Max stored messages loaded for a selected mailbox in the admin page. Capped at 5000. |
| `MAIL_CODE_MAX_JSON_BODY_BYTES` | `5242880` | Max JSON request body size. Capped at 50 MiB. |
| `OPENAI_CODEX_CLIENT_ID` | Codex client id | Client id used for OpenAI/Codex RT refresh. Usually leave unchanged. |
| `OPENAI_OAUTH_REFRESH_SCOPE` | `openid profile email` | Scope sent when refreshing OpenAI/Codex RTs. |
| `MAIL_CODE_CPA_PROBE_USER_AGENT` | Codex-like UA | User-Agent sent for CPA probe/refresh requests. |
| `MAIL_CODE_WEBHOOK_URLS` | empty | Comma-separated webhook URLs called when a newly seen message contains a code. |
| `MAIL_CODE_WEBHOOK_SECRET` | empty | Optional HMAC secret for webhook signatures. |
| `MAIL_CODE_WEBHOOK_TIMEOUT_SECONDS` | `8` | Per-webhook request timeout. |

For backward compatibility, `MAIL_CODE_HELPER_API_KEY` and
`HOTMAIL_HELPER_API_KEY` are also accepted.

## Commercial Operation Flow

1. Open `/admin` and log in with `MAIL_CODE_ADMIN_PASSWORD`.
2. Bulk import mailbox accounts.
3. Generate an access code for a customer/order and bind it to one mailbox.
4. Send the generated `mc_...` access code to the customer.
5. The customer opens `/`, logs in with the access code, and fetches codes from
   the assigned mailbox only.

The access code is displayed once when created. It is stored hashed in SQLite and
cannot be recovered later. Admins can disable it, edit its mailbox binding,
change expiry/max reads, clear read count, or regenerate a new code. Regenerating
immediately invalidates the old code.

`maxReads` counts successful verification-code fetches. Opening the recent-mail
view does not consume a read, and concurrent code fetches are guarded by a
database-level conditional update so a one-read Access Code cannot be consumed
successfully by multiple requests at the same time.

The admin panel can refresh one mailbox or all enabled mailboxes. Fetched message
summaries are stored in SQLite so the admin can review received mail records
across accounts without logging into each mailbox provider. Stored previews are
cleaned into plain text to avoid showing raw HTML templates, embedded CSS, or
font declarations in either the user page or admin records table.

The service also runs an automatic polling worker by default. It periodically
reads enabled mailboxes, stores newly seen message summaries, and triggers
webhooks for newly detected verification codes. Set
`MAIL_CODE_AUTO_POLL_INTERVAL_SECONDS=0` when you only want manual refresh.

## CPA Account Inspection And Refresh

The admin page includes a **CPA 巡检与刷新** panel for CPA/Codex credential
maintenance. The CPA service must expose the management API used by
`gpt-account-manager`, including:

- `GET /v0/management/auth-files`
- `GET /v0/management/auth-files/download?name=...`
- `POST /v0/management/api-call`
- `POST /v0/management/auth-files?name=...`
- `GET /v0/management/codex-auth-url`
- `POST /v0/management/oauth-callback`

Typical flow:

1. Open `/admin` and log in.
2. In **CPA 巡检与刷新**, enter the CPA base URL, for example
   `http://localhost:8317`, and the CPA management key. In Docker, use a URL
   reachable from the mail-code-helper container, such as
   `http://host.docker.internal:8317` for a host service or the CPA compose
   service name for same-network containers.
3. Click **巡检 401** to list Codex/OpenAI auth files that return 401.
4. Click **导入刷新池** to store detected 401 items in the local
   `cpa_refresh_queue` table.
5. Click **刷新 RT 并更新 CPA**. Items with a valid OpenAI/Codex
   `refresh_token` are refreshed through `https://auth.openai.com/oauth/token`;
   the refreshed auth JSON is uploaded back to CPA.
6. Items without a usable RT remain marked as needing real login verification.

Operational notes:

- CPA RT refresh is serialized in the backend. If another refresh is already
  running, `/api/admin/cpa/refresh-rt` returns `409` instead of consuming the
  same RTs concurrently.
- Re-importing a 401 item without an RT does not overwrite a queued auth file
  that already has a usable RT.
- If a CPA auth file cannot be downloaded, the item is reported as a download
  failure and is not treated as a login/SMS verification case.
- The selected-mailbox message view is bounded by `MAIL_CODE_ADMIN_MESSAGE_LIMIT`
  to keep large mail histories responsive. API JSON payloads are bounded by
  `MAIL_CODE_MAX_JSON_BODY_BYTES`.
- CPA parameter errors such as missing `managementKey` or invalid `baseUrl`
  return `400`; CPA service connection/HTTP failures return `502`.

OAuth flow:

1. Click **CPA OAuth** to request a CPA Codex authorization URL.
2. Open the URL and complete the provider's normal login, including SMS/OTP if
   required.
3. Paste the real `http://localhost/.../callback?code=...&state=...` callback
   URL into **OAuth 回调地址**.
4. Click **提交回调** so CPA can exchange and store the credential.

The implementation intentionally does not skip SMS verification. It only
refreshes existing valid RTs or hands the operator to the real OAuth/login flow.

### CPA Admin API

All CPA admin endpoints require the normal admin bearer token from
`POST /api/admin/login`.

- `GET /api/admin/cpa/queue?baseUrl=http://localhost:8317`
  returns local refresh-pool rows.
- `POST /api/admin/cpa/scan-401`
  scans CPA auth files and returns 401 candidates.
- `POST /api/admin/cpa/queue-401`
  imports scan results into the local refresh pool.
- `POST /api/admin/cpa/refresh-rt`
  refreshes queued RTs and uploads successful results back to CPA by default.
- `POST /api/admin/cpa/oauth/start`
  requests a CPA OAuth authorization URL.
- `POST /api/admin/cpa/oauth/callback`
  submits the real localhost OAuth callback URL to CPA.

Example scan request:

```json
{
  "baseUrl": "http://localhost:8317",
  "managementKey": "cpa-management-key",
  "maxItems": 20
}
```

Example refresh request:

```json
{
  "baseUrl": "http://localhost:8317",
  "managementKey": "cpa-management-key",
  "limit": 20,
  "updateCpa": true
}
```

## Admin Bulk Import Formats

Hotmail / Outlook OAuth:

```text
email@hotmail.com----password----clientId----refreshToken
```

Custom domain IMAP:

```text
alias@example.com----imap-username@example.com----app-password----imap.example.com----993
```

Blank lines and comment lines starting with `#` are ignored. A submission with
no valid account lines is rejected instead of reporting a successful zero-account
import.

For catch-all domain mailboxes, import one row per sellable alias. The service
uses the imported email as `targetEmail`, so users only match messages sent to
their assigned alias.

Aliases are supported when the provider exposes the alias in message recipients
(`To`, `Cc`, or `Bcc`). For custom IMAP catch-all mailboxes, use the same IMAP
username/password on multiple imported rows and put each sellable alias in the
first `email` field. For Microsoft mailboxes, set the account's `targetEmail` in
the admin edit dialog when the login mailbox receives mail for another alias.

## Hotmail / Outlook Payload

```json
{
  "provider": "hotmail",
  "email": "user@hotmail.com",
  "clientId": "00000000-0000-0000-0000-000000000000",
  "refreshToken": "microsoft-refresh-token",
  "mailboxes": ["INBOX", "Junk"],
  "senderFilters": ["verify@x.com"],
  "top": 10
}
```

Call:

```bash
curl -X POST https://mail-code.example.com/api/code \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d @payload.json
```

## Custom Domain IMAP Payload

Normal mailbox:

```json
{
  "provider": "custom-imap",
  "imapHost": "imap.example.com",
  "imapPort": 993,
  "imapSsl": true,
  "username": "inbox@example.com",
  "password": "app-password",
  "mailboxes": ["INBOX"],
  "senderFilters": ["noreply"],
  "top": 10
}
```

Catch-all mailbox, filtering by recipient alias:

```json
{
  "provider": "custom-imap",
  "imapHost": "imap.example.com",
  "imapPort": 993,
  "imapSsl": true,
  "username": "catchall@example.com",
  "password": "app-password",
  "targetEmail": "alias001@example.com",
  "mailboxes": ["INBOX"],
  "top": 10
}
```

## Response Shape

`POST /api/code` returns:

```json
{
  "ok": true,
  "provider": "custom-imap",
  "transport": "custom-imap",
  "code": "123456",
  "message": {
    "subject": "Your verification code",
    "bodyPreview": "...",
    "receivedDateTime": "2026-05-26T00:00:00Z"
  }
}
```

## Admin Mail Records

Admin-only endpoints:

- `GET /api/admin/messages?accountId=acct_xxx&limit=100` returns stored message records.
- `POST /api/admin/messages/fetch` refreshes messages for one account or all enabled accounts.
- `POST /api/admin/grants/patch` edits Access Code status, name, binding, expiry, max reads, or read count.
- `POST /api/admin/grants/regenerate` creates a new Access Code for an existing grant.

`POST /api/admin/messages/fetch` body:

```json
{
  "accountId": "acct_xxx",
  "top": 10
}
```

Omit `accountId` to refresh all enabled accounts.

## Automatic Polling And Webhooks

Automatic polling uses the same account configuration and alias filtering as
manual admin refresh. It does not consume user Access Code read counts.

When a newly stored message contains a detected verification code, each URL in
`MAIL_CODE_WEBHOOK_URLS` receives a `POST` request:

```json
{
  "event": "mail_code.detected",
  "source": "auto-poll",
  "account": {
    "id": "acct_xxx",
    "provider": "custom-imap",
    "email": "catchall@example.com",
    "targetEmail": "alias001@example.com"
  },
  "message": {
    "id": "message-id",
    "mailbox": "INBOX",
    "sender": "noreply@example.com",
    "recipients": ["alias001@example.com"],
    "subject": "Your verification code",
    "bodyPreview": "Your code is 123456",
    "receivedDateTime": "2026-05-26T00:00:00Z",
    "code": "123456"
  }
}
```

If `MAIL_CODE_WEBHOOK_SECRET` is set, requests include
`X-Mail-Code-Signature: sha256=<hmac>` over the compact JSON body.

## Nginx Example

```nginx
server {
    listen 443 ssl http2;
    server_name mail-code.example.com;

    location / {
        proxy_pass http://127.0.0.1:17373;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

For internet-facing deployments, put `/admin` behind a VPN, IP allowlist, or
additional reverse-proxy authentication/rate limiting. The backend rate-limits
failed admin password attempts by client IP, using `X-Forwarded-For` when the
proxy supplies it, so configure the proxy to overwrite that header instead of
passing untrusted client-provided values through.
