# Deployment Guide

Use this guide when deploying Mail Code Helper to a production host.

## 1. Prepare the server

Requirements:

- Docker Engine and Docker Compose plugin
- A reverse proxy with HTTPS, such as Nginx, Caddy, or Cloudflare Tunnel
- A persistent disk/volume for SQLite data
- Firewall rules that expose only the public HTTPS endpoint

The included `docker-compose.yml` binds the app to `127.0.0.1:17373` by default, so it is intended to sit behind a reverse proxy.

## 2. Create production configuration

Copy the example file and edit every placeholder:

```bash
cp .env.example .env
```

Required production values:

```env
MAIL_CODE_ADMIN_PASSWORD=<strong unique admin password>
MAIL_CODE_SESSION_SECRET=<long random signing secret>
MAIL_CODE_API_KEYS=<long random api key>
MAIL_CODE_ALLOWED_ORIGINS=https://your-domain.example
```

Generate strong random values on Linux:

```bash
openssl rand -base64 32
```

Important:

- Values beginning with `change-this` are rejected at startup.
- `MAIL_CODE_SESSION_SECRET` is required when binding publicly.
- `MAIL_CODE_API_KEYS` protects direct `/api/code` and `/api/messages` integrations only; it does not protect `/admin` or the user portal.
- Do not commit `.env`.

## 3. Validate and start

```bash
docker compose config
docker compose up -d --build
docker compose logs -f
```

The service should log a line similar to:

```text
Mail code helper listening on http://0.0.0.0:17373
```

Check health locally on the server:

```bash
curl -fsS http://127.0.0.1:17373/health
```

Expected result includes:

```json
{"ok":true,"service":"mail-code-helper"}
```

## 4. Configure HTTPS reverse proxy

Example Nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:17373;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
}
```

Recommended: restrict `/admin` by IP allowlist, VPN, Cloudflare Access, or equivalent access control.

## 5. First production checks

Open:

- `https://your-domain.example/health`
- `https://your-domain.example/admin`
- `https://your-domain.example/`

Then verify:

1. Admin login succeeds with `MAIL_CODE_ADMIN_PASSWORD`.
2. Repeated wrong admin passwords are rate-limited.
3. Mailbox import rejects blank/invalid input.
4. A test mailbox can be refreshed manually.
5. A test Access Code can be generated and used from the user page.
6. Fetching a verification code consumes `maxReads` only when a code is found.
7. Recent-message viewing does not expose mailbox credentials.
8. Webhooks, if configured, receive signed payloads when `MAIL_CODE_WEBHOOK_SECRET` is set.

## 6. Data persistence and backup

SQLite data lives in the `mail-code-data` named Docker volume by default.

Back up the database from a running deployment:

```bash
docker compose exec mail-code-helper python - <<'PY'
import sqlite3
src = sqlite3.connect('/app/data/mail-code-helper.sqlite3')
dst = sqlite3.connect('/app/data/mail-code-helper.backup.sqlite3')
src.backup(dst)
dst.close()
src.close()
PY
```

Then copy the backup out:

```bash
docker compose cp mail-code-helper:/app/data/mail-code-helper.backup.sqlite3 ./mail-code-helper.backup.sqlite3
```

Back up before upgrades and before deleting accounts or grants in bulk.

## 7. Upgrade

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs -f
curl -fsS http://127.0.0.1:17373/health
```

If the upgrade fails, roll back to the previous commit and rebuild:

```bash
git log --oneline -5
git checkout <previous-good-commit>
docker compose up -d --build
```

## 8. Troubleshooting

### Container starts then exits

Check logs:

```bash
docker compose logs --tail=200 mail-code-helper
```

Common causes:

- `.env` still contains `change-this...` placeholders
- `MAIL_CODE_SESSION_SECRET` is missing while binding publicly
- Port `17373` is already in use
- The data volume is not writable

### Admin login disabled

Set `MAIL_CODE_ADMIN_PASSWORD` and restart:

```bash
docker compose up -d
```

### Direct API returns 401

Confirm the request sends one of these:

```text
Authorization: Bearer <MAIL_CODE_API_KEYS value>
X-API-Key: <MAIL_CODE_API_KEYS value>
```

### Mail fetching fails

Check the account status in `/admin`, then verify:

- OAuth `clientId` and `refreshToken` are valid
- Custom IMAP host/port/SSL are correct
- App password is still valid
- Folder names match the provider's mailbox names
- Alias/catch-all accounts have the expected `targetEmail`
