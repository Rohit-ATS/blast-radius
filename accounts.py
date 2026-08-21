"""Accounts, API keys, monitors, alerts and the security log.

Everything the platform side of Blast Radius needs, in one SQLite file that is
separate from `deps.db`. The graph database is crawler-owned and opened
read-only by the API; nothing here writes to it.

Design notes worth knowing before changing anything:

* **Keys are never stored.** A key is shown exactly once, at creation. What is
  persisted is a SHA-256 of the secret plus a short non-secret prefix used to
  identify the row in the UI. A stolen database yields no working key.
* **Auth is pluggable.** The default backend is local (PBKDF2-HMAC-SHA256,
  310k iterations, per-user salt) so the whole thing runs offline with no
  third-party account. Set SUPABASE_URL and SUPABASE_ANON_KEY and the same
  calls are proxied to Supabase's GoTrue instead, with a local mirror row
  keyed on the Supabase user id so keys and monitors still belong to someone.
* **Monitors are the 24-hour watch.** A monitor is a package the account cares
  about. A worker thread re-evaluates each one on a timer, compares the answer
  to the last observation, and writes an alert when something moved. Alerts
  land in the account's stream, which the dashboard reads over SSE.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time

import config

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_DB = os.environ.get("ACCOUNTS_DB", os.path.join(HERE, "accounts.db"))

SUPABASE_URL = config.SUPABASE_URL
SUPABASE_ANON_KEY = config.SUPABASE_ANON_KEY
SUPABASE_SERVICE_KEY = config.SUPABASE_SERVICE_KEY
SUPABASE_ON = config.SUPABASE_ON

KEY_PREFIX = "brk_live_"
PBKDF2_ROUNDS = 310_000
SESSION_DAYS = 30

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class AuthError(Exception):
    """Anything the caller did wrong, carrying an HTTP status."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS accounts (
  id            TEXT PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  name          TEXT,
  pw_hash       TEXT,           -- null when the account is Supabase-backed
  pw_salt       TEXT,
  provider      TEXT NOT NULL DEFAULT 'local',
  created_at    REAL NOT NULL,
  last_seen_at  REAL
);

CREATE TABLE IF NOT EXISTS sessions (
  token       TEXT PRIMARY KEY,
  account_id  TEXT NOT NULL,
  created_at  REAL NOT NULL,
  expires_at  REAL NOT NULL,
  ip          TEXT,
  agent       TEXT
);
CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);

CREATE TABLE IF NOT EXISTS api_keys (
  id           TEXT PRIMARY KEY,
  account_id   TEXT NOT NULL,
  name         TEXT NOT NULL,
  prefix       TEXT NOT NULL,   -- non-secret, shown in the vault
  key_hash     TEXT NOT NULL,   -- sha256 of the full secret
  created_at   REAL NOT NULL,
  last_used_at REAL,
  calls        INTEGER NOT NULL DEFAULT 0,
  revoked_at   REAL
);
CREATE INDEX IF NOT EXISTS keys_account ON api_keys(account_id);
CREATE INDEX IF NOT EXISTS keys_hash    ON api_keys(key_hash);

CREATE TABLE IF NOT EXISTS monitors (
  id            TEXT PRIMARY KEY,
  account_id    TEXT NOT NULL,
  package       TEXT NOT NULL,
  label         TEXT,
  created_at    REAL NOT NULL,
  last_check_at REAL,
  last_total    INTEGER,
  last_status   TEXT,
  checks        INTEGER NOT NULL DEFAULT 0,
  active        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS monitors_account ON monitors(account_id);

CREATE TABLE IF NOT EXISTS alerts (
  id          TEXT PRIMARY KEY,
  account_id  TEXT NOT NULL,
  monitor_id  TEXT,
  level       TEXT NOT NULL,          -- info | notable | high | critical
  title       TEXT NOT NULL,
  detail      TEXT,
  data        TEXT,
  created_at  REAL NOT NULL,
  read_at     REAL
);
CREATE INDEX IF NOT EXISTS alerts_account ON alerts(account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT,
  event       TEXT NOT NULL,
  detail      TEXT,
  ip          TEXT,
  agent       TEXT,
  key_id      TEXT,
  at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_account ON audit(account_id, at DESC);

CREATE TABLE IF NOT EXISTS webhooks (
  id            TEXT PRIMARY KEY,
  account_id    TEXT NOT NULL,
  url           TEXT NOT NULL,
  secret        TEXT NOT NULL,       -- shown to the owner; needed to verify
  label         TEXT,
  created_at    REAL NOT NULL,
  last_at       REAL,
  last_ok       INTEGER,
  last_detail   TEXT,
  deliveries    INTEGER NOT NULL DEFAULT 0,
  failures      INTEGER NOT NULL DEFAULT 0,
  consecutive   INTEGER NOT NULL DEFAULT 0,
  active        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS webhooks_account ON webhooks(account_id);
"""

_init_lock = threading.Lock()
_ready = False


def db() -> sqlite3.Connection:
    """One connection per call. WAL, so the monitor worker and request threads
    never block each other.

    The `_init_lock` only serialises threads inside one process. Under gunicorn
    there are several processes, and on a cold deploy they all boot at the same
    instant against a database file that does not exist yet — so they all run
    `executescript(SCHEMA)` concurrently and one of them loses:

        sqlite3.OperationalError: database is locked
        Application startup failed. Exiting.

    Found by running the real container with two workers; it would have failed
    identically on the first Render deploy. `busy_timeout` makes a writer wait
    for the lock instead of failing instantly, and the retry covers the window
    before WAL mode is established. The schema is all `IF NOT EXISTS`, so
    several processes applying it at once is harmless once they can take turns.
    """
    global _ready
    conn = sqlite3.connect(ACCOUNTS_DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")

    if not _ready:
        with _init_lock:
            if not _ready:
                for attempt in range(6):
                    try:
                        conn.executescript(SCHEMA)
                        conn.commit()
                        _ready = True
                        break
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc) and "busy" not in str(exc):
                            raise
                        time.sleep(0.4 * (attempt + 1))
                else:
                    raise sqlite3.OperationalError(
                        f"{ACCOUNTS_DB} stayed locked while applying the schema; "
                        "another process may be holding a long write transaction")
    return conn


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return dk.hex(), salt


def verify_password(password: str, stored: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored)


# --------------------------------------------------------------------------
# audit log
# --------------------------------------------------------------------------

def log_event(account_id: str | None, event: str, detail: str = "",
              ip: str = "", agent: str = "", key_id: str | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO audit (account_id, event, detail, ip, agent, key_id, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (account_id, event, detail, ip, (agent or "")[:180], key_id, _now()))
        conn.commit()


def security_log(account_id: str, limit: int = 60) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT event, detail, ip, agent, key_id, at FROM audit "
            "WHERE account_id = ? ORDER BY at DESC LIMIT ?",
            (account_id, limit)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# supabase (only used when configured)
# --------------------------------------------------------------------------

def _gotrue(method: str, path: str, payload: dict | None = None,
            token: str | None = None) -> tuple[int, dict]:
    """One call to Supabase Auth. Returns (status, body) rather than raising,
    because several non-2xx responses are ordinary outcomes here — a duplicate
    signup, a wrong password, an unconfirmed email."""
    import requests

    headers = {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        res = requests.request(method, f"{SUPABASE_URL}/auth/v1{path}",
                               json=payload, headers=headers, timeout=15)
    except Exception as exc:
        raise AuthError(
            f"could not reach Supabase at {SUPABASE_URL} "
            f"({exc.__class__.__name__}). Check SUPABASE_URL in .env.",
            502, "supabase_unreachable")

    body: dict = {}
    try:
        body = res.json()
    except Exception:
        body = {"raw": res.text[:200]}
    return res.status_code, body


def _gotrue_error(status: int, body: dict) -> AuthError:
    """Map GoTrue's vocabulary onto ours, so the browser sees one shape."""
    msg = (body.get("msg") or body.get("error_description")
           or body.get("message") or body.get("error") or "Supabase rejected the request")
    low = str(msg).lower()

    if status == 401 and "api key" in low:
        return AuthError("SUPABASE_ANON_KEY is not valid for this project. "
                         "Check Settings → API in the Supabase dashboard.",
                         500, "supabase_misconfigured")
    if "already registered" in low or "already exists" in low or status == 422 and "user" in low:
        return AuthError("an account with that email already exists.", 409, "email_taken")
    if "invalid login" in low or "invalid credentials" in low:
        return AuthError("email or password is wrong.", 401, "bad_credentials")
    if "email not confirmed" in low or "not confirmed" in low:
        return AuthError("confirm your email address first — check your inbox for "
                         "the link Supabase sent.", 403, "email_unconfirmed")
    if "password" in low and ("short" in low or "least" in low):
        return AuthError(str(msg), 400, "weak_password")
    if "rate" in low or status == 429:
        return AuthError("Supabase is rate limiting this project; try again shortly.",
                         429, "supabase_rate_limited")
    return AuthError(str(msg), status if 400 <= status < 500 else 502, "supabase_error")


def supabase_user(token: str) -> dict:
    """Confirm an access token really is who it claims to be.

    The token comes back from GoTrue over TLS, but it is still a value this
    process did not mint, so it is verified against the issuer rather than
    decoded and trusted.
    """
    status, body = _gotrue("GET", "/user", token=token)
    if status >= 400 or not body.get("id"):
        raise _gotrue_error(status, body)
    return body


def supabase_ping() -> dict:
    """Used by setup_check.py and /api/platform-stats. Never raises."""
    if not SUPABASE_ON:
        return {"configured": False, "reachable": None,
                "detail": "SUPABASE_URL / SUPABASE_ANON_KEY are not set; "
                          "local password auth is in use."}
    try:
        status, body = _gotrue("GET", "/settings")
    except AuthError as exc:
        return {"configured": True, "reachable": False, "detail": exc.message}

    if status == 401:
        return {"configured": True, "reachable": True, "key_valid": False,
                "detail": "the project answered but rejected SUPABASE_ANON_KEY."}
    if status >= 400:
        return {"configured": True, "reachable": True, "key_valid": False,
                "detail": f"Supabase returned {status}."}

    return {"configured": True, "reachable": True, "key_valid": True,
            "url": SUPABASE_URL,
            "email_signup_enabled": not body.get("disable_signup", False),
            "autoconfirm": bool(body.get("mailer_autoconfirm", False)),
            "external_providers": sorted(
                k for k, v in (body.get("external") or {}).items() if v)}


def _mirror(user: dict, email: str) -> str:
    """Supabase owns the credential; we still need a local row so keys,
    monitors and alerts have an owner."""
    uid = user.get("id") or _id("acct")
    with db() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE id = ?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO accounts (id, email, name, provider, created_at) "
                "VALUES (?,?,?,?,?)",
                (uid, email, (user.get("user_metadata") or {}).get("name"),
                 "supabase", _now()))
        conn.execute("UPDATE accounts SET last_seen_at = ? WHERE id = ?", (_now(), uid))
        conn.commit()
    return uid


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

def _validate(email: str, password: str) -> None:
    if not EMAIL_RE.match(email or ""):
        raise AuthError("that does not look like an email address.", 400, "bad_email")
    if len(password or "") < 8:
        raise AuthError("password must be at least 8 characters.", 400, "weak_password")


def signup(email: str, password: str, name: str = "", ip: str = "", agent: str = "") -> dict:
    email = (email or "").strip().lower()
    _validate(email, password)

    if SUPABASE_ON:
        status, body = _gotrue("POST", "/signup",
                               {"email": email, "password": password,
                                "data": {"name": name}})
        if status >= 400:
            raise _gotrue_error(status, body)

        # GoTrue answers in one of two shapes. With email confirmation off it
        # returns a session and the account is immediately usable. With it on
        # it returns only the user, and signing them in here would be a lie.
        user = body.get("user") or body
        session = body.get("session") or (body if body.get("access_token") else None)
        if not user.get("id"):
            raise AuthError("Supabase accepted the sign-up but returned no user.",
                            502, "supabase_error")

        uid = _mirror(user, email)
        if not session:
            log_event(uid, "account.created", f"{email} (awaiting confirmation)", ip, agent)
            raise AuthError(
                "Account created. Confirm your email address using the link "
                "Supabase just sent, then sign in.", 202, "confirm_email")
    else:
        pw_hash, salt = hash_password(password)
        uid = _id("acct")
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone()
            if exists:
                raise AuthError("an account with that email already exists.", 409, "email_taken")
            conn.execute(
                "INSERT INTO accounts (id, email, name, pw_hash, pw_salt, provider, created_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, email, name or email.split("@")[0], pw_hash, salt, "local", _now(), _now()))
            conn.commit()

    log_event(uid, "account.created", email, ip, agent)
    # No key is minted here on purpose: a secret is only ever shown at the
    # moment it is created, and that moment belongs to the dashboard where the
    # owner can actually copy it.
    add_alert(uid, "info", "Welcome to Blast Radius",
              "Your account is live. Create an API key and add a monitor to "
              "start the 24-hour watch.")
    return {"account": get_account(uid)}


def login(email: str, password: str, ip: str = "", agent: str = "") -> dict:
    email = (email or "").strip().lower()
    if SUPABASE_ON:
        status, body = _gotrue("POST", "/token?grant_type=password",
                               {"email": email, "password": password})
        if status >= 400:
            log_event(None, "login.failed", email, ip, agent)
            raise _gotrue_error(status, body)

        token = body.get("access_token")
        if not token:
            raise AuthError("Supabase accepted the credentials but issued no token.",
                            502, "supabase_error")

        # Verified against the issuer rather than decoded and trusted.
        user = supabase_user(token)
        uid = _mirror(user, user.get("email") or email)
    else:
        with db() as conn:
            row = conn.execute(
                "SELECT id, pw_hash, pw_salt FROM accounts WHERE email = ?", (email,)).fetchone()
        if not row or not row["pw_hash"] or not verify_password(password, row["pw_hash"], row["pw_salt"]):
            log_event(row["id"] if row else None, "login.failed", email, ip, agent)
            raise AuthError("email or password is wrong.", 401, "bad_credentials")
        uid = row["id"]
        with db() as conn:
            conn.execute("UPDATE accounts SET last_seen_at = ? WHERE id = ?", (_now(), uid))
            conn.commit()

    log_event(uid, "login.ok", email, ip, agent)
    return {"account": get_account(uid)}


def request_password_reset(email: str, redirect_to: str = "") -> dict:
    """Supabase sends the mail. Locally there is no mailer, and inventing a
    reset flow that silently does nothing would be worse than saying so."""
    email = (email or "").strip().lower()
    if not SUPABASE_ON:
        raise AuthError(
            "password reset needs an email provider. Configure Supabase in .env, "
            "or reset the password directly in accounts.db.", 501, "not_supported")

    payload = {"email": email}
    if redirect_to:
        payload["redirect_to"] = redirect_to
    status, body = _gotrue("POST", "/recover", payload)
    if status >= 400:
        raise _gotrue_error(status, body)
    # GoTrue answers 200 whether or not the address exists, and so do we —
    # a differing response is an account-enumeration oracle.
    return {"sent": True}


def get_account(account_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT id, email, name, provider, created_at, last_seen_at "
            "FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def start_session(account_id: str, ip: str = "", agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, account_id, created_at, expires_at, ip, agent) "
            "VALUES (?,?,?,?,?,?)",
            (token, account_id, _now(), _now() + SESSION_DAYS * 86400, ip, (agent or "")[:180]))
        conn.commit()
    return token


def session_account(token: str | None) -> dict | None:
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT account_id, expires_at FROM sessions WHERE token = ?", (token,)).fetchone()
        if not row:
            return None
        if row["expires_at"] < _now():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return None
    return get_account(row["account_id"])


def end_session(token: str | None) -> None:
    if not token:
        return
    with db() as conn:
        row = conn.execute("SELECT account_id FROM sessions WHERE token = ?", (token,)).fetchone()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    if row:
        log_event(row["account_id"], "logout", "")


# --------------------------------------------------------------------------
# api keys
# --------------------------------------------------------------------------

def create_key(account_id: str, name: str = "New key", ip: str = "", agent: str = "") -> dict:
    """Returns the plaintext secret exactly once. It is not recoverable."""
    secret = KEY_PREFIX + secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:40]
    key_hash = hashlib.sha256(secret.encode()).hexdigest()
    kid = _id("key")
    prefix = secret[:len(KEY_PREFIX) + 6]

    with db() as conn:
        conn.execute(
            "INSERT INTO api_keys (id, account_id, name, prefix, key_hash, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (kid, account_id, (name or "New key")[:60], prefix, key_hash, _now()))
        conn.commit()

    log_event(account_id, "key.created", f"{name} ({prefix}…)", ip, agent, kid)
    return {"id": kid, "name": name, "prefix": prefix, "secret": secret,
            "created_at": _now()}


def list_keys(account_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, prefix, created_at, last_used_at, calls, revoked_at "
            "FROM api_keys WHERE account_id = ? ORDER BY created_at DESC",
            (account_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_key(account_id: str, key_id: str, ip: str = "", agent: str = "") -> bool:
    with db() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND account_id = ? AND revoked_at IS NULL",
            (_now(), key_id, account_id))
        conn.commit()
    if cur.rowcount:
        log_event(account_id, "key.revoked", key_id, ip, agent, key_id)
    return bool(cur.rowcount)


def resolve_key(secret: str | None) -> dict | None:
    """Look a presented key up by hash. Bumps usage counters on success."""
    if not secret:
        return None
    key_hash = hashlib.sha256(secret.strip().encode()).hexdigest()
    with db() as conn:
        row = conn.execute(
            "SELECT id, account_id, name, prefix, revoked_at FROM api_keys WHERE key_hash = ?",
            (key_hash,)).fetchone()
        if not row or row["revoked_at"]:
            return None
        conn.execute(
            "UPDATE api_keys SET last_used_at = ?, calls = calls + 1 WHERE id = ?",
            (_now(), row["id"]))
        conn.commit()
    return dict(row)


def key_usage(account_id: str) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS keys, COALESCE(SUM(calls),0) AS calls "
            "FROM api_keys WHERE account_id = ? AND revoked_at IS NULL",
            (account_id,)).fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# monitors + alerts
# --------------------------------------------------------------------------

def add_monitor(account_id: str, package: str, label: str = "",
                ip: str = "", agent: str = "") -> dict:
    package = (package or "").strip()
    if not package:
        raise AuthError("a monitor needs a package name.", 400, "no_package")
    mid = _id("mon")
    with db() as conn:
        dupe = conn.execute(
            "SELECT id FROM monitors WHERE account_id = ? AND package = ? AND active = 1",
            (account_id, package)).fetchone()
        if dupe:
            raise AuthError(f"{package} is already monitored.", 409, "duplicate")
        conn.execute(
            "INSERT INTO monitors (id, account_id, package, label, created_at) "
            "VALUES (?,?,?,?,?)", (mid, account_id, package, label or "", _now()))
        conn.commit()
    log_event(account_id, "monitor.added", package, ip, agent)
    return get_monitor(mid)


def get_monitor(monitor_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
    return dict(row) if row else None


def list_monitors(account_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM monitors WHERE account_id = ? AND active = 1 "
            "ORDER BY created_at DESC", (account_id,)).fetchall()
    return [dict(r) for r in rows]


def remove_monitor(account_id: str, monitor_id: str, ip: str = "", agent: str = "") -> bool:
    with db() as conn:
        cur = conn.execute(
            "UPDATE monitors SET active = 0 WHERE id = ? AND account_id = ?",
            (monitor_id, account_id))
        conn.commit()
    if cur.rowcount:
        log_event(account_id, "monitor.removed", monitor_id, ip, agent)
    return bool(cur.rowcount)


# --------------------------------------------------------------------------
# webhooks
# --------------------------------------------------------------------------

def add_webhook(account_id: str, url: str, label: str = "",
                ip: str = "", agent: str = "") -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise AuthError("a webhook URL has to start with http:// or https://.",
                        400, "bad_url")
    wid = _id("wh")
    secret = "whsec_" + secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "INSERT INTO webhooks (id, account_id, url, secret, label, created_at) "
            "VALUES (?,?,?,?,?,?)", (wid, account_id, url, secret, label or "", _now()))
        conn.commit()
    log_event(account_id, "webhook.added", url[:120], ip, agent)
    return get_webhook(wid)


def get_webhook(webhook_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    return dict(row) if row else None


def list_webhooks(account_id: str, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM webhooks WHERE account_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY created_at DESC"
    with db() as conn:
        rows = conn.execute(sql, (account_id,)).fetchall()
    return [dict(r) for r in rows]


def remove_webhook(account_id: str, webhook_id: str, ip: str = "", agent: str = "") -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM webhooks WHERE id = ? AND account_id = ?",
                           (webhook_id, account_id))
        conn.commit()
    if cur.rowcount:
        log_event(account_id, "webhook.removed", webhook_id, ip, agent)
    return bool(cur.rowcount)


def record_delivery(webhook_id: str, ok: bool, detail: str) -> None:
    """An endpoint that has failed this many times in a row is parked rather
    than retried forever — see notify.DISABLE_AFTER."""
    import notify
    with db() as conn:
        conn.execute(
            "UPDATE webhooks SET last_at = ?, last_ok = ?, last_detail = ?, "
            "deliveries = deliveries + 1, "
            "failures = failures + ?, "
            "consecutive = CASE WHEN ? THEN 0 ELSE consecutive + 1 END "
            "WHERE id = ?",
            (_now(), 1 if ok else 0, detail[:200], 0 if ok else 1, 1 if ok else 0, webhook_id))
        conn.execute(
            "UPDATE webhooks SET active = 0 WHERE id = ? AND consecutive >= ?",
            (webhook_id, notify.DISABLE_AFTER))
        conn.commit()


def add_alert(account_id: str, level: str, title: str, detail: str = "",
              data: dict | None = None, monitor_id: str | None = None) -> dict:
    aid = _id("alert")
    at = _now()
    with db() as conn:
        conn.execute(
            "INSERT INTO alerts (id, account_id, monitor_id, level, title, detail, data, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (aid, account_id, monitor_id, level, title, detail,
             json.dumps(data or {}), at))
        conn.commit()
    alert = {"id": aid, "account_id": account_id, "monitor_id": monitor_id,
             "level": level, "title": title, "detail": detail,
             "data": data or {}, "created_at": at, "read_at": None}
    _publish(account_id, alert)

    # …and out of the building: webhooks always, email for the loud ones.
    # Never allowed to fail the caller — the traversal that found the problem
    # must not be undone by a receiver that is down.
    try:
        import notify
        acct = get_account(account_id) or {}
        notify.dispatch(account_id, alert, acct.get("email"))
    except Exception:
        pass

    return alert


def list_alerts(account_id: str, limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, monitor_id, level, title, detail, data, created_at, read_at "
            "FROM alerts WHERE account_id = ? ORDER BY created_at DESC LIMIT ?",
            (account_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data"] = json.loads(d.get("data") or "{}")
        except Exception:
            d["data"] = {}
        out.append(d)
    return out


def mark_alerts_read(account_id: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "UPDATE alerts SET read_at = ? WHERE account_id = ? AND read_at IS NULL",
            (_now(), account_id))
        conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# realtime fan-out
# --------------------------------------------------------------------------
# Dashboards subscribe per account. The monitor worker and the alert writer
# push into every queue belonging to that account; a slow reader is dropped
# rather than allowed to back the worker up.

import queue as _queue

_subs: dict[str, list["_queue.Queue"]] = {}
_subs_lock = threading.Lock()


def subscribe(account_id: str) -> "_queue.Queue":
    q = _queue.Queue(maxsize=64)
    with _subs_lock:
        _subs.setdefault(account_id, []).append(q)
    return q


def unsubscribe(account_id: str, q: "_queue.Queue") -> None:
    with _subs_lock:
        lst = _subs.get(account_id) or []
        if q in lst:
            lst.remove(q)
        if not lst:
            _subs.pop(account_id, None)


def _publish(account_id: str, alert: dict) -> None:
    with _subs_lock:
        targets = list(_subs.get(account_id) or [])
    for q in targets:
        try:
            q.put_nowait({"type": "alert", "alert": alert})
        except _queue.Full:
            pass


def publish_state(account_id: str, payload: dict) -> None:
    with _subs_lock:
        targets = list(_subs.get(account_id) or [])
    for q in targets:
        try:
            q.put_nowait(payload)
        except _queue.Full:
            pass


# --------------------------------------------------------------------------
# the 24-hour watch
# --------------------------------------------------------------------------

MONITOR_INTERVAL = config.MONITOR_INTERVAL
MONITOR_STALE = config.MONITOR_STALE

_worker_started = False


def _classify(total: int) -> str:
    if total >= 1000:
        return "critical"
    if total >= 100:
        return "high"
    if total >= 10:
        return "notable"
    return "info"


def check_monitor(monitor: dict, measure) -> dict | None:
    """Run one monitor. `measure(package)` returns the blast payload or raises.

    Emits an alert when the exposure count moves, when a monitor first
    resolves, or when a package the account watches stops being answerable.
    """
    mid, account_id, pkg = monitor["id"], monitor["account_id"], monitor["package"]
    at = _now()
    try:
        result = measure(pkg)
        total = int(result.get("total") or 0)
        status = "ok"
    except Exception as exc:
        with db() as conn:
            conn.execute(
                "UPDATE monitors SET last_check_at = ?, last_status = ?, checks = checks + 1 "
                "WHERE id = ?", (at, "error", mid))
            conn.commit()
        if (monitor.get("last_status") or "") != "error":
            return add_alert(account_id, "notable", f"{pkg} could not be measured",
                             f"{exc.__class__.__name__}: {exc}"[:200],
                             {"package": pkg}, mid)
        return None

    previous = monitor.get("last_total")
    with db() as conn:
        conn.execute(
            "UPDATE monitors SET last_check_at = ?, last_total = ?, last_status = ?, "
            "checks = checks + 1 WHERE id = ?", (at, total, status, mid))
        conn.commit()

    publish_state(account_id, {"type": "monitor", "monitor_id": mid,
                               "package": pkg, "total": total, "at": at})

    if previous is None:
        return add_alert(account_id, "info", f"Monitoring {pkg}",
                         f"Baseline set at {total:,} packages transitively exposed.",
                         {"package": pkg, "total": total}, mid)

    if total != previous:
        delta = total - previous
        direction = "grew" if delta > 0 else "shrank"
        level = _classify(abs(delta)) if delta > 0 else "info"
        return add_alert(
            account_id, level,
            f"{pkg} blast radius {direction} by {abs(delta):,}",
            f"Now {total:,} packages transitively exposed, was {previous:,}.",
            {"package": pkg, "total": total, "previous": previous, "delta": delta}, mid)

    return None


def due_monitors() -> list[dict]:
    cutoff = _now() - MONITOR_STALE
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM monitors WHERE active = 1 AND "
            "(last_check_at IS NULL OR last_check_at < ?) ORDER BY last_check_at IS NOT NULL, "
            "last_check_at LIMIT 25", (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def start_worker(measure, log=None) -> None:
    """Spawn the watch loop. `measure` is injected so this module never has to
    import the graph client — which keeps it testable and keeps the import
    graph one-directional."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def loop():
        while True:
            try:
                for monitor in due_monitors():
                    try:
                        check_monitor(monitor, measure)
                    except Exception as exc:
                        if log:
                            log("monitor_failed", monitor=monitor.get("package"),
                                error=f"{exc.__class__.__name__}: {exc}"[:200])
                    time.sleep(0.4)          # be kind to the graph
            except Exception as exc:
                if log:
                    log("monitor_loop_error", error=f"{exc.__class__.__name__}: {exc}"[:200])
            time.sleep(MONITOR_INTERVAL)

    threading.Thread(target=loop, daemon=True, name="monitors").start()


def stats() -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM accounts) AS accounts, "
            "       (SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL) AS keys, "
            "       (SELECT COUNT(*) FROM monitors WHERE active = 1) AS monitors, "
            "       (SELECT COUNT(*) FROM alerts) AS alerts").fetchone()
    with db() as conn:
        hooks = conn.execute(
            "SELECT COUNT(*) AS n FROM webhooks WHERE active = 1").fetchone()["n"]
    return {**dict(row), "webhooks": hooks,
            "auth_provider": "supabase" if SUPABASE_ON else "local",
            "monitor_interval_s": MONITOR_INTERVAL,
            "monitor_stale_s": MONITOR_STALE,
            "worker_running": _worker_started}
