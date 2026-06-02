import base64
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

REQUEST_TIMEOUT_SECONDS = 45
OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_CLIENT_ID = os.environ.get("OPENAI_CODEX_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann").strip()
OPENAI_OAUTH_REFRESH_SCOPE = os.environ.get("OPENAI_OAUTH_REFRESH_SCOPE", "openid profile email").strip()
CPA_PROBE_USER_AGENT = os.environ.get(
    "MAIL_CODE_CPA_PROBE_USER_AGENT",
    "codex_cli_rs/0.76.0 (Windows; x86_64) mail-code-helper",
).strip()


class CpaConfigError(RuntimeError):
    pass


class CpaUpstreamError(RuntimeError):
    pass


def now_ms():
    return int(time.time() * 1000)


def compact_text(value, limit=600):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:limit]


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value, fallback):
    try:
        parsed = json.loads(str(value or ""))
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except Exception:
        return fallback


def generate_id(prefix):
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def first_text(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cpa_refresh_queue (
            id TEXT PRIMARY KEY,
            cpa_base_url TEXT NOT NULL DEFAULT '',
            cpa_name TEXT NOT NULL DEFAULT '',
            auth_index TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            status_label TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            has_refresh_token INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            cpa_item_json TEXT NOT NULL DEFAULT '{}',
            auth_file_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            refreshed_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_cpa_refresh_queue_base_name
          ON cpa_refresh_queue(cpa_base_url, cpa_name);
        CREATE INDEX IF NOT EXISTS idx_cpa_refresh_queue_status
          ON cpa_refresh_queue(status, updated_at);
        """
    )
    conn.execute(
        """
        DELETE FROM cpa_refresh_queue
        WHERE cpa_name != ''
          AND id NOT IN (
            SELECT id
            FROM (
              SELECT
                id,
                ROW_NUMBER() OVER (
                  PARTITION BY cpa_base_url, cpa_name
                  ORDER BY has_refresh_token DESC, updated_at DESC, created_at DESC
                ) AS rn
              FROM cpa_refresh_queue
              WHERE cpa_name != ''
            )
            WHERE rn = 1
          )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cpa_refresh_queue_base_name_unique
          ON cpa_refresh_queue(cpa_base_url, cpa_name)
          WHERE cpa_name != ''
        """
    )


def normalize_base_url(value):
    clean = str(value or "").strip()
    if clean and not re.match(r"^https?://", clean, flags=re.I):
        default_scheme = "http" if re.match(r"^(localhost|127\.0\.0\.1|\[::1\]|::1)(:\d+)?(/|$)", clean, flags=re.I) else "https"
        clean = f"{default_scheme}://{clean}"
    return clean.rstrip("/")


def normalize_cpa_base_url(value):
    clean = normalize_base_url(value)
    if not clean:
        return ""
    parsed = urlparse(clean)
    if not parsed.scheme or not parsed.netloc:
        return clean
    path = parsed.path or ""
    if path in {"", "/"} or "management.html" in path or path.startswith("/management"):
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return clean


def validate_cpa_base_url(base_url):
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise CpaConfigError("CPA 地址必须使用 http 或 https")
    if not parsed.hostname:
        raise CpaConfigError("CPA 地址缺少主机名")


def config(payload):
    base_url = normalize_cpa_base_url(
        payload.get("base_url") or payload.get("baseUrl") or payload.get("cpaBaseUrl") or "http://localhost:8317"
    )
    management_key = str(payload.get("management_key") or payload.get("managementKey") or "").strip()
    if not management_key:
        raise CpaConfigError("缺少 CPA 管理密钥")
    validate_cpa_base_url(base_url)
    return base_url, management_key


def headers(management_key):
    return {
        "Authorization": f"Bearer {management_key}",
        "X-Management-Key": management_key,
        "Accept": "application/json",
    }


def request_json(url, method="GET", json_data=None, request_headers=None, timeout=30):
    body = None
    final_headers = {"Accept": "application/json", **(request_headers or {})}
    if json_data is not None:
        body = json.dumps(json_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        final_headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=body, headers=final_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return {"status": "ok"}
            try:
                parsed = json.loads(raw)
            except Exception:
                return {"body": raw}
            return parsed if isinstance(parsed, dict) else {"data": parsed}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"error": raw}
        detail = payload.get("detail") or payload.get("error_description") or payload.get("error") or raw or str(exc)
        raise CpaUpstreamError(f"CPA HTTP {exc.code}: {compact_text(detail)}") from exc
    except URLError as exc:
        raise CpaUpstreamError(f"CPA 请求失败: {compact_text(exc)}") from exc


def nested_text(source, *keys):
    current = source
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()

def extract_state_from_auth_url(auth_url):
    try:
        return parse_qs(urlparse(auth_url).query).get("state", [""])[0]
    except Exception:
        return ""


def direct_oauth_start(payload):
    base_url, management_key = config(payload)
    result = request_json(f"{base_url}/v0/management/codex-auth-url", request_headers=headers(management_key), timeout=30)
    authorize_url = first_text(
        nested_text(result, "url"), nested_text(result, "auth_url"), nested_text(result, "authUrl"),
        nested_text(result, "data", "url"), nested_text(result, "data", "auth_url"), nested_text(result, "data", "authUrl"),
    )
    if not authorize_url.startswith(("http://", "https://")):
        raise CpaUpstreamError("CPA 没有返回有效的 OAuth 授权链接")
    state = first_text(
        nested_text(result, "state"), nested_text(result, "auth_state"), nested_text(result, "authState"),
        nested_text(result, "data", "state"), nested_text(result, "data", "auth_state"), nested_text(result, "data", "authState"),
        extract_state_from_auth_url(authorize_url),
    )
    return {
        "ok": True,
        "authorizeUrl": authorize_url,
        "authorize_url": authorize_url,
        "state": state,
        "baseUrl": base_url,
        "message": "已生成 CPA OAuth 授权链接。请完成真实验证流程；不会跳过短信或 OTP。",
    }


def parse_localhost_oauth_callback(callback_url, expected_state=""):
    raw = str(callback_url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise CpaConfigError("只接受真实 localhost / 127.0.0.1 OAuth 回调地址")
    query = parse_qs(parsed.query)
    error = first_text((query.get("error") or [""])[0], (query.get("error_description") or [""])[0])
    if error:
        raise CpaConfigError(f"OAuth 授权失败: {error}")
    code = first_text((query.get("code") or [""])[0])
    state = first_text((query.get("state") or [""])[0])
    if not code or not state:
        raise CpaConfigError("OAuth 回调地址缺少 code 或 state")
    if expected_state and state != expected_state:
        raise CpaConfigError("OAuth 回调 state 与本轮授权不一致")
    return {"url": urlunparse(parsed), "code": code, "state": state}


def direct_oauth_callback(payload):
    base_url, management_key = config(payload)
    callback = parse_localhost_oauth_callback(
        payload.get("callback_url") or payload.get("callbackUrl") or payload.get("redirect_url") or payload.get("redirectUrl"),
        payload.get("state") or payload.get("oauth_state") or payload.get("oauthState") or "",
    )
    result = request_json(
        f"{base_url}/v0/management/oauth-callback",
        method="POST",
        json_data={"provider": "codex", "redirect_url": callback["url"]},
        request_headers=headers(management_key),
        timeout=45,
    )
    return {
        "ok": True,
        "cpaUpdate": True,
        "state": callback["state"],
        "result": result,
        "message": first_text(nested_text(result, "message"), nested_text(result, "data", "message"), "CPA 已接收 OAuth 回调"),
    }


def item_type(item):
    return str(item.get("type") or item.get("typo") or "").strip().lower()


def item_name(item):
    return first_text(item.get("name"), item.get("id"), item.get("filename"), item.get("file"))


def item_auth_index(item):
    return first_text(item.get("auth_index"), item.get("authIndex"), item.get("index"))


def item_chatgpt_account_id(item):
    for key in ("chatgpt_account_id", "chatgptAccountId", "account_id", "accountId"):
        value = first_text(item.get(key))
        if value:
            return value
    id_token = item.get("id_token")
    if isinstance(id_token, dict):
        return first_text(id_token.get("chatgpt_account_id"), id_token.get("account_id"))
    return ""


def infer_email(item, auth_file=None):
    auth_file = auth_file if isinstance(auth_file, dict) else {}
    candidates = [
        item.get("email"), item.get("account"), item.get("name"), item.get("id"),
        auth_file.get("email"), auth_file.get("account"), auth_file.get("name"),
    ]
    for key in ("user", "profile", "account"):
        obj = auth_file.get(key)
        if isinstance(obj, dict):
            candidates.append(obj.get("email"))
    for value in candidates:
        match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", str(value or ""), flags=re.I)
        if match:
            return match.group(0).lower()
    return ""


def list_auth_files(base_url, management_key):
    payload = request_json(f"{base_url}/v0/management/auth-files", request_headers=headers(management_key), timeout=30)
    files = payload.get("files") or payload.get("data") or payload.get("items") or []
    return [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []


def download_auth_file(base_url, management_key, name):
    clean_name = str(name or "").strip()
    if not clean_name:
        return {}
    payload = request_json(
        f"{base_url}/v0/management/auth-files/download?name={quote(clean_name, safe='')}",
        request_headers=headers(management_key),
        timeout=30,
    )
    if isinstance(payload.get("auth_file"), dict):
        return payload["auth_file"]
    if isinstance(payload.get("authFile"), dict):
        return payload["authFile"]
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    body = payload.get("body")
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return payload if isinstance(payload, dict) else {}


def probe_payload(item):
    call_headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": CPA_PROBE_USER_AGENT,
    }
    account_id = item_chatgpt_account_id(item)
    if account_id:
        call_headers["Chatgpt-Account-Id"] = account_id
    return {
        "authIndex": item_auth_index(item),
        "method": "GET",
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "header": call_headers,
    }


def status_message(value, status_code=None, action=""):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (dict, list)) else str(value or "")
    parts = []
    if action:
        parts.append(str(action))
    if status_code is not None:
        parts.append(f"HTTP {status_code}")
    if raw:
        parts.append(raw)
    return compact_text(" - ".join(parts), 420), compact_text(raw, 1200)


def probe_status(base_url, management_key, item):
    name = item_name(item)
    result = {
        **item,
        "name": name,
        "email": first_text(item.get("email"), item.get("account")),
        "auth_index": item_auth_index(item),
        "type": item_type(item),
        "status_code": None,
        "ok": None,
        "action": "scanned",
        "message": "",
    }
    if not result["auth_index"]:
        message, raw = status_message("missing auth_index", action="skipped")
        result.update({"ok": False, "action": "skipped", "message": message, "raw_message": raw})
        return result
    try:
        payload = request_json(
            f"{base_url}/v0/management/api-call",
            method="POST",
            json_data=probe_payload(item),
            request_headers=headers(management_key),
            timeout=30,
        )
        status_code = payload.get("status_code") if payload.get("status_code") is not None else payload.get("statusCode")
        if status_code is None and isinstance(payload.get("body"), str):
            try:
                parsed_body = json.loads(payload["body"])
                status_code = parsed_body.get("status") or parsed_body.get("status_code")
            except Exception:
                pass
        result["status_code"] = status_code
        status_text = str(status_code or "")
        actions = {"200": "ready", "401": "401", "403": "risk_blocked", "429": "usage_limit_reached"}
        action = actions.get(status_text, "http_error" if status_text else "probe_failed")
        message, raw = status_message(payload, status_code=status_code, action=action)
        result.update({"ok": status_text == "200", "action": action, "message": message, "raw_message": raw})
    except Exception as exc:
        message, raw = status_message(str(exc), action="probe_failed")
        result.update({"ok": False, "action": "probe_failed", "message": message, "raw_message": raw})
    return result


def is_401_item(item):
    status_code = item.get("status_code") if item.get("status_code") is not None else item.get("statusCode")
    if str(status_code) == "401":
        return True
    text = " ".join(str(item.get(key) or "") for key in ("status", "status_message", "error", "message", "action")).lower()
    return bool(re.search(r"\b401\b", text) or "unauthorized" in text)


def candidates(payload):
    base_url, management_key = config(payload)
    max_items = max(1, min(int(payload.get("max_items") or payload.get("maxItems") or 20), 100))
    files = list_auth_files(base_url, management_key)
    picked = [item for item in files if item_type(item) in {"", "codex", "chatgpt", "openai"}]
    return base_url, management_key, max_items, picked[:max_items]

def extract_token(auth_file, *names):
    if not isinstance(auth_file, dict):
        return ""
    containers = [auth_file]
    for key in ("tokens", "token", "credentials"):
        if isinstance(auth_file.get(key), dict):
            containers.append(auth_file[key])
    for container in containers:
        for name in names:
            value = first_text(container.get(name))
            if value:
                return value
    return ""


def extract_refresh_token(auth_file):
    return extract_token(
        auth_file,
        "chatgpt_refresh_token", "openai_refresh_token", "codex_refresh_token", "refresh_token", "refreshToken",
    )


def extract_id_token(auth_file):
    return extract_token(auth_file, "id_token", "idToken")


def extract_session_token(auth_file):
    return extract_token(auth_file, "session_token", "sessionToken")


def jwt_payload(token):
    try:
        part = str(token or "").split(".")[1]
        padded = part.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        parsed = json.loads(base64.b64decode(padded).decode("utf-8", errors="replace"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def normal_plan_type(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "team" in raw:
        return "team"
    if "pro" in raw and "plus" not in raw:
        return "pro"
    if "plus" in raw:
        return "plus"
    if "free" in raw:
        return "free"
    return raw[:40]


def access_token_expires_at(token):
    payload = jwt_payload(token)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds")
    return ""


def build_synthetic_id_token(email_addr, account_id, plan_type, expires_at):
    def encode(value):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    timestamp = int(time.time())
    exp = timestamp + 3600
    if expires_at:
        try:
            exp = int(datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
    return ".".join([
        encode({"alg": "none", "typ": "JWT", "cpa_synthetic": True}),
        encode({
            "iss": "mail-code-helper",
            "aud": "chatgpt",
            "email": email_addr,
            "chatgpt_account_id": account_id,
            "account_id": account_id,
            "chatgpt_plan_type": plan_type,
            "iat": timestamp,
            "exp": exp,
        }),
        "synthetic",
    ])


def session_to_auth(session, fallback=None, require_refresh_token=False):
    fallback = fallback if isinstance(fallback, dict) else {}
    tokens = session.get("tokens") if isinstance(session.get("tokens"), dict) else {}
    token = session.get("token") if isinstance(session.get("token"), dict) else {}
    credentials = session.get("credentials") if isinstance(session.get("credentials"), dict) else {}
    access_token = first_text(
        session.get("accessToken"), session.get("access_token"), tokens.get("accessToken"), tokens.get("access_token"),
        token.get("accessToken"), token.get("access_token"), credentials.get("access_token"),
    )
    if not access_token:
        raise CpaUpstreamError("Session JSON 缺少 access_token")
    refresh_token = first_text(session.get("refreshToken"), session.get("refresh_token"), tokens.get("refreshToken"), tokens.get("refresh_token"))
    if require_refresh_token and not refresh_token:
        raise CpaUpstreamError("OpenAI OAuth 响应没有 refresh_token")
    session_token = first_text(session.get("sessionToken"), session.get("session_token"), tokens.get("sessionToken"), tokens.get("session_token"))
    id_token = first_text(session.get("idToken"), session.get("id_token"), tokens.get("idToken"), tokens.get("id_token"))
    payload = jwt_payload(access_token)
    id_payload = jwt_payload(id_token)
    auth_claim = payload.get("https://api.openai.com/auth") if isinstance(payload.get("https://api.openai.com/auth"), dict) else {}
    id_auth_claim = id_payload.get("https://api.openai.com/auth") if isinstance(id_payload.get("https://api.openai.com/auth"), dict) else {}
    profile = payload.get("https://api.openai.com/profile") if isinstance(payload.get("https://api.openai.com/profile"), dict) else {}
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    email_addr = first_text(user.get("email"), session.get("email"), credentials.get("email"), profile.get("email"), id_payload.get("email"), payload.get("email"), fallback.get("email"))
    account_id = first_text(
        account.get("id"), session.get("account_id"), session.get("chatgptAccountId"), session.get("chatgpt_account_id"),
        auth_claim.get("chatgpt_account_id"), id_auth_claim.get("chatgpt_account_id"), fallback.get("auth_index"), fallback.get("account_id"),
    )
    plan_type = normal_plan_type(first_text(account.get("planType"), session.get("planType"), session.get("plan_type"), auth_claim.get("chatgpt_plan_type"), id_auth_claim.get("chatgpt_plan_type"), fallback.get("plan_type")))
    expires_at = access_token_expires_at(access_token) or first_text(session.get("expires"), session.get("expiresAt"), session.get("expires_at"), fallback.get("expired"))
    original_id_token = id_token
    if not id_token:
        id_token = build_synthetic_id_token(email_addr, account_id, plan_type, expires_at)
    result = {
        "type": "codex",
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "email": email_addr,
        "name": first_text(email_addr, fallback.get("name"), "ChatGPT Account"),
        "plan_type": plan_type,
        "chatgpt_plan_type": plan_type,
        "id_token": id_token,
        "id_token_synthetic": not bool(original_id_token),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session_token": session_token,
        "last_refresh": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "expired": expires_at,
    }
    return {key: value for key, value in result.items() if value not in {"", None}}


def refresh_openai_with_rt(refresh_token):
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OPENAI_CODEX_CLIENT_ID,
        "scope": OPENAI_OAUTH_REFRESH_SCOPE,
    }
    request = Request(
        OPENAI_OAUTH_TOKEN_URL,
        data=urlencode(form).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": CPA_PROBE_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except Exception:
                payload = {"raw": raw}
            return response.getcode(), payload if isinstance(payload, dict) else {"data": payload}, raw
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"error": raw}
        return exc.code, payload if isinstance(payload, dict) else {"data": payload}, raw
    except URLError as exc:
        raise CpaUpstreamError(f"OpenAI RT 刷新请求失败: {compact_text(exc)}") from exc


def lifecycle_status_label(status):
    return {
        "active": "可用",
        "refreshed": "已刷新",
        "rt_rotated": "已刷新并轮换 RT",
        "rt_invalid": "RT 失效",
        "session_expired": "会话失效",
        "banned": "封禁/停用",
        "risk_blocked": "风控/受限",
        "usage_limit_reached": "额度耗尽",
        "needs_login_verification": "需要真实登录验证",
        "needs_login": "需要真实登录验证",
        "probe_failed": "探测失败",
        "queued": "待刷新",
        "uploaded": "已更新 CPA",
        "error": "错误",
    }.get(status, status or "未知")


def classify_oauth_refresh_error(status, data, raw):
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        code = first_text(error_obj.get("code"), error_obj.get("type"), error_obj.get("error"))
        message = first_text(error_obj.get("message"), data.get("error_description"), data.get("message"), raw)
    else:
        code = first_text(error_obj)
        message = first_text(data.get("error_description"), data.get("message"), data.get("detail"), raw)
    lowered = f"{code} {message}".lower()
    if status in {400, 401} or code in {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_request", "token_expired"}:
        if any(word in lowered for word in ("deactivated", "disabled", "banned", "suspended")):
            return "banned", message or code or f"HTTP {status}"
        return "rt_invalid", message or code or f"HTTP {status}"
    if status == 403:
        return "risk_blocked", message or "OpenAI 拒绝刷新请求"
    return "probe_failed", message or f"HTTP {status}"


def refresh_auth_file(auth_file, row=None):
    row = row if isinstance(row, dict) else {}
    auth_file = auth_file if isinstance(auth_file, dict) else {}
    refresh_token = extract_refresh_token(auth_file)
    email_addr = infer_email(row, auth_file)
    name = first_text(row.get("name"), auth_file.get("name"), email_addr)
    if not refresh_token:
        return {
            "ok": False,
            "status": "needs_login_verification",
            "statusLabel": lifecycle_status_label("needs_login_verification"),
            "email": email_addr,
            "name": name,
            "message": "缺少 refresh_token，需要真实登录/OAuth 验证；不会跳过短信或 OTP。",
            "authFile": None,
        }
    status, data, raw = refresh_openai_with_rt(refresh_token)
    if status == 200 and data.get("access_token"):
        next_rt = first_text(data.get("refresh_token"), refresh_token)
        session = {
            "email": email_addr,
            "access_token": first_text(data.get("access_token")),
            "refresh_token": next_rt,
            "id_token": first_text(data.get("id_token"), extract_id_token(auth_file)),
            "session_token": extract_session_token(auth_file),
            "expires_at": access_token_expires_at(first_text(data.get("access_token"))),
        }
        fallback = {**row, "email": email_addr, "name": name, "auth_index": item_auth_index(row)}
        new_auth = session_to_auth(session, fallback, require_refresh_token=True)
        if auth_file:
            new_auth = {**auth_file, **new_auth}
        rotated = next_rt != refresh_token
        state = "rt_rotated" if rotated else "refreshed"
        return {
            "ok": True,
            "status": state,
            "statusLabel": lifecycle_status_label(state),
            "email": new_auth.get("email") or email_addr,
            "name": name or new_auth.get("name"),
            "message": "RT 刷新成功" + ("，并已轮换" if rotated else ""),
            "authFile": new_auth,
        }
    state, message = classify_oauth_refresh_error(status, data, raw)
    return {
        "ok": False,
        "status": state,
        "statusLabel": lifecycle_status_label(state),
        "email": email_addr,
        "name": name,
        "message": compact_text(message),
        "authFile": None,
    }


def auth_filename(value, auth_file):
    name = str(value or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    if not name:
        name = first_text(auth_file.get("name"), auth_file.get("email"), auth_file.get("account_id"), "chatgpt-auth")
    name = re.sub(r"[^A-Za-z0-9._@+-]+", "-", name).strip(".-") or "chatgpt-auth"
    if not name.lower().endswith(".json"):
        name = f"{name}.json"
    return name


def upload_auth_file(base_url, management_key, name, auth_file):
    filename = auth_filename(name, auth_file if isinstance(auth_file, dict) else {})
    payload = request_json(
        f"{base_url}/v0/management/auth-files?name={quote(filename, safe='')}",
        method="POST",
        json_data=auth_file,
        request_headers=headers(management_key),
        timeout=30,
    )
    ok = payload.get("status") == "ok" or payload.get("success") is True or payload == {"status": "ok"}
    return {"uploaded": ok, "name": filename, "payload": payload, "error": "" if ok else "CPA 上传失败"}

def scan_401(payload):
    base_url, management_key, max_items, picked = candidates(payload)
    results = []
    for item in picked:
        if is_401_item(item):
            message, raw = status_message(item.get("message") or item.get("error") or "401 Unauthorized", status_code=401, action="401")
            results.append({**item, "name": item_name(item), "email": infer_email(item), "status_code": 401, "ok": False, "action": "401", "message": message, "raw_message": raw})
        else:
            results.append(probe_status(base_url, management_key, item))
    queueable = []
    for row in results:
        if str(row.get("status_code")) != "401" and row.get("action") != "401":
            continue
        name = item_name(row)
        auth_file = {}
        download_error = ""
        if name:
            try:
                auth_file = download_auth_file(base_url, management_key, name)
            except Exception as exc:
                download_error = compact_text(exc)
        has_rt = bool(extract_refresh_token(auth_file))
        state = "probe_failed" if download_error else ("queued" if has_rt else "needs_login_verification")
        queueable.append({
            **row,
            "name": name,
            "email": infer_email(row, auth_file) or first_text(row.get("email"), row.get("account")),
            "hasRefreshToken": has_rt,
            "status": state,
            "statusLabel": lifecycle_status_label(state),
            "downloadError": download_error,
        })
    return {
        "ok": True,
        "baseUrl": base_url,
        "total": len(picked),
        "maxItems": max_items,
        "results": results,
        "candidates": queueable,
        "summary": {
            "total": len(picked),
            "detected401": len(queueable),
            "withRefreshToken": sum(1 for item in queueable if item.get("hasRefreshToken")),
            "needsLoginVerification": sum(1 for item in queueable if not item.get("hasRefreshToken") and not item.get("downloadError")),
            "downloadFailed": sum(1 for item in queueable if item.get("downloadError")),
            "failed": sum(1 for item in results if item.get("action") == "probe_failed"),
            "skipped": sum(1 for item in results if item.get("action") == "skipped"),
        },
    }


def safe_queue_row(row):
    item = json_loads(row["cpa_item_json"], {})
    return {
        "id": row["id"],
        "baseUrl": row["cpa_base_url"],
        "name": row["cpa_name"],
        "authIndex": row["auth_index"],
        "email": row["email"],
        "status": row["status"],
        "statusLabel": row["status_label"] or lifecycle_status_label(row["status"]),
        "action": row["action"],
        "hasRefreshToken": bool(row["has_refresh_token"]),
        "lastError": row["last_error"],
        "item": item,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "refreshedAt": row["refreshed_at"],
    }


def list_queue(conn, base_url=""):
    if base_url:
        rows = conn.execute(
            "SELECT * FROM cpa_refresh_queue WHERE cpa_base_url = ? ORDER BY updated_at DESC",
            (normalize_cpa_base_url(base_url),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM cpa_refresh_queue ORDER BY updated_at DESC LIMIT 300").fetchall()
    return [safe_queue_row(row) for row in rows]


def upsert_queue_item(conn, base_url, item, auth_file, status=None, action="queued", last_error=""):
    timestamp = now_ms()
    name = item_name(item)
    auth_index = item_auth_index(item)
    email_addr = infer_email(item, auth_file) or first_text(item.get("email"), item.get("account"))
    has_rt = 1 if extract_refresh_token(auth_file) else 0
    final_status = status or ("queued" if has_rt else "needs_login_verification")
    existing = conn.execute(
        "SELECT * FROM cpa_refresh_queue WHERE cpa_base_url = ? AND cpa_name = ?",
        (base_url, name),
    ).fetchone()
    if existing and not has_rt:
        previous_auth_file = json_loads(existing["auth_file_json"], {})
        if extract_refresh_token(previous_auth_file):
            auth_file = previous_auth_file
            has_rt = 1
            final_status = "queued"
            last_error = ""
    if existing:
        email_addr = email_addr or existing["email"]
        auth_index = auth_index or existing["auth_index"]
    payload = {
        "cpa_base_url": base_url,
        "cpa_name": name,
        "auth_index": auth_index,
        "email": email_addr,
        "status": final_status,
        "status_label": lifecycle_status_label(final_status),
        "action": action,
        "has_refresh_token": has_rt,
        "last_error": compact_text(last_error),
        "cpa_item_json": json_dumps(item),
        "auth_file_json": json_dumps(auth_file if isinstance(auth_file, dict) else {}),
        "updated_at": timestamp,
    }
    if existing:
        payload["id"] = existing["id"]
    else:
        payload["id"] = generate_id("cpa")
        payload["created_at"] = timestamp
    payload.setdefault("created_at", timestamp)
    if not name:
        conn.execute(
            """
            INSERT INTO cpa_refresh_queue (
              id, cpa_base_url, cpa_name, auth_index, email, status, status_label, action,
              has_refresh_token, last_error, cpa_item_json, auth_file_json, created_at, updated_at
            ) VALUES (
              :id, :cpa_base_url, :cpa_name, :auth_index, :email, :status, :status_label, :action,
              :has_refresh_token, :last_error, :cpa_item_json, :auth_file_json, :created_at, :updated_at
            )
            """,
            payload,
        )
        return conn.execute("SELECT * FROM cpa_refresh_queue WHERE id = ?", (payload["id"],)).fetchone()

    conn.execute(
        """
        INSERT INTO cpa_refresh_queue (
          id, cpa_base_url, cpa_name, auth_index, email, status, status_label, action,
          has_refresh_token, last_error, cpa_item_json, auth_file_json, created_at, updated_at
        ) VALUES (
          :id, :cpa_base_url, :cpa_name, :auth_index, :email, :status, :status_label, :action,
          :has_refresh_token, :last_error, :cpa_item_json, :auth_file_json, :created_at, :updated_at
        )
        ON CONFLICT(cpa_base_url, cpa_name) WHERE cpa_name != '' DO UPDATE SET
          auth_index = COALESCE(NULLIF(excluded.auth_index, ''), cpa_refresh_queue.auth_index),
          email = COALESCE(NULLIF(excluded.email, ''), cpa_refresh_queue.email),
          status = CASE
            WHEN excluded.has_refresh_token = 0 AND cpa_refresh_queue.has_refresh_token = 1 THEN 'queued'
            ELSE excluded.status
          END,
          status_label = CASE
            WHEN excluded.has_refresh_token = 0 AND cpa_refresh_queue.has_refresh_token = 1 THEN '待刷新'
            ELSE excluded.status_label
          END,
          action = excluded.action,
          has_refresh_token = CASE
            WHEN excluded.has_refresh_token = 0 AND cpa_refresh_queue.has_refresh_token = 1 THEN 1
            ELSE excluded.has_refresh_token
          END,
          last_error = CASE
            WHEN excluded.has_refresh_token = 0 AND cpa_refresh_queue.has_refresh_token = 1 THEN ''
            ELSE excluded.last_error
          END,
          cpa_item_json = excluded.cpa_item_json,
          auth_file_json = CASE
            WHEN excluded.has_refresh_token = 0 AND cpa_refresh_queue.has_refresh_token = 1 THEN cpa_refresh_queue.auth_file_json
            ELSE excluded.auth_file_json
          END,
          updated_at = excluded.updated_at
        """,
        payload,
    )
    return conn.execute(
        "SELECT * FROM cpa_refresh_queue WHERE cpa_base_url = ? AND cpa_name = ?",
        (base_url, name),
    ).fetchone()


def import_401_to_queue(conn, payload):
    base_url, management_key = config(payload)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not items:
        scanned = scan_401(payload)
        items = scanned.get("candidates") or []
    imported = []
    errors = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item_name(item)
        if not name:
            errors.append({"item": item, "error": "缺少 CPA auth 文件名"})
            continue
        auth_file = {}
        try:
            auth_file = download_auth_file(base_url, management_key, name)
        except Exception as exc:
            errors.append({"name": name, "error": compact_text(exc)})
            continue
        state = "queued" if extract_refresh_token(auth_file) else "needs_login_verification"
        last_error = "" if state == "queued" else "缺少 refresh_token；需要完成真实登录/OAuth 验证后才能刷新 CPA。"
        row = upsert_queue_item(conn, base_url, {**item, "name": name}, auth_file, status=state, action="imported", last_error=last_error)
        imported.append(safe_queue_row(row))
    return {"ok": True, "imported": len(imported), "errors": errors, "queue": list_queue(conn, base_url)}


def refresh_queue(conn, payload):
    base_url, management_key = config(payload)
    raw_ids = payload.get("ids") or payload.get("queueIds") or []
    ids = [str(item).strip() for item in raw_ids if str(item).strip()] if isinstance(raw_ids, list) else []
    limit = max(1, min(int(payload.get("limit") or 50), 100))
    update_cpa = payload.get("updateCpa") is not False and payload.get("upload") is not False
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT * FROM cpa_refresh_queue
            WHERE cpa_base_url = ? AND id IN ({placeholders})
            ORDER BY updated_at ASC
            """,
            [base_url, *ids],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM cpa_refresh_queue
            WHERE cpa_base_url = ? AND status IN ('queued','error','probe_failed','rt_invalid')
            ORDER BY updated_at ASC LIMIT ?
            """,
            (base_url, limit),
        ).fetchall()
    results = []
    uploaded = 0
    for row in rows:
        queue_item = safe_queue_row(row)
        item = json_loads(row["cpa_item_json"], {})
        auth_file = json_loads(row["auth_file_json"], {})
        name = row["cpa_name"]
        try:
            if not auth_file and name:
                auth_file = download_auth_file(base_url, management_key, name)
            result = refresh_auth_file(auth_file, {**item, "name": name, "auth_index": row["auth_index"], "email": row["email"]})
            state = result.get("status") or ("uploaded" if result.get("ok") else "error")
            action = "refreshed"
            last_error = "" if result.get("ok") else result.get("message") or "刷新失败"
            new_auth_file = result.get("authFile") if isinstance(result.get("authFile"), dict) else auth_file
            if result.get("ok") and update_cpa:
                upload = upload_auth_file(base_url, management_key, name, new_auth_file)
                result["upload"] = upload
                if upload.get("uploaded"):
                    uploaded += 1
                    state = "uploaded"
                    action = "uploaded"
                    last_error = ""
                else:
                    state = "error"
                    action = "upload_failed"
                    last_error = upload.get("error") or "CPA 上传失败"
                    result["ok"] = False
                    result["message"] = last_error
            timestamp = now_ms()
            conn.execute(
                """
                UPDATE cpa_refresh_queue
                SET status = ?, status_label = ?, action = ?, has_refresh_token = ?, last_error = ?,
                    auth_file_json = ?, updated_at = ?, refreshed_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    lifecycle_status_label(state),
                    action,
                    1 if extract_refresh_token(new_auth_file) else 0,
                    compact_text(last_error),
                    json_dumps(new_auth_file if isinstance(new_auth_file, dict) else {}),
                    timestamp,
                    timestamp if result.get("ok") else row["refreshed_at"],
                    row["id"],
                ),
            )
            conn.commit()
            results.append({**queue_item, **result, "id": row["id"], "status": state, "statusLabel": lifecycle_status_label(state)})
        except Exception as exc:
            timestamp = now_ms()
            detail = compact_text(exc)
            conn.execute(
                "UPDATE cpa_refresh_queue SET status = ?, status_label = ?, action = ?, last_error = ?, updated_at = ? WHERE id = ?",
                ("error", lifecycle_status_label("error"), "refresh_failed", detail, timestamp, row["id"]),
            )
            conn.commit()
            results.append({**queue_item, "ok": False, "status": "error", "statusLabel": lifecycle_status_label("error"), "message": detail})
    final_queue = list_queue(conn, base_url)
    return {
        "ok": True,
        "results": results,
        "summary": {
            "total": len(results),
            "refreshed": sum(1 for item in results if item.get("ok")),
            "uploaded": uploaded,
            "needsLoginVerification": sum(1 for item in final_queue if item.get("status") == "needs_login_verification"),
            "failed": sum(1 for item in results if not item.get("ok")),
        },
        "queue": final_queue,
    }
