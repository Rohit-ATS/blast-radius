"""The platform layer: config, keys, the watch, webhook signing and delivery.

These run against a temporary accounts database, so they never touch the real
one and can assert on exact counts. The graph is not needed: `check_monitor`
takes its measurement function as an argument precisely so the watch can be
tested without a database behind it.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@pytest.fixture()
def acc(tmp_path, monkeypatch):
    """A fresh accounts module bound to a throwaway database.

    Supabase is cleared explicitly: these assert the local credential path, and
    a developer with real credentials in their environment (or another test
    that set them) would otherwise send this suite's fake sign-ups to a live
    project.
    """
    monkeypatch.setenv("ACCOUNTS_DB", str(tmp_path / "accounts.db"))
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.setenv("BLAST_ENV_FILE", str(tmp_path / "absent.env"))

    import config
    importlib.reload(config)
    import accounts
    importlib.reload(accounts)
    accounts._ready = False
    assert not accounts.SUPABASE_ON, "these tests must exercise the local path"

    yield accounts

    importlib.reload(config)
    importlib.reload(accounts)


# ---------------------------------------------------------------- config

def test_env_file_is_parsed_and_real_env_wins(tmp_path, monkeypatch):
    # Names are prefixed so this cannot collide with a real .env the developer
    # actually has. An earlier version asserted on SUPABASE_URL and passed only
    # in isolation: once another test had imported config with a real .env
    # present, os.environ already held the real value and setdefault correctly
    # refused to overwrite it — which is the very behaviour under test.
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "BRTEST_PLAIN=https://example.supabase.co\n"
        'export BRTEST_QUOTED="with spaces"\n'
        "BRTEST_SINGLE='single quoted'\n"
        "BRTEST_TRAILING=value # not part of it\n"
        "BRTEST_ALREADY_SET=from_file\n"
        "a line that is not an assignment\n"
        "\n", encoding="utf-8")

    for name in ("BRTEST_PLAIN", "BRTEST_QUOTED", "BRTEST_SINGLE", "BRTEST_TRAILING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BRTEST_ALREADY_SET", "from_environment")
    monkeypatch.setenv("BLAST_ENV_FILE", str(env))

    import config
    importlib.reload(config)

    assert os.environ["BRTEST_PLAIN"] == "https://example.supabase.co"
    assert os.environ["BRTEST_QUOTED"] == "with spaces"
    assert os.environ["BRTEST_SINGLE"] == "single quoted"
    assert os.environ["BRTEST_TRAILING"] == "value"
    # a value already in the environment is never overwritten by the file
    assert os.environ["BRTEST_ALREADY_SET"] == "from_environment"
    # a malformed line is skipped rather than being fatal
    assert len(config.LOADED) == 5


def test_describe_never_leaks_a_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "super-secret-anon-key-value")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    import config
    importlib.reload(config)

    local = config.describe()
    assert "super-secret-anon-key-value" not in json.dumps(local)
    assert "…" in local["supabase_anon_key"], "the anon key was not redacted"

    # The version served over HTTP says whether auth is configured and nothing
    # more — not the project URL, not which variables are set, not the path of
    # the file they came from.
    public = config.describe(public=True)
    blob = json.dumps(public)
    assert "super-secret-anon-key-value" not in blob
    assert "example.supabase.co" not in blob
    assert public["supabase_configured"] is True
    assert "env_keys_loaded" not in public and "env_file" not in public


# ------------------------------------------------------------------ keys

def test_key_is_stored_only_as_a_digest(acc):
    out = acc.signup("k@example.com", "hunter2hunter2")
    account_id = out["account"]["id"]
    key = acc.create_key(account_id, "CI")
    secret = key["secret"]

    with acc.db() as conn:
        rows = conn.execute("SELECT key_hash, prefix FROM api_keys").fetchall()
    assert len(rows) == 1
    # the plaintext must appear nowhere in the row
    assert secret not in rows[0]["key_hash"]
    assert rows[0]["prefix"] in secret
    assert len(rows[0]["key_hash"]) == 64          # sha256 hex

    assert acc.resolve_key(secret)["account_id"] == account_id
    assert acc.resolve_key(secret + "x") is None
    assert acc.resolve_key("") is None


def test_revocation_takes_effect_immediately(acc):
    account_id = acc.signup("r@example.com", "hunter2hunter2")["account"]["id"]
    key = acc.create_key(account_id, "temp")
    assert acc.resolve_key(key["secret"])
    assert acc.revoke_key(account_id, key["id"])
    assert acc.resolve_key(key["secret"]) is None
    # revoking twice is not an error but reports nothing changed
    assert not acc.revoke_key(account_id, key["id"])


def test_a_key_belongs_to_exactly_one_account(acc):
    a = acc.signup("a@example.com", "hunter2hunter2")["account"]["id"]
    b = acc.signup("b@example.com", "hunter2hunter2")["account"]["id"]
    key = acc.create_key(a, "a's key")
    assert not acc.revoke_key(b, key["id"]), "another account revoked a key it does not own"
    assert acc.resolve_key(key["secret"])["account_id"] == a


def test_passwords_are_salted_and_verified(acc):
    h1, s1 = acc.hash_password("correct horse battery staple")
    h2, s2 = acc.hash_password("correct horse battery staple")
    assert s1 != s2 and h1 != h2, "identical passwords produced identical hashes"
    assert acc.verify_password("correct horse battery staple", h1, s1)
    assert not acc.verify_password("wrong", h1, s1)


def test_signup_rejects_weak_input(acc):
    with pytest.raises(acc.AuthError) as e:
        acc.signup("not-an-email", "hunter2hunter2")
    assert e.value.code == "bad_email"

    with pytest.raises(acc.AuthError) as e:
        acc.signup("short@example.com", "abc")
    assert e.value.code == "weak_password"

    acc.signup("dupe@example.com", "hunter2hunter2")
    with pytest.raises(acc.AuthError) as e:
        acc.signup("dupe@example.com", "hunter2hunter2")
    assert e.value.status == 409


# --------------------------------------------------------------- the watch

def test_monitor_baseline_then_delta(acc):
    account_id = acc.signup("m@example.com", "hunter2hunter2")["account"]["id"]
    monitor = acc.add_monitor(account_id, "debug")

    # first observation is a baseline, not a change
    first = acc.check_monitor(acc.get_monitor(monitor["id"]), lambda p: {"total": 100})
    assert first["level"] == "info" and "Monitoring" in first["title"]

    # an unchanged measurement raises nothing at all
    assert acc.check_monitor(acc.get_monitor(monitor["id"]), lambda p: {"total": 100}) is None

    # growth raises an alert carrying both numbers
    grew = acc.check_monitor(acc.get_monitor(monitor["id"]), lambda p: {"total": 1500})
    assert grew["data"]["previous"] == 100
    assert grew["data"]["total"] == 1500
    assert grew["data"]["delta"] == 1400
    assert grew["level"] == "critical"          # a 1400-package jump

    shrank = acc.check_monitor(acc.get_monitor(monitor["id"]), lambda p: {"total": 1400})
    assert shrank["data"]["delta"] == -100
    assert shrank["level"] == "info", "a shrinking blast radius is not an emergency"


def test_monitor_failure_alerts_once_not_every_sweep(acc):
    account_id = acc.signup("f@example.com", "hunter2hunter2")["account"]["id"]
    monitor = acc.add_monitor(account_id, "never-crawled")

    def boom(_pkg):
        raise RuntimeError("not in the crawled graph yet")

    first = acc.check_monitor(acc.get_monitor(monitor["id"]), boom)
    assert first and "could not be measured" in first["title"]
    # still broken on the next sweep — but it must not alert again
    assert acc.check_monitor(acc.get_monitor(monitor["id"]), boom) is None


def test_monitors_are_scoped_to_their_account(acc):
    a = acc.signup("one@example.com", "hunter2hunter2")["account"]["id"]
    b = acc.signup("two@example.com", "hunter2hunter2")["account"]["id"]
    m = acc.add_monitor(a, "debug")
    assert not acc.remove_monitor(b, m["id"])
    assert len(acc.list_monitors(a)) == 1
    assert len(acc.list_monitors(b)) == 0


def test_due_monitors_respects_the_stale_window(acc):
    account_id = acc.signup("d@example.com", "hunter2hunter2")["account"]["id"]
    acc.add_monitor(account_id, "fresh")
    assert len(acc.due_monitors()) == 1, "a never-checked monitor is due immediately"

    acc.check_monitor(acc.get_monitor(acc.list_monitors(account_id)[0]["id"]),
                      lambda p: {"total": 1})
    assert acc.due_monitors() == [], "a just-checked monitor is not due again yet"


# ---------------------------------------------------------------- webhooks

def test_signature_round_trips_and_rejects_tampering():
    import notify
    body = b'{"type":"alert","level":"high"}'
    ts = str(int(time.time()))
    header = notify.sign("whsec_test", body, ts)

    assert notify.verify("whsec_test", body, header)
    assert not notify.verify("whsec_test", b'{"type":"alert","level":"info"}', header)
    assert not notify.verify("whsec_other", body, header)
    assert not notify.verify("whsec_test", body, "garbage")


def test_old_signatures_are_rejected():
    import notify
    body = b"{}"
    stale = notify.sign("whsec_test", body, str(int(time.time()) - 4000))
    assert not notify.verify("whsec_test", body, stale), "a replayed payload was accepted"
    assert notify.verify("whsec_test", body, stale, tolerance=0), "tolerance=0 should skip the check"


def test_webhook_rejects_a_non_http_url(acc):
    account_id = acc.signup("w@example.com", "hunter2hunter2")["account"]["id"]
    with pytest.raises(acc.AuthError) as e:
        acc.add_webhook(account_id, "ftp://example.com/hook")
    assert e.value.code == "bad_url"


def test_repeated_failures_disable_an_endpoint(acc):
    import notify
    account_id = acc.signup("x@example.com", "hunter2hunter2")["account"]["id"]
    hook = acc.add_webhook(account_id, "https://example.invalid/hook")

    for _ in range(notify.DISABLE_AFTER):
        acc.record_delivery(hook["id"], False, "connection refused")

    assert acc.get_webhook(hook["id"])["active"] == 0
    assert acc.list_webhooks(account_id, active_only=True) == []

    # a success resets the streak
    acc.record_delivery(hook["id"], True, "200")
    assert acc.get_webhook(hook["id"])["consecutive"] == 0


class _Receiver(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Receiver.received.append({
            "body": raw,
            "signature": self.headers.get("X-BlastRadius-Signature", ""),
            "event": self.headers.get("X-BlastRadius-Event"),
        })
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def test_a_real_delivery_arrives_signed():
    """The delivery path end to end, over a real socket, verified the way an
    integrator's receiver would verify it."""
    import notify

    _Receiver.received = []
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    payload = {"type": "alert", "id": "alert_test", "level": "high",
               "title": "test", "created_at": time.time()}
    ok, detail = notify._post(f"http://127.0.0.1:{port}/hook", "whsec_abc", payload)
    server.shutdown()

    assert ok, detail
    assert len(_Receiver.received) == 1
    got = _Receiver.received[0]
    assert got["event"] == "alert"
    assert notify.verify("whsec_abc", got["body"], got["signature"])
    assert json.loads(got["body"])["title"] == "test"


def test_delivery_failure_is_reported_not_raised():
    import notify
    ok, detail = notify._post("http://127.0.0.1:9/nothing-listens-here", "s", {})
    assert ok is False
    assert detail                     # says what went wrong rather than raising


# ------------------------------------------------------------- audit trail

def test_every_sensitive_action_is_logged(acc):
    account_id = acc.signup("log@example.com", "hunter2hunter2")["account"]["id"]
    key = acc.create_key(account_id, "CI")
    acc.revoke_key(account_id, key["id"])
    acc.add_monitor(account_id, "debug")

    events = [e["event"] for e in acc.security_log(account_id)]
    for expected in ("account.created", "key.created", "key.revoked", "monitor.added"):
        assert expected in events, f"{expected} was not written to the security log"


def test_the_log_is_scoped_to_one_account(acc):
    a = acc.signup("p@example.com", "hunter2hunter2")["account"]["id"]
    b = acc.signup("q@example.com", "hunter2hunter2")["account"]["id"]
    acc.create_key(a, "a")
    assert all("q@example.com" not in (e["detail"] or "") for e in acc.security_log(a))
    assert len(acc.security_log(b)) == 1        # only its own account.created


def test_sessions_expire(acc):
    account_id = acc.signup("s@example.com", "hunter2hunter2")["account"]["id"]
    token = acc.start_session(account_id)
    assert acc.session_account(token)["id"] == account_id

    with acc.db() as conn:
        conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
                     (time.time() - 1, token))
        conn.commit()
    assert acc.session_account(token) is None, "an expired session still authenticated"
    assert acc.session_account("nonsense") is None
