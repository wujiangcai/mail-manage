# Release Checklist

Use this checklist before pushing a release or deploying to production.

## Git

- [ ] `git status --short --branch` shows no uncommitted changes.
- [ ] The intended branch is checked out.
- [ ] Local commits are pushed to the deployment remote.
- [ ] The server can pull the expected commit.

## Local verification

- [ ] Python sources compile:

  ```bash
  python -m py_compile app.py cpa_support.py
  ```

- [ ] The app starts with production-like env vars and a temporary database.
- [ ] `/health` returns `ok: true`.
- [ ] `/` returns the user page.
- [ ] `/admin` returns the admin page.
- [ ] Admin login succeeds with the configured password.
- [ ] Admin login fails/rate-limits wrong passwords.

## Production configuration

- [ ] `.env` exists on the server and is not committed.
- [ ] `MAIL_CODE_ADMIN_PASSWORD` is strong and unique.
- [ ] `MAIL_CODE_SESSION_SECRET` is long, random, and unique.
- [ ] `MAIL_CODE_API_KEYS` is long, random, and unique.
- [ ] `MAIL_CODE_ALLOWED_ORIGINS` is set to the production HTTPS origin.
- [ ] No value still begins with `change-this`.
- [ ] `MAIL_CODE_DB_PATH` points to persistent storage.
- [ ] Webhooks, if enabled, use HTTPS and `MAIL_CODE_WEBHOOK_SECRET`.

## Docker deployment

- [ ] `docker compose config` succeeds.
- [ ] `docker compose up -d --build` succeeds.
- [ ] `docker compose logs` has no startup error.
- [ ] The backend is bound to localhost or an internal network.
- [ ] The SQLite data volume persists after container restart.

## Reverse proxy / network

- [ ] HTTPS is enabled.
- [ ] The public domain proxies to `127.0.0.1:17373` or an internal container network.
- [ ] `/admin` is additionally restricted by IP, VPN, Cloudflare Access, or equivalent if possible.
- [ ] The raw container port is not directly public.
- [ ] Firewall rules match the intended exposure.

## Functional checks

- [ ] Admin can import a test Hotmail/Outlook or custom IMAP mailbox.
- [ ] Blank/invalid mailbox import returns a clear error.
- [ ] Admin can refresh one enabled mailbox.
- [ ] Admin can refresh all enabled mailboxes.
- [ ] Stored message records appear in the admin page.
- [ ] Admin can create an Access Code bound to a mailbox.
- [ ] User can log in with the Access Code.
- [ ] User can view recent messages without seeing mailbox secrets.
- [ ] User can fetch the latest verification code.
- [ ] `maxReads` is consumed only by successful verification-code fetches.
- [ ] Disabling or deleting an Access Code prevents further user access.
- [ ] Alias/catch-all filtering returns only messages for the assigned recipient.

## CPA checks, if used

- [ ] CPA base URL is reachable from the container/server.
- [ ] CPA management key is valid and not stored in docs or git.
- [ ] 401 scan returns expected candidates.
- [ ] Importing 401 candidates does not overwrite better queued RT data.
- [ ] RT refresh is serialized and returns `409` if already running.
- [ ] Accounts without usable RT remain marked as needing real OAuth/login verification.

## Backup / rollback

- [ ] A SQLite backup was created before upgrade.
- [ ] Backup restore procedure is known.
- [ ] Previous good commit is known.
- [ ] Rollback command has been prepared:

  ```bash
  git checkout <previous-good-commit>
  docker compose up -d --build
  ```

## Final smoke test after deployment

- [ ] `https://your-domain.example/health` returns healthy JSON.
- [ ] `https://your-domain.example/admin` loads and logs in.
- [ ] `https://your-domain.example/` loads user UI.
- [ ] A real mailbox fetch succeeds.
- [ ] Logs stay clean for several minutes after deployment.
