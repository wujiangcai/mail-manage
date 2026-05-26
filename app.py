import argparse
import base64
import email
import hashlib
import hmac
import html
import imaplib
import json
import os
import re
import secrets
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17373
REQUEST_TIMEOUT_SECONDS = 45
FETCH_LIMIT_DEFAULT = 10
TOKEN_TTL_SECONDS = 12 * 60 * 60
ADMIN_TOKEN_TTL_SECONDS = 24 * 60 * 60
DEFAULT_DB_PATH = Path(__file__).with_name("data").joinpath("mail-code-helper.sqlite3")

LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
ENTRA_COMMON_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
ENTRA_CONSUMERS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_API_ORIGIN = "https://graph.microsoft.com"
OUTLOOK_API_ORIGIN = "https://outlook.office.com"
GRAPH_SCOPES = "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
MICROSOFT_IMAP_HOST = "outlook.office365.com"
MICROSOFT_IMAP_PORT = 993

TOKEN_ENDPOINTS = {
    "live": {
        "name": "live",
        "url": LIVE_TOKEN_URL,
        "extra_data": {},
    },
    "entra-consumers-delegated": {
        "name": "entra-consumers-delegated",
        "url": ENTRA_CONSUMERS_TOKEN_URL,
        "extra_data": {"scope": GRAPH_SCOPES},
    },
    "entra-common-delegated": {
        "name": "entra-common-delegated",
        "url": ENTRA_COMMON_TOKEN_URL,
        "extra_data": {"scope": GRAPH_SCOPES},
    },
    "entra-common-default": {
        "name": "entra-common-default",
        "url": ENTRA_COMMON_TOKEN_URL,
        "extra_data": {"scope": GRAPH_DEFAULT_SCOPE},
    },
    "entra-common-outlook": {
        "name": "entra-common-outlook",
        "url": ENTRA_COMMON_TOKEN_URL,
        "extra_data": {},
    },
}


def normalize_port(raw_value, default=DEFAULT_PORT):
    candidate = default if raw_value is None or str(raw_value).strip() == "" else raw_value
    try:
        port = int(str(candidate).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid port: {raw_value}") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"Port out of range: {port}")
    return port


def resolve_config(argv=None):
    parser = argparse.ArgumentParser(description="Mail verification-code helper service.")
    parser.add_argument("--host", default=os.environ.get("MAIL_CODE_HOST") or DEFAULT_HOST)
    parser.add_argument("--port", default=os.environ.get("MAIL_CODE_PORT") or DEFAULT_PORT)
    args = parser.parse_args(argv)
    return {
        "host": str(args.host or DEFAULT_HOST).strip() or DEFAULT_HOST,
        "port": normalize_port(args.port),
    }


def split_env_list(value):
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def get_api_keys():
    return (
        split_env_list(os.environ.get("MAIL_CODE_API_KEYS"))
        or split_env_list(os.environ.get("MAIL_CODE_HELPER_API_KEY"))
        or split_env_list(os.environ.get("HOTMAIL_HELPER_API_KEY"))
    )


def get_allowed_origins():
    return split_env_list(os.environ.get("MAIL_CODE_ALLOWED_ORIGINS")) or ["*"]


def resolve_cors_origin(handler):
    allowed = get_allowed_origins()
    if "*" in allowed:
        return "*"
    request_origin = str(handler.headers.get("Origin") or "").strip()
    if request_origin and request_origin in allowed:
        return request_origin
    return allowed[0] if allowed else ""


def send_cors_headers(handler):
    origin = resolve_cors_origin(handler)
    if origin:
        handler.send_header("Access-Control-Allow-Origin", origin)
    handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def get_request_api_key(handler):
    auth_header = str(handler.headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    header_value = str(handler.headers.get("X-API-Key") or "").strip()
    if header_value:
        return header_value
    query = parse_qs(urlparse(handler.path).query)
    return str((query.get("api_key") or [""])[0] or "").strip()


def require_api_key(handler):
    keys = get_api_keys()
    if not keys:
        return
    provided = get_request_api_key(handler)
    if not provided or not any(hmac.compare_digest(provided, key) for key in keys):
        raise PermissionError("Unauthorized: missing or invalid API key")


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    send_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler, status, content_type, body):
    encoded = str(body or "").encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    send_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(encoded)


def read_json_payload(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON payload: {exc}") from exc


def post_form(url, data):
    request = Request(
        url,
        data=urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url, headers=None):
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.getcode(), json.loads(response.read().decode("utf-8"))


def mask_secret(value, keep=6):
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) <= keep:
        return "*" * len(raw)
    return f"{raw[:keep]}...{raw[-keep:]}"


def compact_text(value, limit=600):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:limit]


def log_info(message):
    print(f"[MailCodeHelper] {message}", flush=True)


def now_ms():
    return int(time.time() * 1000)


def get_db_path():
    return Path(os.environ.get("MAIL_CODE_DB_PATH") or DEFAULT_DB_PATH).expanduser()


def db_connect():
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mail_accounts (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL,
                password TEXT NOT NULL DEFAULT '',
                client_id TEXT NOT NULL DEFAULT '',
                refresh_token TEXT NOT NULL DEFAULT '',
                imap_host TEXT NOT NULL DEFAULT '',
                imap_port INTEGER NOT NULL DEFAULT 993,
                imap_ssl INTEGER NOT NULL DEFAULT 1,
                imap_username TEXT NOT NULL DEFAULT '',
                imap_password TEXT NOT NULL DEFAULT '',
                target_email TEXT NOT NULL DEFAULT '',
                mailboxes TEXT NOT NULL DEFAULT '["INBOX"]',
                sender_filters TEXT NOT NULL DEFAULT '[]',
                subject_filters TEXT NOT NULL DEFAULT '[]',
                required_keywords TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'ready',
                last_error TEXT NOT NULL DEFAULT '',
                last_checked_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS access_grants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                access_code_hash TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                expires_at INTEGER NOT NULL DEFAULT 0,
                max_reads INTEGER NOT NULL DEFAULT 0,
                read_count INTEGER NOT NULL DEFAULT 0,
                last_used_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE
            );
            """
        )


def json_loads(value, fallback):
    try:
        parsed = json.loads(str(value or ""))
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except Exception:
        return fallback


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def generate_id(prefix):
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def hash_secret(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def get_signing_secret():
    configured = os.environ.get("MAIL_CODE_SESSION_SECRET") or os.environ.get("MAIL_CODE_API_KEYS") or os.environ.get("MAIL_CODE_ADMIN_PASSWORD")
    return str(configured or "local-dev-mail-code-helper-secret")


def b64url_encode(data):
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(value):
    padded = str(value or "") + "=" * (-len(str(value or "")) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def sign_token(payload):
    body = b64url_encode(payload)
    signature = hmac.new(get_signing_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


def verify_token(token, expected_kind):
    raw = str(token or "").strip()
    if "." not in raw:
        raise PermissionError("Unauthorized: missing session token")
    body, signature = raw.rsplit(".", 1)
    expected_signature = hmac.new(get_signing_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    actual_signature = base64.urlsafe_b64decode((signature + "=" * (-len(signature) % 4)).encode("ascii"))
    if not hmac.compare_digest(actual_signature, expected_signature):
        raise PermissionError("Unauthorized: invalid session token")
    payload = b64url_decode(body)
    if payload.get("kind") != expected_kind:
        raise PermissionError("Unauthorized: invalid session scope")
    if int(payload.get("exp") or 0) < int(time.time()):
        raise PermissionError("Unauthorized: session expired")
    return payload


def get_bearer_token(handler):
    auth_header = str(handler.headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


def require_admin_session(handler):
    return verify_token(get_bearer_token(handler), "admin")


def require_user_session(handler):
    return verify_token(get_bearer_token(handler), "user")


def get_admin_password():
    return str(os.environ.get("MAIL_CODE_ADMIN_PASSWORD") or "").strip()


def require_configured_admin_password():
    password = get_admin_password()
    if not password:
        raise RuntimeError("MAIL_CODE_ADMIN_PASSWORD is not configured")
    return password


def safe_account(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "provider": row["provider"],
        "label": row["label"],
        "email": row["email"],
        "targetEmail": row["target_email"],
        "imapHost": row["imap_host"],
        "imapPort": row["imap_port"],
        "imapSsl": bool(row["imap_ssl"]),
        "mailboxes": json_loads(row["mailboxes"], []),
        "senderFilters": json_loads(row["sender_filters"], []),
        "subjectFilters": json_loads(row["subject_filters"], []),
        "requiredKeywords": json_loads(row["required_keywords"], []),
        "enabled": bool(row["enabled"]),
        "status": row["status"],
        "lastError": row["last_error"],
        "lastCheckedAt": row["last_checked_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def safe_grant(row, account=None):
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "accountId": row["account_id"],
        "enabled": bool(row["enabled"]),
        "expiresAt": row["expires_at"],
        "maxReads": row["max_reads"],
        "readCount": row["read_count"],
        "lastUsedAt": row["last_used_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "account": safe_account(account) if account else None,
    }


def decode_mime_header(value):
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="ignore"))
        else:
            parts.append(str(chunk))
    return "".join(parts).strip()


def normalize_address_list(raw_values):
    source = raw_values if isinstance(raw_values, list) else [raw_values]
    values = []
    seen = set()
    for _, addr in getaddresses([str(item or "") for item in source if item]):
        normalized = str(addr or "").strip()
        key = normalized.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        values.append(normalized)
    return values


def normalize_message_recipients(message):
    to = normalize_address_list(message.get_all("To", []))
    cc = normalize_address_list(message.get_all("Cc", []))
    bcc = normalize_address_list(message.get_all("Bcc", []))
    return {
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "all": list(dict.fromkeys([*to, *cc, *bcc])),
    }


def extract_text_part(message):
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if "attachment" in str(part.get("Content-Disposition") or "").lower():
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="ignore").strip()
            if part.get_content_type() == "text/plain" and text:
                return text
            if part.get_content_type() == "text/html" and text:
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text))).strip()
        return ""

    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="ignore").strip()
    if message.get_content_type() == "text/html":
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text))).strip()
    return text


def mailbox_candidates(mailbox):
    raw = str(mailbox or "INBOX").strip() or "INBOX"
    normalized = raw.lower()
    if normalized in {"junk", "junk email", "junk e-mail", "junkemail"}:
        return ["Junk", "Junk Email", "Junk E-Mail"]
    if normalized == "inbox":
        return ["INBOX"]
    return [raw]


def normalize_mailbox_label(mailbox):
    raw = str(mailbox or "INBOX").strip() or "INBOX"
    normalized = raw.lower()
    if normalized in {"junk", "junk email", "junk e-mail", "junkemail"}:
        return "Junk"
    if normalized == "inbox":
        return "INBOX"
    return raw


def normalize_mailbox_id(mailbox):
    normalized = str(mailbox or "INBOX").strip().lower()
    if normalized in {"junk", "junk email", "junk e-mail", "junkemail"}:
        return "junkemail"
    return "inbox"


def select_mailbox(client, mailbox):
    for candidate in mailbox_candidates(mailbox):
        status, _ = client.select(candidate)
        if status == "OK":
            return candidate
    raise RuntimeError(f"Mailbox not found: {mailbox}")


def to_timestamp_ms(raw_date):
    if not raw_date:
        return 0
    try:
        parsed = parsedate_to_datetime(raw_date)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return 0


def to_iso_string(timestamp_ms):
    if not timestamp_ms:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_imap_message(message_id, raw_bytes, mailbox):
    parsed = email.message_from_bytes(raw_bytes)
    sender_name, sender_addr = parseaddr(parsed.get("From", ""))
    body = extract_text_part(parsed)
    timestamp_ms = to_timestamp_ms(parsed.get("Date"))
    return {
        "id": str(message_id),
        "mailbox": mailbox,
        "subject": decode_mime_header(parsed.get("Subject", "")),
        "from": {
            "emailAddress": {
                "address": sender_addr.strip(),
                "name": sender_name.strip(),
            }
        },
        "bodyPreview": body[:500],
        "body": {"content": body},
        "recipients": normalize_message_recipients(parsed),
        "receivedDateTime": to_iso_string(timestamp_ms),
        "receivedTimestamp": timestamp_ms,
    }


def fetch_messages_with_client(client, mailbox="INBOX", top=FETCH_LIMIT_DEFAULT):
    logical_mailbox = normalize_mailbox_label(mailbox)
    select_mailbox(client, mailbox)
    status, data = client.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        return {"mailbox": logical_mailbox, "messages": [], "count": 0}

    message_ids = data[0].split()
    limit = max(1, min(int(top or FETCH_LIMIT_DEFAULT), 30))
    selected_ids = list(reversed(message_ids[-limit:]))
    messages = []
    for message_id in selected_ids:
        fetch_status, fetch_data = client.fetch(message_id, "(RFC822)")
        if fetch_status != "OK" or not fetch_data:
            continue
        raw_bytes = b""
        for item in fetch_data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw_bytes = item[1]
                break
        if raw_bytes:
            messages.append(normalize_imap_message(message_id.decode("utf-8", errors="ignore"), raw_bytes, logical_mailbox))
    return {"mailbox": logical_mailbox, "messages": messages, "count": len(messages)}


def fetch_messages_for_mailboxes(client, mailboxes, top):
    mailbox_results = []
    messages = []
    for mailbox in mailboxes or ["INBOX"]:
        result = fetch_messages_with_client(client, mailbox=mailbox, top=top)
        mailbox_results.append(result)
        messages.extend(result["messages"])
    messages.sort(key=lambda item: int(item.get("receivedTimestamp") or 0), reverse=True)
    return {"mailboxResults": mailbox_results, "messages": messages}


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def is_custom_imap_payload(payload):
    provider = str(payload.get("provider") or payload.get("mailProvider") or "").strip().lower()
    if provider in {"imap", "custom-imap", "domain-imap", "domain"}:
        return True
    return bool(payload.get("imapHost") or payload.get("imap_host") or isinstance(payload.get("imap"), dict))


def normalize_custom_imap_config(payload):
    imap_config = payload.get("imap") if isinstance(payload.get("imap"), dict) else {}
    host = str(payload.get("imapHost") or payload.get("imap_host") or imap_config.get("host") or "").strip()
    port = normalize_port(payload.get("imapPort") or payload.get("imap_port") or imap_config.get("port") or 993)
    username = str(
        payload.get("username")
        or payload.get("user")
        or imap_config.get("username")
        or imap_config.get("user")
        or payload.get("email")
        or ""
    ).strip()
    password = str(
        payload.get("password")
        or payload.get("appPassword")
        or payload.get("app_password")
        or imap_config.get("password")
        or imap_config.get("appPassword")
        or ""
    )
    use_ssl = parse_bool(payload.get("imapSsl") if "imapSsl" in payload else imap_config.get("ssl"), default=True)
    starttls = parse_bool(payload.get("starttls", imap_config.get("starttls")), default=False)
    target_email = str(payload.get("targetEmail") or payload.get("target_email") or payload.get("email") or "").strip()

    if not host:
        raise RuntimeError("Missing imapHost for custom domain mailbox")
    if not username:
        raise RuntimeError("Missing username for custom domain mailbox")
    if not password:
        raise RuntimeError("Missing password/appPassword for custom domain mailbox")
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "ssl": use_ssl,
        "starttls": starttls,
        "target_email": target_email,
    }


def open_custom_imap(config):
    if config["ssl"]:
        client = imaplib.IMAP4_SSL(config["host"], config["port"], timeout=REQUEST_TIMEOUT_SECONDS)
    else:
        client = imaplib.IMAP4(config["host"], config["port"], timeout=REQUEST_TIMEOUT_SECONDS)
        if config["starttls"]:
            client.starttls()
    client.login(config["username"], config["password"])
    return client


def collect_custom_imap_messages(payload, mailboxes, top):
    config = normalize_custom_imap_config(payload)
    client = None
    try:
        client = open_custom_imap(config)
        result = fetch_messages_for_mailboxes(client, mailboxes, top)
        return {
            "provider": "custom-imap",
            "transport": "custom-imap",
            "targetEmail": config["target_email"],
            "messages": result["messages"],
            "mailboxResults": result["mailboxResults"],
            "nextRefreshToken": "",
            "tokenEndpoint": "",
        }
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def build_xoauth2(email_addr, access_token):
    return f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def open_microsoft_imap(email_addr, access_token):
    client = imaplib.IMAP4_SSL(MICROSOFT_IMAP_HOST, MICROSOFT_IMAP_PORT, timeout=REQUEST_TIMEOUT_SECONDS)
    client.authenticate("XOAUTH2", lambda _: build_xoauth2(email_addr, access_token))
    return client


def try_refresh_access_token(endpoint, client_id, refresh_token):
    request_data = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        **(endpoint.get("extra_data") or {}),
    }
    started_at = time.monotonic()
    try:
        payload = post_form(endpoint["url"], request_data)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return {
            "ok": False,
            "endpoint": endpoint["name"],
            "status": getattr(exc, "code", None),
            "error": compact_text(detail or str(exc)),
            "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        }
    except URLError as exc:
        return {
            "ok": False,
            "endpoint": endpoint["name"],
            "status": None,
            "error": compact_text(f"Token request failed: {exc}"),
            "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        }

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        return {
            "ok": False,
            "endpoint": endpoint["name"],
            "status": 200,
            "error": compact_text(payload.get("error_description") or payload.get("error") or json.dumps(payload, ensure_ascii=False)),
            "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        }

    return {
        "ok": True,
        "endpoint": endpoint["name"],
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "access_token": access_token,
        "next_refresh_token": str(payload.get("refresh_token") or "").strip(),
    }


def refresh_access_token(client_id, refresh_token, strategy_names):
    errors = []
    selected = [TOKEN_ENDPOINTS[name] for name in strategy_names if name in TOKEN_ENDPOINTS]
    log_info(
        "token refresh start "
        f"clientId={mask_secret(client_id)} refreshToken={mask_secret(refresh_token)} "
        f"strategies={[item['name'] for item in selected]}"
    )
    for endpoint in selected:
        result = try_refresh_access_token(endpoint, client_id, refresh_token)
        if result["ok"]:
            log_info(f"token refresh success endpoint={result['endpoint']} elapsedMs={result['elapsed_ms']}")
            return result
        errors.append(result)
        log_info(f"token refresh failed endpoint={result['endpoint']} status={result['status']} detail={result['error']}")
    detail = " | ".join(f"{item['endpoint']}({item['status']}): {item['error']}" for item in errors)
    raise RuntimeError(f"Token refresh failed on all endpoints: {detail}")


def normalize_api_recipient_address(raw_value):
    if not isinstance(raw_value, dict):
        return ""
    email_addr = raw_value.get("emailAddress") or raw_value.get("EmailAddress") or {}
    if not isinstance(email_addr, dict):
        return ""
    return str(email_addr.get("address") or email_addr.get("Address") or "").strip()


def normalize_api_recipient_list(raw_values):
    values = []
    seen = set()
    for item in raw_values if isinstance(raw_values, list) else []:
        address = normalize_api_recipient_address(item)
        key = address.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        values.append(address)
    return values


def normalize_api_recipients(to_values=None, cc_values=None, bcc_values=None):
    to = normalize_api_recipient_list(to_values)
    cc = normalize_api_recipient_list(cc_values)
    bcc = normalize_api_recipient_list(bcc_values)
    return {
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "all": list(dict.fromkeys([*to, *cc, *bcc])),
    }


def iso_to_timestamp_ms(value):
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def normalize_graph_message(message, mailbox):
    sender = message.get("from", {}) or {}
    email_addr = sender.get("emailAddress", {}) if isinstance(sender, dict) else {}
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    body_content = str(body.get("content") or "").strip()
    received = str(message.get("receivedDateTime") or "").strip()
    return {
        "id": str(message.get("id") or message.get("internetMessageId") or "").strip(),
        "mailbox": normalize_mailbox_label(mailbox),
        "subject": str(message.get("subject") or "").strip(),
        "from": {
            "emailAddress": {
                "address": str(email_addr.get("address") or "").strip(),
                "name": str(email_addr.get("name") or "").strip(),
            }
        },
        "bodyPreview": str(message.get("bodyPreview") or "").strip(),
        "body": {"content": body_content},
        "recipients": normalize_api_recipients(
            message.get("toRecipients"),
            message.get("ccRecipients"),
            message.get("bccRecipients"),
        ),
        "receivedDateTime": received,
        "receivedTimestamp": iso_to_timestamp_ms(received),
    }


def normalize_outlook_message(message, mailbox):
    sender = message.get("From", {}) or message.get("from", {}) or {}
    email_addr = sender.get("EmailAddress", {}) if isinstance(sender, dict) else {}
    if isinstance(sender, dict) and not email_addr:
        email_addr = sender.get("emailAddress", {}) or {}
    body = message.get("Body") or message.get("body") or {}
    body_content = str(body.get("Content") or body.get("content") or "").strip() if isinstance(body, dict) else ""
    received = str(message.get("ReceivedDateTime") or message.get("receivedDateTime") or "").strip()
    return {
        "id": str(message.get("Id") or message.get("id") or "").strip(),
        "mailbox": normalize_mailbox_label(mailbox),
        "subject": str(message.get("Subject") or message.get("subject") or "").strip(),
        "from": {
            "emailAddress": {
                "address": str(email_addr.get("Address") or email_addr.get("address") or "").strip(),
                "name": str(email_addr.get("Name") or email_addr.get("name") or "").strip(),
            }
        },
        "bodyPreview": str(message.get("BodyPreview") or message.get("bodyPreview") or "").strip(),
        "body": {"content": body_content},
        "recipients": normalize_api_recipients(
            message.get("ToRecipients") or message.get("toRecipients"),
            message.get("CcRecipients") or message.get("ccRecipients"),
            message.get("BccRecipients") or message.get("bccRecipients"),
        ),
        "receivedDateTime": received,
        "receivedTimestamp": iso_to_timestamp_ms(received),
    }


def fetch_graph_messages(access_token, mailbox="INBOX", top=FETCH_LIMIT_DEFAULT):
    query = urlencode({
        "$top": max(1, min(int(top or FETCH_LIMIT_DEFAULT), 30)),
        "$select": "id,internetMessageId,subject,from,bodyPreview,body,receivedDateTime,toRecipients,ccRecipients,bccRecipients",
        "$orderby": "receivedDateTime desc",
    })
    url = f"{GRAPH_API_ORIGIN}/v1.0/me/mailFolders/{normalize_mailbox_id(mailbox)}/messages?{query}"
    try:
        _, payload = get_json(url, headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"})
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Graph request failed: {detail or exc}") from exc
    messages = [normalize_graph_message(item, mailbox) for item in (payload.get("value") or [])]
    return {"mailbox": normalize_mailbox_label(mailbox), "messages": messages, "count": len(messages)}


def fetch_outlook_api_messages(access_token, mailbox="INBOX", top=FETCH_LIMIT_DEFAULT):
    query = urlencode({
        "$top": max(1, min(int(top or FETCH_LIMIT_DEFAULT), 30)),
        "$select": "Id,Subject,From,BodyPreview,Body,ReceivedDateTime,ToRecipients,CcRecipients,BccRecipients",
        "$orderby": "ReceivedDateTime desc",
    })
    url = f"{OUTLOOK_API_ORIGIN}/api/v2.0/me/mailfolders/{normalize_mailbox_id(mailbox)}/messages?{query}"
    try:
        _, payload = get_json(url, headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"})
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Outlook API request failed: {detail or exc}") from exc
    messages = [normalize_outlook_message(item, mailbox) for item in (payload.get("value") or [])]
    return {"mailbox": normalize_mailbox_label(mailbox), "messages": messages, "count": len(messages)}


def collect_microsoft_imap_messages(email_addr, client_id, refresh_token, mailboxes, top):
    token = refresh_access_token(client_id, refresh_token, ["live", "entra-consumers-delegated", "entra-common-delegated"])
    client = None
    try:
        client = open_microsoft_imap(email_addr, token["access_token"])
        result = fetch_messages_for_mailboxes(client, mailboxes, top)
        return {
            "provider": "hotmail",
            "transport": "imap",
            "messages": result["messages"],
            "mailboxResults": result["mailboxResults"],
            "nextRefreshToken": token.get("next_refresh_token") or "",
            "tokenEndpoint": token.get("endpoint") or "",
        }
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def collect_graph_messages(email_addr, client_id, refresh_token, mailboxes, top):
    del email_addr
    token = refresh_access_token(client_id, refresh_token, ["entra-common-delegated", "entra-consumers-delegated", "entra-common-default"])
    mailbox_results = [fetch_graph_messages(token["access_token"], mailbox=mailbox, top=top) for mailbox in mailboxes]
    messages = []
    for item in mailbox_results:
        messages.extend(item["messages"])
    messages.sort(key=lambda item: int(item.get("receivedTimestamp") or 0), reverse=True)
    return {
        "provider": "hotmail",
        "transport": "graph",
        "messages": messages,
        "mailboxResults": mailbox_results,
        "nextRefreshToken": token.get("next_refresh_token") or "",
        "tokenEndpoint": token.get("endpoint") or "",
    }


def collect_outlook_messages(email_addr, client_id, refresh_token, mailboxes, top):
    del email_addr
    token = refresh_access_token(client_id, refresh_token, ["entra-common-outlook", "entra-common-delegated"])
    mailbox_results = [fetch_outlook_api_messages(token["access_token"], mailbox=mailbox, top=top) for mailbox in mailboxes]
    messages = []
    for item in mailbox_results:
        messages.extend(item["messages"])
    messages.sort(key=lambda item: int(item.get("receivedTimestamp") or 0), reverse=True)
    return {
        "provider": "hotmail",
        "transport": "outlook",
        "messages": messages,
        "mailboxResults": mailbox_results,
        "nextRefreshToken": token.get("next_refresh_token") or "",
        "tokenEndpoint": token.get("endpoint") or "",
    }


def collect_microsoft_messages(payload, mailboxes, top):
    email_addr = str(payload.get("email") or "").strip()
    client_id = str(payload.get("clientId") or payload.get("client_id") or "").strip()
    refresh_token = str(payload.get("refreshToken") or payload.get("refresh_token") or "").strip()
    if not email_addr or not client_id or not refresh_token:
        raise RuntimeError("Missing email/clientId/refreshToken")

    collectors = [
        ("imap", collect_microsoft_imap_messages),
        ("graph", collect_graph_messages),
        ("outlook", collect_outlook_messages),
    ]
    errors = []
    for transport, collector in collectors:
        try:
            log_info(f"message collection start provider=hotmail transport={transport} email={email_addr}")
            return collector(email_addr, client_id, refresh_token, mailboxes, top)
        except Exception as exc:
            message = compact_text(exc)
            errors.append(f"{transport}: {message}")
            log_info(f"message collection failed provider=hotmail transport={transport} detail={message}")
    raise RuntimeError(f"Message collection failed on all transports: {' | '.join(errors)}")


def extract_code(text, code_patterns=None):
    source = str(text or "")
    for pattern in code_patterns or []:
        try:
            source_pattern = str((pattern or {}).get("source") or "").strip()
            if not source_pattern:
                continue
            flags = str((pattern or {}).get("flags") or "").lower()
            re_flags = 0
            if "i" in flags:
                re_flags |= re.IGNORECASE
            if "m" in flags:
                re_flags |= re.MULTILINE
            if "s" in flags:
                re_flags |= re.DOTALL
            match = re.search(source_pattern, source, flags=re_flags)
            if not match:
                continue
            if match.lastindex:
                for group_index in range(1, match.lastindex + 1):
                    candidate = str(match.group(group_index) or "").strip()
                    if candidate:
                        return candidate
            candidate = str(match.group(0) or "").strip()
            if candidate:
                return candidate
        except re.error:
            continue

    patterns = [
        r"(?:验证码|校验码|动态码|代码)[^0-9]{0,24}(\d{4,8})",
        r"(?:verification|security|login|one-time|one time|auth)[^0-9]{0,40}(\d{4,8})",
        r"(?:code(?:\s+is|[\s:：]))[^0-9]{0,20}(\d{4,8})",
        r"\b(\d{6})\b",
        r"\b(\d{4,8})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def get_message_search_text(message):
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    return "\n".join([
        str(message.get("subject") or ""),
        str(message.get("bodyPreview") or ""),
        str(body.get("content") or ""),
        str(message.get("from", {}).get("emailAddress", {}).get("address", "")),
    ])


def select_latest_code(messages, filters):
    sender_keywords = [str(item).strip().lower() for item in filters.get("senderFilters") or [] if str(item).strip()]
    subject_keywords = [str(item).strip().lower() for item in filters.get("subjectFilters") or [] if str(item).strip()]
    required_keywords = [str(item).strip().lower() for item in filters.get("requiredKeywords") or [] if str(item).strip()]
    recipient_keywords = [str(item).strip().lower() for item in filters.get("recipientFilters") or [] if str(item).strip()]
    target_email = str(filters.get("targetEmail") or filters.get("target_email") or "").strip().lower()
    if target_email:
        recipient_keywords.append(target_email)
    excluded = {str(item).strip() for item in filters.get("excludeCodes") or [] if str(item).strip()}
    filter_after = int(filters.get("filterAfterTimestamp") or 0)
    code_patterns = filters.get("codePatterns") or []

    def match_message(message, apply_time_filter):
        timestamp = int(message.get("receivedTimestamp") or 0)
        if apply_time_filter and filter_after and timestamp and timestamp < filter_after:
            return None

        sender = str(message.get("from", {}).get("emailAddress", {}).get("address", "")).lower()
        subject = str(message.get("subject", ""))
        preview = str(message.get("bodyPreview", ""))
        recipients = message.get("recipients", {}) if isinstance(message.get("recipients"), dict) else {}
        recipient_text = " ".join(str(item or "") for item in recipients.get("all", [])).lower()
        search_text = get_message_search_text(message)
        combined = " ".join([sender, subject.lower(), preview.lower(), search_text.lower(), recipient_text])
        code = extract_code(search_text, code_patterns=code_patterns)
        if not code or code in excluded:
            return None

        if recipient_keywords and not any(keyword in recipient_text for keyword in recipient_keywords):
            return None
        sender_ok = bool(sender_keywords) and any(keyword in combined for keyword in sender_keywords)
        subject_ok = bool(subject_keywords) and any(keyword in combined for keyword in subject_keywords)
        keyword_ok = bool(required_keywords) and any(keyword in combined for keyword in required_keywords)
        if (sender_keywords or subject_keywords or required_keywords) and not sender_ok and not subject_ok and not keyword_ok:
            return None
        return {"code": code, "message": message}

    for use_time_fallback in [False, True]:
        matches = []
        for message in messages or []:
            result = match_message(message, apply_time_filter=not use_time_fallback)
            if result:
                matches.append(result)
        if matches:
            matches.sort(key=lambda item: int(item["message"].get("receivedTimestamp") or 0), reverse=True)
            selected = matches[0]
            return {
                "code": selected["code"],
                "message": selected["message"],
                "usedTimeFallback": use_time_fallback,
            }
    return {"code": "", "message": None, "usedTimeFallback": False}


def collect_messages(payload):
    top = max(1, min(int(payload.get("top") or FETCH_LIMIT_DEFAULT), 30))
    mailboxes = payload.get("mailboxes") if isinstance(payload.get("mailboxes"), list) else [payload.get("mailbox") or "INBOX"]
    if is_custom_imap_payload(payload):
        return collect_custom_imap_messages(payload, mailboxes, top)
    return collect_microsoft_messages(payload, mailboxes, top)


def account_row_to_payload(row, extra_filters=None):
    extra_filters = extra_filters or {}
    mailboxes = json_loads(row["mailboxes"], ["INBOX"])
    payload = {
        "provider": row["provider"],
        "email": row["email"],
        "mailboxes": mailboxes,
        "top": extra_filters.get("top") or FETCH_LIMIT_DEFAULT,
        "senderFilters": extra_filters.get("senderFilters") or json_loads(row["sender_filters"], []),
        "subjectFilters": extra_filters.get("subjectFilters") or json_loads(row["subject_filters"], []),
        "requiredKeywords": extra_filters.get("requiredKeywords") or json_loads(row["required_keywords"], []),
        "recipientFilters": extra_filters.get("recipientFilters") or [],
        "excludeCodes": extra_filters.get("excludeCodes") or [],
        "filterAfterTimestamp": extra_filters.get("filterAfterTimestamp") or 0,
        "codePatterns": extra_filters.get("codePatterns") or [],
    }
    if row["provider"] == "custom-imap":
        payload.update({
            "provider": "custom-imap",
            "imapHost": row["imap_host"],
            "imapPort": row["imap_port"],
            "imapSsl": bool(row["imap_ssl"]),
            "username": row["imap_username"] or row["email"],
            "password": row["imap_password"],
            "targetEmail": row["target_email"] or row["email"],
        })
    else:
        payload.update({
            "provider": "hotmail",
            "clientId": row["client_id"],
            "refreshToken": row["refresh_token"],
        })
    return payload


def load_account(conn, account_id):
    row = conn.execute("SELECT * FROM mail_accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        raise RuntimeError("Account not found")
    return row


def load_grant_with_account(conn, grant_id):
    grant = conn.execute("SELECT * FROM access_grants WHERE id = ?", (grant_id,)).fetchone()
    if not grant:
        raise PermissionError("Unauthorized: grant not found")
    account = load_account(conn, grant["account_id"])
    return grant, account


def validate_grant(grant, account):
    if not grant["enabled"]:
        raise PermissionError("This access code is disabled")
    if grant["expires_at"] and grant["expires_at"] < now_ms():
        raise PermissionError("This access code is expired")
    if grant["max_reads"] and grant["read_count"] >= grant["max_reads"]:
        raise PermissionError("This access code has no reads left")
    if not account["enabled"]:
        raise PermissionError("This mailbox is disabled")


def update_account_after_fetch(conn, account_id, result=None, error=None):
    timestamp = now_ms()
    if error:
        conn.execute(
            "UPDATE mail_accounts SET status = ?, last_error = ?, last_checked_at = ?, updated_at = ? WHERE id = ?",
            ("error", compact_text(error), timestamp, timestamp, account_id),
        )
        return
    next_refresh = str((result or {}).get("nextRefreshToken") or "").strip()
    if next_refresh:
        conn.execute(
            "UPDATE mail_accounts SET refresh_token = ?, status = ?, last_error = '', last_checked_at = ?, updated_at = ? WHERE id = ?",
            (next_refresh, "ready", timestamp, timestamp, account_id),
        )
    else:
        conn.execute(
            "UPDATE mail_accounts SET status = ?, last_error = '', last_checked_at = ?, updated_at = ? WHERE id = ?",
            ("ready", timestamp, timestamp, account_id),
        )


def increment_grant_usage(conn, grant_id):
    timestamp = now_ms()
    conn.execute(
        "UPDATE access_grants SET read_count = read_count + 1, last_used_at = ?, updated_at = ? WHERE id = ?",
        (timestamp, timestamp, grant_id),
    )


def list_accounts(conn):
    rows = conn.execute("SELECT * FROM mail_accounts ORDER BY created_at DESC").fetchall()
    return [safe_account(row) for row in rows]


def list_grants(conn):
    rows = conn.execute(
        """
        SELECT
          g.*,
          a.id AS a_id,
          a.provider AS a_provider,
          a.label AS a_label,
          a.email AS a_email,
          a.target_email AS a_target_email,
          a.imap_host AS a_imap_host,
          a.imap_port AS a_imap_port,
          a.imap_ssl AS a_imap_ssl,
          a.mailboxes AS a_mailboxes,
          a.sender_filters AS a_sender_filters,
          a.subject_filters AS a_subject_filters,
          a.required_keywords AS a_required_keywords,
          a.enabled AS a_enabled,
          a.status AS a_status,
          a.last_error AS a_last_error,
          a.last_checked_at AS a_last_checked_at,
          a.created_at AS a_created_at,
          a.updated_at AS a_updated_at
        FROM access_grants g
        JOIN mail_accounts a ON a.id = g.account_id
        ORDER BY g.created_at DESC
        """
    ).fetchall()
    results = []
    for row in rows:
        account = {
            "id": row["a_id"],
            "provider": row["a_provider"],
            "label": row["a_label"],
            "email": row["a_email"],
            "targetEmail": row["a_target_email"],
            "imapHost": row["a_imap_host"],
            "imapPort": row["a_imap_port"],
            "imapSsl": bool(row["a_imap_ssl"]),
            "mailboxes": json_loads(row["a_mailboxes"], []),
            "senderFilters": json_loads(row["a_sender_filters"], []),
            "subjectFilters": json_loads(row["a_subject_filters"], []),
            "requiredKeywords": json_loads(row["a_required_keywords"], []),
            "enabled": bool(row["a_enabled"]),
            "status": row["a_status"],
            "lastError": row["a_last_error"],
            "lastCheckedAt": row["a_last_checked_at"],
            "createdAt": row["a_created_at"],
            "updatedAt": row["a_updated_at"],
        }
        results.append(safe_grant(row, account=None) | {"account": account})
    return results


def parse_import_lines(text, provider, options=None):
    options = options or {}
    normalized_provider = "custom-imap" if provider in {"imap", "custom-imap", "domain-imap"} else "hotmail"
    imported = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("----")]
        timestamp = now_ms()
        account_id = generate_id("acct")
        if normalized_provider == "custom-imap":
            if len(parts) < 5:
                raise RuntimeError("IMAP import line format: email----username----password----imapHost----imapPort")
            email_addr, username, password, imap_host, imap_port = parts[:5]
            imported.append({
                "id": account_id,
                "provider": "custom-imap",
                "label": options.get("label") or email_addr,
                "email": email_addr,
                "password": "",
                "client_id": "",
                "refresh_token": "",
                "imap_host": imap_host,
                "imap_port": normalize_port(imap_port or 993),
                "imap_ssl": 1,
                "imap_username": username,
                "imap_password": password,
                "target_email": email_addr,
                "mailboxes": json_dumps(options.get("mailboxes") or ["INBOX"]),
                "sender_filters": json_dumps(options.get("senderFilters") or []),
                "subject_filters": json_dumps(options.get("subjectFilters") or []),
                "required_keywords": json_dumps(options.get("requiredKeywords") or []),
                "created_at": timestamp,
                "updated_at": timestamp,
            })
        else:
            if len(parts) < 4:
                raise RuntimeError("Hotmail import line format: email----password----clientId----refreshToken")
            email_addr, password, client_id = parts[:3]
            refresh_token = "----".join(parts[3:]).strip()
            imported.append({
                "id": account_id,
                "provider": "hotmail",
                "label": options.get("label") or email_addr,
                "email": email_addr,
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "imap_host": "",
                "imap_port": 993,
                "imap_ssl": 1,
                "imap_username": "",
                "imap_password": "",
                "target_email": "",
                "mailboxes": json_dumps(options.get("mailboxes") or ["INBOX", "Junk"]),
                "sender_filters": json_dumps(options.get("senderFilters") or []),
                "subject_filters": json_dumps(options.get("subjectFilters") or []),
                "required_keywords": json_dumps(options.get("requiredKeywords") or []),
                "created_at": timestamp,
                "updated_at": timestamp,
            })
    return imported


def insert_accounts(conn, accounts):
    for item in accounts:
        conn.execute(
            """
            INSERT INTO mail_accounts (
              id, provider, label, email, password, client_id, refresh_token,
              imap_host, imap_port, imap_ssl, imap_username, imap_password,
              target_email, mailboxes, sender_filters, subject_filters, required_keywords,
              created_at, updated_at
            ) VALUES (
              :id, :provider, :label, :email, :password, :client_id, :refresh_token,
              :imap_host, :imap_port, :imap_ssl, :imap_username, :imap_password,
              :target_email, :mailboxes, :sender_filters, :subject_filters, :required_keywords,
              :created_at, :updated_at
            )
            """,
            item,
        )


def create_access_grant(conn, payload):
    account_id = str(payload.get("accountId") or payload.get("account_id") or "").strip()
    if not account_id:
        raise RuntimeError("Missing accountId")
    load_account(conn, account_id)
    access_code = f"mc_{secrets.token_urlsafe(18)}"
    timestamp = now_ms()
    expires_in_days = int(payload.get("expiresInDays") or payload.get("expires_in_days") or 0)
    expires_at = timestamp + expires_in_days * 86400 * 1000 if expires_in_days > 0 else 0
    grant = {
        "id": generate_id("grant"),
        "name": str(payload.get("name") or "").strip(),
        "access_code_hash": hash_secret(access_code),
        "account_id": account_id,
        "enabled": 1,
        "expires_at": expires_at,
        "max_reads": max(0, int(payload.get("maxReads") or payload.get("max_reads") or 0)),
        "read_count": 0,
        "last_used_at": 0,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    conn.execute(
        """
        INSERT INTO access_grants (
          id, name, access_code_hash, account_id, enabled, expires_at,
          max_reads, read_count, last_used_at, created_at, updated_at
        ) VALUES (
          :id, :name, :access_code_hash, :account_id, :enabled, :expires_at,
          :max_reads, :read_count, :last_used_at, :created_at, :updated_at
        )
        """,
        grant,
    )
    return {"grant": safe_grant(grant), "accessCode": access_code}


def static_file(name):
    return Path(__file__).with_name("static").joinpath(name)


class MailCodeHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        send_cors_headers(self)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"", "/"}:
            target = static_file("index.html")
            if target.exists():
                text_response(self, 200, "text/html; charset=utf-8", target.read_text(encoding="utf-8"))
                return
            text_response(self, 200, "text/plain; charset=utf-8", "mail-code-helper")
            return
        if path == "/admin":
            target = static_file("admin.html")
            if target.exists():
                text_response(self, 200, "text/html; charset=utf-8", target.read_text(encoding="utf-8"))
                return
            json_response(self, 404, {"ok": False, "error": "admin.html not found"})
            return
        if path == "/health":
            json_response(self, 200, {
                "ok": True,
                "service": "mail-code-helper",
                "version": VERSION,
                "authRequired": bool(get_api_keys()),
                "providers": ["hotmail", "custom-imap"],
                "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
            return
        if path == "/api/admin/overview":
            try:
                require_admin_session(self)
                with db_connect() as conn:
                    json_response(self, 200, {
                        "ok": True,
                        "accounts": list_accounts(conn),
                        "grants": list_grants(conn),
                    })
            except PermissionError as exc:
                json_response(self, 401, {"ok": False, "error": str(exc)})
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": str(exc)})
            return
        if path == "/api/user/me":
            try:
                session = require_user_session(self)
                with db_connect() as conn:
                    grant, account = load_grant_with_account(conn, session.get("grantId"))
                    validate_grant(grant, account)
                    json_response(self, 200, {
                        "ok": True,
                        "grant": safe_grant(grant),
                        "account": safe_account(account),
                    })
            except PermissionError as exc:
                json_response(self, 401, {"ok": False, "error": str(exc)})
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": str(exc)})
            return
        json_response(self, 404, {"ok": False, "error": f"Unsupported path: {self.path}"})

    def do_POST(self):
        try:
            payload = read_json_payload(self)
            path = urlparse(self.path).path
            if path == "/api/admin/login":
                password = str(payload.get("password") or "").strip()
                expected = require_configured_admin_password()
                if not hmac.compare_digest(password, expected):
                    raise PermissionError("Invalid admin password")
                exp = int(time.time()) + ADMIN_TOKEN_TTL_SECONDS
                token = sign_token({"kind": "admin", "exp": exp})
                json_response(self, 200, {"ok": True, "token": token, "expiresAt": exp * 1000})
                return

            if path == "/api/user/login":
                access_code = str(payload.get("accessCode") or payload.get("access_code") or "").strip()
                if not access_code:
                    raise PermissionError("Missing access code")
                with db_connect() as conn:
                    grant = conn.execute(
                        "SELECT * FROM access_grants WHERE access_code_hash = ?",
                        (hash_secret(access_code),),
                    ).fetchone()
                    if not grant:
                        raise PermissionError("Invalid access code")
                    account = load_account(conn, grant["account_id"])
                    validate_grant(grant, account)
                    exp = int(time.time()) + TOKEN_TTL_SECONDS
                    token = sign_token({"kind": "user", "grantId": grant["id"], "accountId": account["id"], "exp": exp})
                    json_response(self, 200, {
                        "ok": True,
                        "token": token,
                        "expiresAt": exp * 1000,
                        "grant": safe_grant(grant),
                        "account": safe_account(account),
                    })
                return

            if path.startswith("/api/admin/"):
                require_admin_session(self)
                self.handle_admin_post(path, payload)
                return

            if path in {"/api/user/messages", "/api/user/code"}:
                session = require_user_session(self)
                self.handle_user_fetch(path, payload, session)
                return

            require_api_key(self)
            if path not in {"/messages", "/api/messages", "/code", "/api/code"}:
                json_response(self, 404, {"ok": False, "error": f"Unsupported path: {self.path}"})
                return

            result = collect_messages(payload)
            if path in {"/messages", "/api/messages"}:
                json_response(self, 200, {"ok": True, **result})
                return

            selected = select_latest_code(result["messages"], payload)
            json_response(self, 200, {
                "ok": True,
                "provider": result.get("provider") or "",
                "transport": result.get("transport") or "",
                "code": selected["code"],
                "message": selected["message"],
                "usedTimeFallback": selected["usedTimeFallback"],
                "nextRefreshToken": result.get("nextRefreshToken") or "",
                "tokenEndpoint": result.get("tokenEndpoint") or "",
            })
        except PermissionError as exc:
            json_response(self, 401, {"ok": False, "error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            json_response(self, 500, {"ok": False, "error": str(exc)})

    def handle_admin_post(self, path, payload):
        with db_connect() as conn:
            if path == "/api/admin/accounts/import":
                provider = str(payload.get("provider") or "hotmail").strip().lower()
                options = {
                    "label": str(payload.get("label") or "").strip(),
                    "mailboxes": payload.get("mailboxes") if isinstance(payload.get("mailboxes"), list) else None,
                    "senderFilters": payload.get("senderFilters") if isinstance(payload.get("senderFilters"), list) else None,
                    "subjectFilters": payload.get("subjectFilters") if isinstance(payload.get("subjectFilters"), list) else None,
                    "requiredKeywords": payload.get("requiredKeywords") if isinstance(payload.get("requiredKeywords"), list) else None,
                }
                accounts = parse_import_lines(payload.get("text") or "", provider, options)
                insert_accounts(conn, accounts)
                conn.commit()
                json_response(self, 200, {"ok": True, "imported": len(accounts), "accounts": list_accounts(conn)})
                return

            if path == "/api/admin/accounts/patch":
                account_id = str(payload.get("accountId") or payload.get("id") or "").strip()
                if not account_id:
                    raise RuntimeError("Missing accountId")
                updates = payload.get("updates") if isinstance(payload.get("updates"), dict) else {}
                allowed = {
                    "enabled": ("enabled", lambda value: 1 if value else 0),
                    "label": ("label", str),
                    "status": ("status", str),
                    "senderFilters": ("sender_filters", json_dumps),
                    "subjectFilters": ("subject_filters", json_dumps),
                    "requiredKeywords": ("required_keywords", json_dumps),
                    "mailboxes": ("mailboxes", json_dumps),
                }
                assignments = []
                values = []
                for key, value in updates.items():
                    if key not in allowed:
                        continue
                    column, normalizer = allowed[key]
                    assignments.append(f"{column} = ?")
                    values.append(normalizer(value))
                if assignments:
                    assignments.append("updated_at = ?")
                    values.append(now_ms())
                    values.append(account_id)
                    conn.execute(f"UPDATE mail_accounts SET {', '.join(assignments)} WHERE id = ?", values)
                    conn.commit()
                json_response(self, 200, {"ok": True, "accounts": list_accounts(conn)})
                return

            if path == "/api/admin/accounts/delete":
                account_id = str(payload.get("accountId") or payload.get("id") or "").strip()
                if not account_id:
                    raise RuntimeError("Missing accountId")
                conn.execute("DELETE FROM mail_accounts WHERE id = ?", (account_id,))
                conn.commit()
                json_response(self, 200, {"ok": True, "accounts": list_accounts(conn), "grants": list_grants(conn)})
                return

            if path == "/api/admin/grants/create":
                result = create_access_grant(conn, payload)
                conn.commit()
                json_response(self, 200, {"ok": True, **result, "grants": list_grants(conn)})
                return

            if path == "/api/admin/grants/patch":
                grant_id = str(payload.get("grantId") or payload.get("id") or "").strip()
                if not grant_id:
                    raise RuntimeError("Missing grantId")
                updates = payload.get("updates") if isinstance(payload.get("updates"), dict) else {}
                allowed = {
                    "enabled": ("enabled", lambda value: 1 if value else 0),
                    "name": ("name", str),
                    "maxReads": ("max_reads", lambda value: max(0, int(value or 0))),
                    "expiresAt": ("expires_at", lambda value: max(0, int(value or 0))),
                }
                assignments = []
                values = []
                for key, value in updates.items():
                    if key not in allowed:
                        continue
                    column, normalizer = allowed[key]
                    assignments.append(f"{column} = ?")
                    values.append(normalizer(value))
                if assignments:
                    assignments.append("updated_at = ?")
                    values.append(now_ms())
                    values.append(grant_id)
                    conn.execute(f"UPDATE access_grants SET {', '.join(assignments)} WHERE id = ?", values)
                    conn.commit()
                json_response(self, 200, {"ok": True, "grants": list_grants(conn)})
                return

            if path == "/api/admin/grants/delete":
                grant_id = str(payload.get("grantId") or payload.get("id") or "").strip()
                if not grant_id:
                    raise RuntimeError("Missing grantId")
                conn.execute("DELETE FROM access_grants WHERE id = ?", (grant_id,))
                conn.commit()
                json_response(self, 200, {"ok": True, "grants": list_grants(conn)})
                return

            json_response(self, 404, {"ok": False, "error": f"Unsupported path: {path}"})

    def handle_user_fetch(self, path, payload, session):
        with db_connect() as conn:
            grant, account = load_grant_with_account(conn, session.get("grantId"))
            validate_grant(grant, account)
            request_payload = account_row_to_payload(account, payload)
            try:
                result = collect_messages(request_payload)
                update_account_after_fetch(conn, account["id"], result=result)
                increment_grant_usage(conn, grant["id"])
                conn.commit()
            except Exception as exc:
                update_account_after_fetch(conn, account["id"], error=exc)
                conn.commit()
                raise

            if path == "/api/user/messages":
                json_response(self, 200, {
                    "ok": True,
                    "provider": result.get("provider") or "",
                    "transport": result.get("transport") or "",
                    "messages": result.get("messages") or [],
                    "mailboxResults": result.get("mailboxResults") or [],
                })
                return

            selected = select_latest_code(result["messages"], request_payload)
            json_response(self, 200, {
                "ok": True,
                "provider": result.get("provider") or "",
                "transport": result.get("transport") or "",
                "code": selected["code"],
                "message": selected["message"],
                "usedTimeFallback": selected["usedTimeFallback"],
            })


def main(argv=None):
    config = resolve_config(argv)
    init_db()
    if config["host"] in {"0.0.0.0", "::"} and not get_api_keys():
        log_info("WARNING: public bind without MAIL_CODE_API_KEYS is not recommended.")
    if not get_admin_password():
        log_info("WARNING: MAIL_CODE_ADMIN_PASSWORD is not configured; admin login is disabled.")
    server = ThreadingHTTPServer((config["host"], config["port"]), MailCodeHandler)
    print(f"Mail code helper listening on http://{config['host']}:{config['port']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
