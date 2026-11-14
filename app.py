"""
app.py — Flask entry point for Identity Sync v3.

Multi-tenant OIDC proxy: each org authenticates against its own Entra tenant App Registration.
All secrets (Entra credentials, UYUNI sync passwords) loaded from Azure Key Vault at boot.
AZURE_KEYVAULT_URL is mandatory — no env var fallback in production.

NOTE: Requires gunicorn with a single worker (--workers 1 --threads N).
      Multi-worker deployments need sticky sessions or an external session store,
      because _SESSIONS is an in-process dict.

Set IDENTITY_SYNC_TESTING=1 to skip KV initialization (test environments only).
"""

import collections
import functools
import html
import ipaddress
import logging
import os
import secrets
import ssl
import threading
import time
from datetime import datetime, timezone

import requests as _requests
import websocket as _websocket
from flask import Flask, make_response, redirect, request
from flask_sock import Sock

import secrets_client
from oidc_client import extract_identity, get_client, resolve_role
from uyuni_user_provisioner import ensure_user
from uyuni_web_client import get_session_cookies

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    force=True,
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_TESTING = os.environ.get("IDENTITY_SYNC_TESTING") == "1"

# ── Config — read once at import ──────────────────────────────────────────────
_UYUNI_URL    = os.environ.get("UYUNI_URL", "").rstrip("/")
_FLASK_BASE   = os.environ.get("IDENTITY_SYNC_URL", "").rstrip("/")
_CA_CERT_PATH = os.environ.get("UYUNI_CA_CERT", "").strip()
_SESSION_TTL  = int(os.environ.get("SESSION_TTL_HOURS", "12")) * 3600

if not _TESTING and not _CA_CERT_PATH:
    raise RuntimeError("UYUNI_CA_CERT must be set. SSL verification bypass not allowed.")
_CA_CERT: str | bool = _CA_CERT_PATH or False

_UYUNI_URL_B  = _UYUNI_URL.encode()
_FLASK_BASE_B = _FLASK_BASE.encode()


def _check_config() -> None:
    required = [
        "UYUNI_URL", "IDENTITY_SYNC_URL", "UYUNI_ORGS", "UYUNI_CA_CERT", "AZURE_KEYVAULT_URL",
    ]
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def _session_cleanup_loop() -> None:
    """Daemon: evict expired sessions every 10 min regardless of login activity."""
    while True:
        time.sleep(600)
        now = time.time()
        with _SESSIONS_LOCK:
            expired = [k for k, v in _SESSIONS.items() if v["expires"] < now]
            for k in expired:
                del _SESSIONS[k]
        if expired:
            logger.debug("Session cleanup: removed %d expired", len(expired))


def _boot() -> None:
    _check_config()
    orgs = [o.strip().lower() for o in os.environ.get("UYUNI_ORGS", "").split(",") if o.strip()]
    secrets_client.init_secrets(os.environ["AZURE_KEYVAULT_URL"], orgs)


if not _TESTING:
    _boot()
    threading.Thread(target=_session_cleanup_loop, daemon=True, name="session-cleanup").start()

# ── Flask app ─────────────────────────────────────────────────────────────────
app  = Flask(__name__)
sock = Sock(app)

# Applied manually on auth pages only — NOT via after_request, which would
# incorrectly attach our CSP to proxied UYUNI responses.
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Content-Type-Options":  "nosniff",
    "X-Frame-Options":         "DENY",
    "Referrer-Policy":         "no-referrer",
}

# ── Session store ─────────────────────────────────────────────────────────────
# dict: opaque_token → {cookies, expires, user}
# Thread-safe: gunicorn runs 16 threads on 1 process (shared _SESSIONS).

_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()


def _new_session(cookies: dict, user: str) -> str:
    token = secrets.token_urlsafe(32)
    with _SESSIONS_LOCK:
        _SESSIONS[token] = {"cookies": cookies, "expires": time.time() + _SESSION_TTL, "user": user}
    return token


def _get_uyuni_cookies(token: str) -> dict:
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(token)
        if not entry or entry["expires"] < time.time():
            return {}
        return entry["cookies"].copy()


# ── Auth failure rate limiting ────────────────────────────────────────────────
# Tracks per-IP auth failure timestamps. No external dependency.
# Only authentication/authorisation failures counted — not infra errors or bad params.

_AUTH_FAILURES: collections.defaultdict[str, list[float]] = collections.defaultdict(list)
_AUTH_FAILURES_LOCK = threading.Lock()
_AUTH_FAILURE_WINDOW = 60.0   # seconds
_AUTH_FAILURE_MAX    = 15     # failures per IP per window


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if xff:
        try:
            ipaddress.ip_address(xff)
            return xff
        except ValueError:
            pass
    return request.remote_addr or "unknown"


def _check_auth_rate_limit() -> bool:
    """Return True if IP has exceeded the auth failure threshold."""
    ip = _client_ip()
    cutoff = time.time() - _AUTH_FAILURE_WINDOW
    with _AUTH_FAILURES_LOCK:
        recent = [t for t in _AUTH_FAILURES[ip] if t > cutoff]
        _AUTH_FAILURES[ip] = recent
        if len(recent) >= _AUTH_FAILURE_MAX:
            logger.warning("Auth rate limit exceeded for %s (%d failures in %ds)",
                           ip, len(recent), int(_AUTH_FAILURE_WINDOW))
            return True
    return False


def _record_auth_failure() -> None:
    ip = _client_ip()
    cutoff = time.time() - _AUTH_FAILURE_WINDOW
    with _AUTH_FAILURES_LOCK:
        _AUTH_FAILURES[ip].append(time.time())
        if len(_AUTH_FAILURES) > 200:
            stale = [k for k, v in _AUTH_FAILURES.items() if not any(t > cutoff for t in v)]
            for k in stale:
                del _AUTH_FAILURES[k]


# ── Org helpers ───────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _known_orgs() -> frozenset[str]:
    orgs = [o.strip().lower() for o in os.environ.get("UYUNI_ORGS", "").split(",") if o.strip()]
    if not orgs:
        raise RuntimeError("UYUNI_ORGS must contain at least one organization")
    return frozenset(orgs)


# ── Audit ─────────────────────────────────────────────────────────────────────

_AUDIT_TABLE_CLIENT = None
_AUDIT_TABLE_CLIENT_LOCK = threading.Lock()


def _get_audit_table():
    global _AUDIT_TABLE_CLIENT
    if _AUDIT_TABLE_CLIENT is not None:
        return _AUDIT_TABLE_CLIENT
    conn = os.environ.get("AUDIT_TABLE_CONNECTION", "").strip()
    if not conn:
        return None
    with _AUDIT_TABLE_CLIENT_LOCK:
        if _AUDIT_TABLE_CLIENT is None:
            from azure.data.tables import TableServiceClient
            _AUDIT_TABLE_CLIENT = (
                TableServiceClient.from_connection_string(conn)
                .get_table_client("uyuniaccesslog")
            )
    return _AUDIT_TABLE_CLIENT


def _audit(entity: dict) -> None:
    table = _get_audit_table()
    if table is None:
        return
    now = datetime.now(timezone.utc)
    try:
        user_raw  = entity.get("user", "system")
        user_safe = "".join(c for c in str(user_raw) if c.isalnum() or c in "@.-_")[:128]
        table.upsert_entity({
            "PartitionKey": now.strftime("%Y-%m"),
            "RowKey":       f"{now.isoformat()}-{secrets.token_hex(8)}",
            "user":         user_safe,
            "action":       str(entity.get("action", "unknown"))[:64],
            "role":         str(entity.get("role", ""))[:64],
            "timestamp":    now.isoformat(),
        })
    except Exception as e:
        logger.error("Audit write failed: %s (action=%s)", e, entity.get("action"))


# ── HTML helpers ──────────────────────────────────────────────────────────────

_STYLE = (
    "body{font-family:-apple-system,sans-serif;max-width:480px;"
    "margin:80px auto;padding:0 24px;color:#1b1b1b}"
    "h2{color:#0078d4}.err{color:#d13438;margin-top:20px}"
    "a,a:visited{color:#0078d4}"
    ".btn{display:inline-block;margin:8px 8px 0 0;padding:10px 20px;"
    "background:#0078d4;color:#fff;text-decoration:none;border-radius:4px;"
    "border:none;font-size:15px;cursor:pointer}"
    "select{width:100%;padding:10px;margin-bottom:12px;"
    "border:1px solid #ccc;border-radius:4px;font-size:15px;box-sizing:border-box}"
)


def _html_response(body: str, status: int = 200):
    resp = make_response(body, status)
    resp.content_type = "text/html; charset=utf-8"
    resp.headers.update(_SECURITY_HEADERS)
    return resp


def _render_error(message: str, status: int = 400):
    page = (
        "<!DOCTYPE html><html lang='it'><head><meta charset='utf-8'>"
        "<title>Accesso negato — UYUNI</title>"
        f"<style>{_STYLE}</style></head><body>"
        "<h2>Accesso negato</h2>"
        f"<p class='err'>{html.escape(message)}</p>"
        "<a class='btn' href='/api/login'>&#8592; Riprova</a>"
        "</body></html>"
    )
    return _html_response(page, status)


def _render_org_selector(orgs: frozenset[str]):
    options = "".join(
        f"<option value='{html.escape(o)}'>{html.escape(o.upper())}</option>"
        for o in sorted(orgs)
    )
    page = (
        "<!DOCTYPE html><html lang='it'><head><meta charset='utf-8'>"
        "<title>Seleziona organizzazione — UYUNI</title>"
        f"<style>{_STYLE}</style></head><body>"
        "<h2>Seleziona organizzazione</h2>"
        f"<form method='GET' action='/api/login'>"
        f"<select name='org' autofocus>{options}</select>"
        "<button type='submit' class='btn'>Accedi</button>"
        "</form>"
        "</body></html>"
    )
    return _html_response(page)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def root():
    return redirect("/api/login", 302)


@app.route("/api/health")
def health():
    return {"status": "ok"}


@app.route("/api/login", methods=["GET"])
def login_selector():
    """
    Org selector entry point.
    Clears any existing session. Redirects directly if only one org configured
    or if ?org= query param is present (form submission).
    """
    old = request.cookies.get("spm_session", "")
    if old:
        with _SESSIONS_LOCK:
            _SESSIONS.pop(old, None)

    org = request.args.get("org", "").strip().lower()
    if org:
        return redirect(f"/api/login/{org}", 302)

    orgs = _known_orgs()
    if len(orgs) == 1:
        org = next(iter(orgs))
        return redirect(f"/api/login/{org}", 302)

    resp = _render_org_selector(orgs)
    resp.set_cookie("spm_session", "", expires=0,
                    httponly=True, secure=True, samesite="Lax", path="/")
    return resp


def _callback_error(message: str, status: int = 400):
    """_render_error that also clears the oidc_* cookies set during login_org."""
    resp = _render_error(message, status)
    resp.set_cookie("oidc_state", "", expires=0,
                    httponly=True, secure=True, samesite="Lax", path="/api/callback")
    resp.set_cookie("oidc_org", "", expires=0,
                    httponly=True, secure=True, samesite="Lax", path="/api/callback")
    return resp


@app.route("/api/login/<org>", methods=["GET"])
def login_org(org: str):
    """
    Start OIDC flow for a specific org.
    Sets oidc_state (anti-CSRF) and oidc_org cookies, both scoped to /api/callback.
    """
    org = org.strip().lower()
    if org not in _known_orgs():
        return _render_error(f"Organizzazione {html.escape(org)!r} non riconosciuta.", 404)

    state  = secrets.token_urlsafe(32)
    client = get_client(org)

    resp = redirect(client.get_auth_url(state=state), 302)
    resp.set_cookie("oidc_state", state,
                    httponly=True, secure=True, samesite="Lax",
                    path="/api/callback", max_age=600)
    resp.set_cookie("oidc_org", org,
                    httponly=True, secure=True, samesite="Lax",
                    path="/api/callback", max_age=600)
    resp.set_cookie("spm_session", "", expires=0,
                    httponly=True, secure=True, samesite="Lax", path="/")
    resp.headers.update(_SECURITY_HEADERS)
    return resp


@app.route("/api/callback", methods=["GET"])
def callback():
    """
    OAuth2 callback. Sequence:
    1. Validate oidc_org cookie → resolve per-org OIDCClient
    2. Anti-CSRF state check
    3. Exchange auth code for JWT
    4. Verify JWT issuer matches org's tenant (prevents cross-tenant attacks)
    5. Extract identity + resolve App Role
    6. Provision UYUNI account (create or sync role + reset ephemeral password)
    7. Web login → JSESSIONID + pxt-session-cookie
    8. Create server-side session, set spm_session cookie
    """
    if _check_auth_rate_limit():
        return _callback_error("Troppi tentativi falliti. Riprovare tra poco.", 429)

    # 1. Org from cookie (set during login_org)
    org = request.cookies.get("oidc_org", "").strip().lower()
    if not org or org not in _known_orgs():
        logger.warning("Callback: missing or unknown oidc_org cookie: %r", org)
        return _callback_error("Sessione non valida o scaduta. Riprova.", 400)

    client = get_client(org)

    # 2. Anti-CSRF — compare_digest is constant-time (no timing attack)
    expected = request.cookies.get("oidc_state", "")
    received = request.args.get("state", "")
    if not expected or not secrets.compare_digest(expected, received):
        logger.warning("CSRF state mismatch for org %r", org)
        return _callback_error("Sessione non valida o scaduta. Riprova.", 400)

    # Microsoft OIDC errors (MFA failure, Conditional Access, user cancel)
    if request.args.get("error"):
        logger.warning("OIDC error for org %r: %s — %s",
                       org, request.args["error"], request.args.get("error_description"))
        _record_auth_failure()
        return _callback_error("Autenticazione fallita. Riprovare.", 401)

    code = request.args.get("code", "").strip()
    if not code:
        return _callback_error("Authorization code mancante.", 400)

    # 3. Token exchange (server→Microsoft, never via browser)
    token = client.exchange_code(code)
    if token is None:
        _record_auth_failure()
        return _callback_error("Errore autenticazione. Riprovare.", 401)

    # 4. Issuer verification — mandatory for multi-tenant security
    if not client.verify_issuer(token):
        logger.error("Issuer mismatch in token for org %r", org)
        _record_auth_failure()
        return _callback_error("Token non valido.", 401)

    # 5. Identity + role
    identity = extract_identity(token)
    if not identity:
        return _callback_error("Impossibile determinare identità dal token.", 400)
    username, domain = identity

    authorized, role = resolve_role(token)
    uyuni_login = f"{username}@{domain}"
    if not authorized:
        _audit({"user": uyuni_login, "action": "denied"})
        logger.info("Access denied: %r (org=%s)", uyuni_login, org)
        _record_auth_failure()
        return _callback_error("Utente non autorizzato. Contattare l'amministratore.", 403)

    # 6. UYUNI account provisioning
    try:
        sync_pass = secrets_client.get_sync_password(org)
        uyuni_pass = ensure_user(_UYUNI_URL, org, uyuni_login, role, sync_pass)
    except Exception as e:
        logger.error("UYUNI provisioning failed for %r: %s", uyuni_login, e, exc_info=True)
        return _callback_error("Errore provisioning account UYUNI. Riprovare.", 500)

    # 7. Web login → session cookies
    try:
        uyuni_cookies = get_session_cookies(_UYUNI_URL, uyuni_login, uyuni_pass, verify=_CA_CERT)
    except Exception as e:
        logger.error("UYUNI web login failed for %r: %s", uyuni_login, e, exc_info=True)
        return _callback_error("Errore accesso UYUNI. Riprovare.", 500)

    _audit({"user": uyuni_login, "action": "login", "role": role or "readonly"})
    logger.info("Login: %r (org=%s, role=%s)", uyuni_login, org, role or "readonly")

    # 8. Server-side session — browser never sees UYUNI cookies directly
    session_token = _new_session(uyuni_cookies, uyuni_login)
    resp = redirect("/rhn/manager/systems/list/all", 302)
    resp.headers.update(_SECURITY_HEADERS)
    resp.set_cookie("spm_session", session_token,
                    httponly=True, secure=True, samesite="Lax", path="/")
    resp.set_cookie("oidc_state", "", expires=0,
                    httponly=True, secure=True, samesite="Lax", path="/api/callback")
    resp.set_cookie("oidc_org", "", expires=0,
                    httponly=True, secure=True, samesite="Lax", path="/api/callback")
    return resp


@app.route("/api/logout", methods=["GET"])
@app.route("/rhn/manager/logout", methods=["GET", "POST"])
def logout():
    """
    Logout: remove server-side session, invalidate UYUNI JSESSIONID (best-effort),
    clear spm_session cookie, redirect to /api/login.
    """
    token = request.cookies.get("spm_session", "")
    with _SESSIONS_LOCK:
        entry = _SESSIONS.pop(token, None)
    if entry:
        try:
            _requests.get(
                f"{_UYUNI_URL}/rhn/manager/logout",
                cookies=entry["cookies"],
                verify=_CA_CERT,
                timeout=5,
                allow_redirects=False,
            )
        except Exception as e:
            logger.debug("UYUNI logout call failed (non-critical): %s", e)
        user = entry.get("user", "unknown")
        _audit({"user": user, "action": "logout"})
        logger.info("Logout: %r", user)
    resp = redirect("/api/login", 302)
    resp.set_cookie("spm_session", "", expires=0,
                    httponly=True, secure=True, samesite="Lax", path="/")
    return resp


# ── HTTP Proxy ────────────────────────────────────────────────────────────────

_PROXY_SKIP_REQ  = frozenset({"host", "cookie", "content-length",
                               "connection", "te", "keep-alive"})
_PROXY_SKIP_RESP = frozenset({"transfer-encoding", "content-encoding",
                               "content-length", "set-cookie",
                               "connection", "keep-alive"})
_UYUNI_LOGIN_PATTERNS = ("/rhn/manager/login", "/rhn/Login2.do", "/rhn/Login.do")
_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _do_proxy(full_path: str):
    uyuni_cookies = _get_uyuni_cookies(request.cookies.get("spm_session", ""))
    target = f"{_UYUNI_URL}/{full_path.lstrip('/')}"
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", errors="replace")

    fwd_headers: dict[str, str] = {}
    for k, v in request.headers:
        k_lower = k.lower()
        if k_lower in _PROXY_SKIP_REQ:
            continue
        if k_lower == "origin":
            v = _UYUNI_URL
        elif k_lower == "referer" and _FLASK_BASE:
            v = v.replace(_FLASK_BASE, _UYUNI_URL)
        fwd_headers[k] = v

    try:
        upstream = _requests.request(
            method=request.method,
            url=target,
            headers=fwd_headers,
            cookies=uyuni_cookies,
            data=request.get_data() if request.method in ("POST", "PUT", "PATCH", "DELETE") else None,
            allow_redirects=False,
            verify=_CA_CERT,
            timeout=60,
        )
    except _requests.exceptions.RequestException as e:
        logger.error("Proxy error for %s: %s", full_path, e)
        return make_response("Errore connessione UYUNI. Riprovare.", 502)

    if upstream.status_code in (301, 302, 303, 307, 308):
        location = upstream.headers.get("Location", "")
        if any(p in location for p in _UYUNI_LOGIN_PATTERNS):
            is_xhr = (
                request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or "application/json" in request.headers.get("Accept", "")
            )
            if is_xhr:
                return make_response('{"error":"session_expired"}', 401,
                                     {"Content-Type": "application/json"})
            return redirect("/api/login", 302)
        rewritten = location.replace(_UYUNI_URL, _FLASK_BASE) if _FLASK_BASE else location
        resp = make_response("", upstream.status_code)
        resp.headers["Location"] = rewritten
        return resp

    content = upstream.content
    if "text/html" in upstream.headers.get("Content-Type", "") and _FLASK_BASE:
        content = content.replace(_UYUNI_URL_B, _FLASK_BASE_B)

    resp = make_response(content, upstream.status_code)
    for k, v in upstream.headers.items():
        if k.lower() not in _PROXY_SKIP_RESP:
            resp.headers[k] = v
    return resp


# ── WebSocket Proxy ───────────────────────────────────────────────────────────

@sock.route("/rhn/websocket/<path:p>")
def ws_proxy(ws, p: str):
    """Bidirectional WebSocket proxy: browser ↔ Flask ↔ UYUNI."""
    uyuni_cookies = _get_uyuni_cookies(request.cookies.get("spm_session", ""))
    if not uyuni_cookies:
        logger.info("WS: no active session, closing %s", p)
        ws.close()
        return

    uyuni_ws  = _UYUNI_URL.replace("https://", "wss://").replace("http://", "ws://")
    uyuni_ws += f"/rhn/websocket/{p}"
    if request.query_string:
        uyuni_ws += "?" + request.query_string.decode("utf-8", errors="replace")
    cookie_str = "; ".join(f"{k}={v}" for k, v in uyuni_cookies.items())
    sslopt = {"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": _CA_CERT_PATH}

    try:
        upstream = _websocket.create_connection(uyuni_ws, cookie=cookie_str, sslopt=sslopt)
    except Exception as e:
        logger.error("WS connect failed for %s: %s", p, e)
        ws.close()
        return

    def _fwd_upstream():
        try:
            while True:
                msg = upstream.recv()
                if msg is None:
                    break
                ws.send(msg)
        except Exception:
            pass

    threading.Thread(target=_fwd_upstream, daemon=True).start()

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            upstream.send(msg)
    except Exception:
        pass
    finally:
        upstream.close()


# ── Route registrations ───────────────────────────────────────────────────────

@app.route("/rhn/", defaults={"p": ""}, methods=_PROXY_METHODS)
@app.route("/rhn/<path:p>", methods=_PROXY_METHODS)
def proxy(p: str):
    return _do_proxy(f"/rhn/{p}")


@app.route("/<path:p>", methods=_PROXY_METHODS)
def proxy_catchall(p: str):
    if p.startswith("api"):
        return make_response("Not found", 404)
    return _do_proxy(f"/{p}")