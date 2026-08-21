"""The seams.

Every other test file proves one component. This proves they are wired to each
other: sign up, mint a key, call the public API with it, register a monitor,
have the watch measure it, have the alert reach a webhook, read it back through
three different surfaces, then revoke and confirm the door shut.

If a refactor breaks the product without breaking a unit test, it breaks here.

Requires a running server (`py server.py`). Skips cleanly if there is not one.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

BASE = os.environ.get("BLAST_BASE", "http://127.0.0.1:8000")
TIMEOUT = 60


@pytest.fixture(scope="module")
def live():
    try:
        requests.get(f"{BASE}/api/platform-stats", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(f"no server at {BASE}: {exc}")
    return BASE


@pytest.fixture(scope="module")
def account(live):
    """A throwaway account, created through the real HTTP surface."""
    s = requests.Session()
    email = f"itest-{secrets.token_hex(6)}@example.com"
    password = secrets.token_urlsafe(16)

    r = s.post(f"{BASE}/api/auth/signup",
               json={"email": email, "password": password, "name": "Integration"},
               timeout=TIMEOUT)
    if r.status_code == 202:
        # Supabase with "Confirm email" on. Correct behaviour, but the rest of
        # this file needs a session, so say why rather than failing obscurely.
        pytest.skip("this instance requires email confirmation before sign-in; "
                    "turn off Confirm email in Supabase to exercise this path")
    assert r.ok, r.text
    assert r.json()["account"]["email"] == email

    yield {"session": s, "email": email, "password": password,
           "id": r.json()["account"]["id"]}

    s.post(f"{BASE}/api/auth/logout", timeout=TIMEOUT)


@pytest.fixture(scope="module")
def api_key(account):
    r = account["session"].post(f"{BASE}/api/keys", json={"name": "integration"},
                                timeout=TIMEOUT)
    assert r.ok, r.text
    key = r.json()["key"]
    assert key["secret"].startswith("brk_live_")
    return key


def auth(key):
    return {"Authorization": f"Bearer {key['secret']}"}


# ------------------------------------------------------------------ the seams

def test_the_instance_is_live_not_simulated(live):
    """DEMO_MODE serves recorded fixtures. That is a legitimate mode, but it
    must never be mistaken for a live measurement, so this asserts which one
    the running instance is in and that the answer is self-consistent."""
    health = requests.get(f"{BASE}/api/health", timeout=TIMEOUT).json()
    demo = health.get("demo_mode")

    r = requests.get(f"{BASE}/api/blast", params={"name": "debug", "depth": 2},
                     timeout=TIMEOUT)
    assert r.ok, r.text
    body = r.json()

    if demo:
        assert body.get("demo") is True, "demo mode did not label its own response"
    else:
        assert not body.get("demo"), "a live instance served a fixture unannounced"
        assert "X-Demo-Mode" not in r.headers
        assert body["source"] in ("hydradb", "sidecar")
        assert body["latency_ms"] > 0, "a real query cannot take zero time"


def test_signup_creates_a_session_that_the_dashboard_can_use(account):
    me = account["session"].get(f"{BASE}/api/auth/me", timeout=TIMEOUT).json()
    assert me["account"]["email"] == account["email"]
    # every dashboard surface must answer for a fresh account
    for path in ("/api/keys", "/api/monitors", "/api/alerts",
                 "/api/webhooks", "/api/security-log"):
        r = account["session"].get(f"{BASE}{path}", timeout=TIMEOUT)
        assert r.ok, f"{path} -> {r.status_code} {r.text[:120]}"
        assert r.json()["ok"] is True


def test_a_key_minted_in_the_dashboard_works_on_the_public_api(api_key):
    r = requests.get(f"{BASE}/api/v1/whoami", headers=auth(api_key), timeout=TIMEOUT)
    assert r.ok, r.text
    body = r.json()
    assert body["key"]["id"] == api_key["id"]

    # A key raises the ceiling; it does not remove it. This asserted the
    # exemption until Phase 4 replaced it — a leaked key with no ceiling drains
    # the upstream quota for everyone, which makes a trusted key the more
    # dangerous case rather than the safer one.
    limits = body["limits"]
    assert limits["rate_limited"] is True
    assert limits["minute_limit"] >= 1000, "the ceiling should be generous"
    assert limits["day_limit"] >= 10_000
    assert 0 <= limits["minute_used"] <= limits["minute_limit"]


def test_the_public_api_returns_the_same_answer_as_the_console(api_key):
    """/api/v1 is a façade over the console's own handlers. If the two ever
    disagree, an integrator is acting on a different number than the operator
    is looking at."""
    console = requests.get(f"{BASE}/api/blast",
                           params={"name": "debug", "depth": 3}, timeout=TIMEOUT).json()
    public = requests.get(f"{BASE}/api/v1/blast", headers=auth(api_key),
                          params={"name": "debug", "depth": 3}, timeout=TIMEOUT).json()
    assert console["total"] == public["total"]
    assert console["depth"] == public["depth"]
    assert [h["packages"] for h in console["histogram"]] == \
           [h["packages"] for h in public["histogram"]]


def test_every_documented_endpoint_actually_routes(api_key):
    """The docs are generated from a table. This proves the table is not
    describing endpoints that do not exist."""
    docs = requests.get(f"{BASE}/api/docs.json", timeout=TIMEOUT).json()
    assert docs["endpoints"], "the reference is empty"

    for endpoint in docs["endpoints"]:
        path, method = endpoint["path"], endpoint["method"]
        if method != "GET":
            continue                      # POST endpoints need a body; covered below
        params = {}
        for name, _t, notes, _d in endpoint.get("params") or []:
            if name == "name":
                params["name"] = "debug"
            elif name == "bad_version":
                params["bad_version"] = "4.4.2"
        r = requests.get(f"{BASE}{path}", headers=auth(api_key),
                         params=params, timeout=TIMEOUT)
        assert r.status_code != 404, f"{method} {path} is documented but not routed"
        assert r.status_code != 422, \
            f"{method} {path} rejected the parameters its own docs describe: {r.text[:160]}"


def test_v1_answers_in_the_same_shape_as_the_console(api_key):
    """An integrator writes one `if (!body.ok)` check. That only works if
    successes carry `ok` too, not just failures — and the v1 router does not
    inherit the console's envelope automatically, so this guards the seam."""
    ok = requests.get(f"{BASE}/api/v1/blast", headers=auth(api_key),
                      params={"name": "debug", "depth": 2}, timeout=TIMEOUT).json()
    assert ok["ok"] is True
    assert ok["request_id"]
    assert ok["source"] in ("hydradb", "sidecar")

    bad = requests.get(f"{BASE}/api/v1/whoami",
                       headers={"Authorization": "Bearer nope"}, timeout=TIMEOUT).json()
    assert bad["ok"] is False
    assert bad["error"] and bad["message"]
    assert bad["request_id"], "a failure with no request id cannot be traced in the log"


def test_the_api_refuses_without_a_key_and_after_revocation(account):
    r = requests.get(f"{BASE}/api/v1/blast", params={"name": "debug"}, timeout=TIMEOUT)
    assert r.status_code == 401 and r.json()["error"] == "missing_key"

    r = requests.get(f"{BASE}/api/v1/blast", params={"name": "debug"},
                     headers={"Authorization": "Bearer brk_live_not_a_real_key"},
                     timeout=TIMEOUT)
    assert r.status_code == 401 and r.json()["error"] == "bad_key"

    made = account["session"].post(f"{BASE}/api/keys", json={"name": "doomed"},
                                   timeout=TIMEOUT).json()["key"]
    assert requests.get(f"{BASE}/api/v1/whoami", headers=auth(made), timeout=TIMEOUT).ok

    account["session"].delete(f"{BASE}/api/keys/{made['id']}", timeout=TIMEOUT)
    after = requests.get(f"{BASE}/api/v1/whoami", headers=auth(made), timeout=TIMEOUT)
    assert after.status_code == 401, "a revoked key still worked"


def test_keyed_calls_are_not_rate_limited(api_key):
    """The product promises no rate limit on the API. A burst that would trip
    the anonymous limiter must go straight through."""
    codes = [requests.get(f"{BASE}/api/v1/whoami", headers=auth(api_key),
                          timeout=TIMEOUT).status_code for _ in range(25)]
    assert 429 not in codes, "a keyed burst was rate limited despite the documented promise"
    assert set(codes) == {200}


def test_cors_allows_the_methods_the_contract_uses(live):
    """Revoking a key and removing a monitor are DELETEs. A browser client on
    another origin has to be allowed to make them."""
    r = requests.options(f"{BASE}/api/v1/blast",
                         headers={"Origin": "https://someone-elses-app.example",
                                  "Access-Control-Request-Method": "DELETE"},
                         timeout=TIMEOUT)
    allowed = r.headers.get("Access-Control-Allow-Methods", "")
    for method in ("GET", "POST", "DELETE"):
        assert method in allowed, f"{method} is used by the API but blocked by CORS"


# ------------------------------------------------- the watch, end to end

class _Sink(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Sink.got.append({"body": raw,
                          "sig": self.headers.get("X-BlastRadius-Signature", ""),
                          "event": self.headers.get("X-BlastRadius-Event")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def test_a_monitor_raises_an_alert_that_reaches_a_webhook(account, api_key):
    """The whole 24/7 loop over real HTTP: register an endpoint, register a
    monitor, let the watch measure it, and prove the alert arrives signed —
    then prove the same alert is readable from the dashboard and the API."""
    import notify

    _Sink.got = []
    sink = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    port = sink.server_address[1]

    try:
        hook = account["session"].post(
            f"{BASE}/api/webhooks",
            json={"url": f"http://127.0.0.1:{port}/hook", "label": "integration"},
            timeout=TIMEOUT).json()["webhook"]

        r = account["session"].post(f"{BASE}/api/monitors",
                                    json={"package": "debug", "label": "integration"},
                                    timeout=TIMEOUT)
        if r.status_code == 409:
            pytest.skip("this account already monitors debug")
        assert r.ok, r.text

        # the baseline measurement runs immediately rather than on the next sweep
        deadline = time.time() + 90
        while time.time() < deadline and not _Sink.got:
            time.sleep(1)
        assert _Sink.got, "the watch raised no alert within 90s of adding a monitor"

        delivery = _Sink.got[0]
        assert delivery["event"] == "alert"
        assert notify.verify(hook["secret"], delivery["body"], delivery["sig"]), \
            "the delivery signature did not verify against the endpoint secret"

        payload = json.loads(delivery["body"])
        assert payload["data"]["package"] == "debug"
        assert payload["data"]["total"] > 0, "the alert carried no measurement"

        # the same alert, through the dashboard and through the keyed API
        dash = account["session"].get(f"{BASE}/api/alerts", timeout=TIMEOUT).json()["alerts"]
        keyed = requests.get(f"{BASE}/api/v1/alerts", headers=auth(api_key),
                             timeout=TIMEOUT).json()["alerts"]
        assert any(a["id"] == payload["id"] for a in dash), "alert missing from the dashboard"
        assert any(a["id"] == payload["id"] for a in keyed), "alert missing from the API"

        # …and the endpoint's own health reflects the delivery
        hooks = account["session"].get(f"{BASE}/api/webhooks", timeout=TIMEOUT).json()["webhooks"]
        mine = next(h for h in hooks if h["id"] == hook["id"])
        assert mine["deliveries"] >= 1 and mine["failures"] == 0
        assert mine["last_ok"] == 1
    finally:
        sink.shutdown()


def test_a_dead_endpoint_is_recorded_not_retried_forever(account):
    hook = account["session"].post(
        f"{BASE}/api/webhooks",
        json={"url": "http://127.0.0.1:9/nothing-listens", "label": "dead"},
        timeout=TIMEOUT).json()["webhook"]

    r = account["session"].post(f"{BASE}/api/webhooks/{hook['id']}/test", timeout=TIMEOUT)
    assert r.ok, r.text
    body = r.json()
    assert body["delivered"] is False
    assert body["detail"], "a failed delivery must say why"
    assert body["webhook"]["failures"] >= 1

    account["session"].delete(f"{BASE}/api/webhooks/{hook['id']}", timeout=TIMEOUT)


# ------------------------------------------------------------- surfaces

def test_every_page_is_served(live):
    for path in ("/", "/check", "/developers", "/dashboard", "/signin"):
        r = requests.get(f"{BASE}{path}", timeout=TIMEOUT)
        assert r.ok, f"{path} -> {r.status_code}"
        assert "text/html" in r.headers.get("content-type", "")
        assert "<title>" in r.text

    # and every asset those pages reference
    for asset in ("/style.css", "/app.css", "/shell.js", "/app.js",
                  "/landing.js", "/check.js", "/developers.js",
                  "/dashboard.js", "/signin.js"):
        r = requests.get(f"{BASE}{asset}", timeout=TIMEOUT)
        assert r.ok, f"{asset} -> {r.status_code}"
        assert r.content, f"{asset} is empty"


def test_the_docs_render_in_all_three_formats(live):
    js = requests.get(f"{BASE}/api/docs.json", timeout=TIMEOUT).json()
    md = requests.get(f"{BASE}/api/docs.md", timeout=TIMEOUT).text
    txt = requests.get(f"{BASE}/api/docs.txt", timeout=TIMEOUT).text

    assert js["endpoints"] and js["quickstarts"]
    # the live origin, not the placeholder, so a copied snippet actually runs
    assert "your-host" not in md and "your-host" not in txt
    for endpoint in js["endpoints"]:
        assert endpoint["path"] in md, f"{endpoint['path']} missing from the Markdown"
        assert endpoint["path"] in txt, f"{endpoint['path']} missing from the text"
    assert "X-BlastRadius-Signature" in md and "X-BlastRadius-Signature" in txt


def test_platform_stats_never_leaks_a_secret(live):
    raw = requests.get(f"{BASE}/api/platform-stats", timeout=TIMEOUT).text
    body = json.loads(raw)
    assert body["ok"] is True
    assert "worker_running" in body

    # The public view answers "is auth configured and working" and nothing about
    # how. Asserted field by field rather than with a blanket string search, so
    # that adding an honest field like `supabase_configured` does not read as a
    # leak while a real one could hide behind a rename.
    assert body["config"]["supabase_configured"] in (True, False)
    assert body["supabase"].keys() <= {"configured", "reachable", "key_valid"}

    for leaked in ("supabase_anon_key", "supabase_url", "supabase_service_key_set",
                   "env_file", "env_keys_loaded", "smtp_host",
                   "pw_hash", "pw_salt", "key_hash", "secret"):
        assert leaked not in raw, f"/api/platform-stats exposes {leaked}"

    for prefix in ("brk_live_", "whsec_", "eyJhbGciOi"):
        assert prefix not in raw, f"a {prefix}… credential appeared in the response"


def test_the_watch_worker_is_actually_running(live):
    body = requests.get(f"{BASE}/api/platform-stats", timeout=TIMEOUT).json()
    assert body["worker_running"] is True, \
        "the 24/7 watch is not running; monitors would never be re-measured"
    assert body["delivery"]["running"] is True, \
        "the delivery worker is not running; alerts would never leave the building"
