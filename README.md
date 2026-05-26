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
- Hotmail / Outlook OAuth refresh-token fetching
- Custom domain mailbox fetching over IMAP
- Optional API-key authentication

The admin-managed service stores mailbox credentials in local SQLite so users can
fetch codes with a purchase/access code instead of seeing mailbox secrets.

## Run Locally

```bash
python app.py --host 127.0.0.1 --port 17373
```

Open:

```text
http://127.0.0.1:17373/
```

## Docker Deployment

```bash
cp .env.example .env
# edit .env and set admin password, session secret, API keys, and CORS origins
docker compose up -d --build
```

The compose file binds to `127.0.0.1:17373` by default. Put Nginx/Caddy/Cloudflare
Tunnel in front of it for HTTPS.

Do not expose this service publicly without `MAIL_CODE_API_KEYS`.

## Environment Variables

| Name | Default | Description |
| --- | --- | --- |
| `MAIL_CODE_HOST` | `127.0.0.1` local, `0.0.0.0` Docker | Bind host |
| `MAIL_CODE_PORT` | `17373` | Bind port |
| `MAIL_CODE_ADMIN_PASSWORD` | empty | Admin panel password. Required for `/admin`. |
| `MAIL_CODE_SESSION_SECRET` | local fallback | Signing secret for admin/user sessions. Set a long random value. |
| `MAIL_CODE_DB_PATH` | `./data/mail-code-helper.sqlite3` | SQLite database path. |
| `MAIL_CODE_API_KEYS` | empty | Comma-separated API keys for direct `/api/code` and `/api/messages` calls. |
| `MAIL_CODE_ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins |

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
cannot be recovered later; generate a new one if the customer loses it.

## Admin Bulk Import Formats

Hotmail / Outlook OAuth:

```text
email@hotmail.com----password----clientId----refreshToken
```

Custom domain IMAP:

```text
alias@example.com----imap-username@example.com----app-password----imap.example.com----993
```

For catch-all domain mailboxes, import one row per sellable alias. The service
uses the imported email as `targetEmail`, so users only match messages sent to
their assigned alias.

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

## Nginx Example

```nginx
server {
    listen 443 ssl http2;
    server_name mail-code.example.com;

    location / {
        proxy_pass http://127.0.0.1:17373;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```
