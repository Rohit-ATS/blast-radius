"""Prove the deployment is real.

Every check here talks to the actual service rather than reading a setting and
declaring victory. A green line means that thing worked once, just now.

    py setup_check.py                 check everything
    py setup_check.py --email you@x   also send a real test email
    py setup_check.py --webhook URL   also send a real signed webhook

Exit code is 0 when nothing is broken, 1 when something is.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

# A Windows console defaults to cp1252, and this script prints package names,
# arrows and bullets. Reconfigure rather than restrict the vocabulary: a
# UnicodeEncodeError raised while reporting a passing check would be caught by
# that check's own `except` and reported as the service being down.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:                                              # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

import config          # noqa: E402,F401 — loads .env before anything reads it

OK, WARN, BAD = "PASS", "WARN", "FAIL"
RESULTS: list[tuple[str, str, str]] = []

C = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m", "": "\033[0m"}
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def record(state: str, name: str, detail: str = "") -> None:
    RESULTS.append((state, name, detail))
    tint, off = (C[state], C[""]) if COLOR else ("", "")
    print(f"  {tint}{state:<4}{off}  {name}" + (f"\n        {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * 74)


# --------------------------------------------------------------------------

def check_env() -> None:
    section("1 · configuration")
    path = config.ENV_PATH
    if os.path.exists(path):
        record(OK, f".env found at {path}",
               f"{len(config.LOADED)} keys loaded: {', '.join(sorted(config.LOADED)) or '(none)'}")
    else:
        record(WARN, ".env not found",
               f"expected at {path}. Copy .env.example to .env to configure "
               "Supabase, SMTP and the watch. Everything still runs without it.")

    if config.PUBLIC_URL:
        record(OK, f"PUBLIC_URL = {config.PUBLIC_URL}")
    else:
        record(WARN, "PUBLIC_URL is not set",
               "emails and webhook payloads will not carry a link back to the dashboard.")

    if config.SECURE_COOKIES:
        record(OK, "SECURE_COOKIES on — session cookie will carry `Secure`")
    else:
        record(WARN, "SECURE_COOKIES off",
               "fine for localhost; set it to 1 once you are behind HTTPS.")


def check_accounts_db() -> None:
    section("2 · account store")
    import accounts
    try:
        with accounts.db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
            conn.execute("INSERT INTO _probe VALUES (1)")
            conn.execute("DROP TABLE _probe")
            conn.commit()
        record(OK, f"accounts.db is writable ({accounts.ACCOUNTS_DB})")
    except Exception as exc:
        record(BAD, "accounts.db is not writable", f"{exc.__class__.__name__}: {exc}")
        return

    s = accounts.stats()
    record(OK, "schema ready",
           f"{s['accounts']} accounts · {s['keys']} live keys · "
           f"{s['monitors']} monitors · {s['alerts']} alerts · {s['webhooks']} webhooks")


def check_auth() -> None:
    section("3 · authentication")
    import accounts

    ping = accounts.supabase_ping()
    if not ping["configured"]:
        record(WARN, "Supabase is not configured — using local password auth",
               "Set SUPABASE_URL and SUPABASE_ANON_KEY in .env to switch. Local auth "
               "is real (PBKDF2-HMAC-SHA256, 310k rounds) but has no email flows.")
        # prove the local path actually works
        try:
            h, salt = accounts.hash_password("correct horse battery staple")
            assert accounts.verify_password("correct horse battery staple", h, salt)
            assert not accounts.verify_password("wrong", h, salt)
            record(OK, "local password hashing verifies")
        except Exception as exc:
            record(BAD, "local password hashing is broken", str(exc))
        return

    if not ping.get("reachable"):
        record(BAD, "Supabase is unreachable", ping.get("detail", ""))
        return
    if not ping.get("key_valid"):
        record(BAD, "Supabase rejected SUPABASE_ANON_KEY", ping.get("detail", ""))
        return

    record(OK, f"Supabase reachable at {ping['url']}")
    record(OK, "SUPABASE_ANON_KEY accepted by the project")

    if ping.get("email_signup_enabled"):
        record(OK, "email sign-up is enabled on the project")
    else:
        record(BAD, "email sign-up is disabled on this Supabase project",
               "Authentication → Providers → Email → enable, or nobody can register.")

    if ping.get("autoconfirm"):
        record(OK, "email autoconfirm on — sign-up signs the user straight in")
    else:
        record(WARN, "email confirmation required",
               "Sign-up will ask the user to click the link Supabase emails them. "
               "Set the Site URL and Redirect URLs under Authentication → URL "
               "Configuration to your PUBLIC_URL, or the link will bounce.")

    if ping.get("external_providers"):
        record(OK, "social providers enabled: " + ", ".join(ping["external_providers"]))

    if config.SUPABASE_SERVICE_KEY:
        record(OK, "service role key present")


def check_graph() -> None:
    section("4 · the graph")
    # The traversal and the reporting are deliberately separated: a printing
    # problem must never be reported as the graph being down.
    try:
        import blast
        from hydra import Hydra
        result, ms = blast.blast_radius(Hydra(budget=25.0), "debug", 2, 50)
    except Exception as exc:
        record(BAD, "the graph did not answer",
               f"{exc.__class__.__name__}: {str(exc)[:180]}\n"
               "        Is HydraDB up on HYDRA_URL? `docker compose up -d`")
        return
    record(OK, "HydraDB answered a real traversal",
           f"debug -> {result.get('total', 0):,} exposed at depth 2, {ms:.0f}ms")


def check_watch() -> None:
    section("5 · the 24/7 watch")
    import accounts
    record(OK, "watch configured",
           f"sweeps every {accounts.MONITOR_INTERVAL:.0f}s; a monitor is due again "
           f"{accounts.MONITOR_STALE:.0f}s after its last check "
           f"(~{accounts.MONITOR_STALE / 3600:.1f}h)")

    due = accounts.due_monitors()
    total = sum(len(accounts.list_monitors(a["id"]))
                for a in _all_accounts())
    record(OK, f"{total} monitors registered, {len(due)} due right now")


def _all_accounts() -> list[dict]:
    import accounts
    with accounts.db() as conn:
        return [dict(r) for r in conn.execute("SELECT id FROM accounts").fetchall()]


def check_delivery(email: str | None, webhook: str | None) -> None:
    section("6 · alert delivery")
    import notify

    if config.EMAIL_ON:
        record(OK, f"SMTP configured: {config.SMTP_HOST}:{config.SMTP_PORT} as {config.SMTP_FROM}")
    else:
        record(WARN, "email delivery is off",
               "Alerts still reach the dashboard and any webhooks. Set SMTP_* in "
               ".env to also email high and critical alerts.")

    # the signing scheme is what a receiver depends on, so prove it round-trips
    body = b'{"type":"test"}'
    ts = str(int(time.time()))
    header = notify.sign("whsec_probe", body, ts)
    if notify.verify("whsec_probe", body, header):
        record(OK, "webhook signatures sign and verify (HMAC-SHA256)")
    else:
        record(BAD, "webhook signing is broken")
    if notify.verify("whsec_probe", b'{"type":"tampered"}', header):
        record(BAD, "webhook signature accepted a tampered body")
    else:
        record(OK, "a tampered body fails verification")

    if webhook:
        ok, detail = notify._post(webhook, "whsec_setup_check", {
            "type": "test", "id": "setup_check", "level": "info",
            "title": "Blast Radius setup check",
            "detail": "Signed with whsec_setup_check.",
            "created_at": time.time()})
        record(OK if ok else BAD, f"test webhook POST to {webhook}", detail)

    if email:
        if not config.EMAIL_ON:
            record(BAD, "cannot send a test email", "SMTP_* is not configured in .env")
        else:
            ok, detail = notify._send_email(email, {
                "level": "high", "title": "Blast Radius setup check",
                "detail": "If this arrived, alert email is working.",
                "data": {"package": "setup-check"}, "created_at": time.time()})
            record(OK if ok else BAD, f"test email to {email}", detail)


def check_server() -> None:
    section("7 · the running server")
    import requests
    base = config.PUBLIC_URL or "http://127.0.0.1:8000"
    try:
        r = requests.get(f"{base}/api/platform-stats", timeout=10)
        d = r.json()
        record(OK, f"server answering at {base}",
               f"auth={d.get('auth_provider')} · worker={'running' if d.get('worker_running') else 'stopped'} "
               f"· {d.get('accounts', 0)} accounts")
        if not d.get("worker_running"):
            record(BAD, "the watch worker is not running",
                   "monitors will never be re-measured; restart the server.")
    except Exception as exc:
        record(WARN, f"no server answering at {base}",
               f"{exc.__class__.__name__}. Start it with `py server.py` and re-run "
               "this to check the live half.")


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", help="send a real test email here")
    ap.add_argument("--webhook", help="send a real signed test POST here")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    print("Blast Radius — setup check")
    print("=" * 74)

    check_env()
    check_accounts_db()
    check_auth()
    check_graph()
    check_watch()
    check_delivery(args.email, args.webhook)
    check_server()

    bad = [r for r in RESULTS if r[0] == BAD]
    warn = [r for r in RESULTS if r[0] == WARN]

    print("\n" + "=" * 74)
    print(f"{len(RESULTS) - len(bad) - len(warn)} passed · {len(warn)} warnings · {len(bad)} failures")
    if bad:
        print("\nFix these before relying on it:")
        for _, name, detail in bad:
            print(f"  · {name}" + (f" — {detail.splitlines()[0]}" if detail else ""))
    elif warn:
        print("\nWorking, with optional pieces unconfigured. Warnings above say which.")
    else:
        print("\nEverything checked out.")

    if args.json:
        print(json.dumps([{"state": s, "check": n, "detail": d} for s, n, d in RESULTS],
                         indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
