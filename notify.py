"""Getting an alert out of the building.

The dashboard is where alerts live, but a monitoring system whose only output
is a web page you have to be looking at is not monitoring anything. This
delivers the same alert to endpoints that reach a human at 3am:

* **Webhooks** — a signed POST to a URL the account owns. Signed with a
  per-endpoint secret using HMAC-SHA256 over the raw body, in the same shape
  Stripe and GitHub use, so a receiver can verify it came from us and was not
  replayed.
* **Email** — plain SMTP, if the deployment configured a mail account.

Delivery happens on a worker thread with bounded retries. A failing endpoint
is recorded, counted and eventually disabled rather than retried forever: an
alert queue that grows without limit because someone's staging server is down
is an outage of its own.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import smtplib
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr

import config

# Bounded on purpose. If deliveries back up this far the receiving side is
# down, and dropping the overflow with a log line beats unbounded memory.
_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=500)
_started = False

RETRIES = 3
BACKOFF = (0, 5, 20)          # seconds before attempt 1, 2, 3
DISABLE_AFTER = 20            # consecutive failures before an endpoint is parked

_log = None                   # injected by start()


def _note(event: str, **fields) -> None:
    if _log:
        _log(event, **fields)


# --------------------------------------------------------------------------
# signing
# --------------------------------------------------------------------------

def sign(secret: str, body: bytes, timestamp: str) -> str:
    """`t=<unix>,v1=<hex>` over `<timestamp>.<body>`.

    The timestamp is inside the signed material so a captured payload cannot be
    replayed later against a receiver that checks freshness.
    """
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    """Reference implementation of what a receiver should do. Used by the
    tests, and worth reading if you are writing the other end."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, got = parts["t"], parts["v1"]
    except Exception:
        return False
    if tolerance and abs(time.time() - int(ts)) > tolerance:
        return False
    want = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, got)


# --------------------------------------------------------------------------
# webhook delivery
# --------------------------------------------------------------------------

def _post(url: str, secret: str, payload: dict) -> tuple[bool, str]:
    import requests

    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "BlastRadius-Webhook/1",
        "X-BlastRadius-Signature": sign(secret, body, timestamp),
        "X-BlastRadius-Event": payload.get("type", "alert"),
        "X-BlastRadius-Delivery": payload.get("id", ""),
    }
    try:
        res = requests.post(url, data=body, headers=headers,
                            timeout=config.WEBHOOK_TIMEOUT)
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"[:160]

    if 200 <= res.status_code < 300:
        return True, f"{res.status_code}"
    return False, f"HTTP {res.status_code}: {res.text[:120]}"


def _deliver_webhooks(account_id: str, payload: dict) -> None:
    import accounts

    for hook in accounts.list_webhooks(account_id, active_only=True):
        ok, detail = False, "not attempted"
        for attempt, wait in enumerate(BACKOFF[:RETRIES]):
            if wait:
                time.sleep(wait)
            ok, detail = _post(hook["url"], hook["secret"], payload)
            if ok:
                break
        accounts.record_delivery(hook["id"], ok, detail)
        _note("webhook_delivery", url=hook["url"][:80], ok=ok, detail=detail[:120])


# --------------------------------------------------------------------------
# email delivery
# --------------------------------------------------------------------------

def _send_email(to: str, alert: dict) -> tuple[bool, str]:
    if not config.EMAIL_ON:
        return False, "smtp not configured"

    base = config.PUBLIC_URL or ""
    level = (alert.get("level") or "info").upper()
    msg = EmailMessage()
    msg["Subject"] = f"[Blast Radius · {level}] {alert.get('title', 'Alert')}"
    msg["From"] = formataddr(("Blast Radius", config.SMTP_FROM))
    msg["To"] = to
    msg.set_content(
        f"{alert.get('title', '')}\n\n"
        f"{alert.get('detail', '')}\n\n"
        f"Level:   {level}\n"
        f"Package: {(alert.get('data') or {}).get('package', '—')}\n"
        f"Raised:  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(alert.get('created_at', time.time())))}\n\n"
        f"{base + '/dashboard#alerts' if base else 'Open your dashboard for the full history.'}\n\n"
        "— Blast Radius, watching your dependency graph.\n")

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            if config.SMTP_TLS:
                smtp.starttls()
                smtp.ehlo()
            if config.SMTP_USER:
                smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True, "sent"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"[:160]


# --------------------------------------------------------------------------
# the worker
# --------------------------------------------------------------------------

def dispatch(account_id: str, alert: dict, email: str | None = None) -> None:
    """Called by accounts.add_alert. Never blocks the caller and never raises
    into it — a webhook that times out must not fail the traversal that found
    the problem."""
    try:
        _QUEUE.put_nowait({"account_id": account_id, "alert": alert, "email": email})
    except queue.Full:
        _note("notify_queue_full", account=account_id)


def _loop() -> None:
    import accounts

    while True:
        job = _QUEUE.get()
        account_id, alert = job["account_id"], job["alert"]
        try:
            payload = {
                "type": "alert",
                "id": alert.get("id"),
                "level": alert.get("level"),
                "title": alert.get("title"),
                "detail": alert.get("detail"),
                "data": alert.get("data") or {},
                "created_at": alert.get("created_at"),
                "account_id": account_id,
                "dashboard_url": (config.PUBLIC_URL + "/dashboard#alerts")
                                 if config.PUBLIC_URL else None,
            }
            _deliver_webhooks(account_id, payload)

            # Email is for the alerts worth waking up for, not every baseline.
            if config.EMAIL_ON and alert.get("level") in ("high", "critical"):
                to = job.get("email")
                if to:
                    ok, detail = _send_email(to, alert)
                    accounts.log_event(account_id, "email.sent" if ok else "email.failed",
                                       f"{to}: {detail}")
                    _note("email_delivery", ok=ok, detail=detail[:120])
        except Exception as exc:
            _note("notify_failed", error=f"{exc.__class__.__name__}: {exc}"[:160])
        finally:
            _QUEUE.task_done()


def start(log=None) -> None:
    global _started, _log
    _log = log
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="notify").start()


def status() -> dict:
    return {"running": _started, "queued": _QUEUE.qsize(),
            "email": "smtp" if config.EMAIL_ON else "off",
            "webhook_timeout_s": config.WEBHOOK_TIMEOUT}
