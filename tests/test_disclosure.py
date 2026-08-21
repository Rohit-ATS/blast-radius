"""What a failing response is allowed to say.

An exception message is written for whoever reads the logs, and it routinely
carries internal hostnames and ports, filesystem paths, library names and
fragments of SQL. Returning it to the caller publishes all of that to anyone
who can provoke the error — and provoking an error is usually easy.

These tests drive the failure paths with the dependencies deliberately
unreachable and assert on what comes back. They exist because the leaks were
not added carelessly: each one was a `detail` field put there to make an outage
diagnosable, which is a good instinct pointed at the wrong audience. The fix is
in errors.py — full text to the log, a closed vocabulary to the caller — and
these pin it, because the next person debugging an outage will feel the same
pull to put the exception back in the response.
"""

from __future__ import annotations

import importlib
import json
import os
import re

import pytest
from fastapi.testclient import TestClient


# Things that must never appear in a response body. Internal locations, the
# shape of the stack, and the names of the libraries underneath.
FORBIDDEN = re.compile(
    r"""(
        HTTPConnectionPool | urllib3 | psycopg | sqlite3\. | Traceback |
        pooler\.supabase\.com | aws-0- | \.supabase\.co |
        /app/ | /usr/local/ | site-packages |
        docker\s+compose |
        127\.0\.0\.1 | localhost | hydradb-internal | Errno\s+111
    )""",
    re.IGNORECASE | re.VERBOSE)


@pytest.fixture()
def client(monkeypatch):
    """A server whose graph fails, deterministically.

    The first version of this pointed HYDRA_URL at a discard port and reloaded
    the module. That worked alone and failed in a full run: other suites reload
    `server` and `config` for their own reasons, and whichever ran last decided
    what these tests were actually pointed at — so a graph that was supposed to
    be unreachable answered fine and the assertions inverted.

    Making the failure a property of the test rather than of the environment
    removes the ordering question entirely. Every graph call raises HydraError,
    which is exactly the path the disclosure rules are about, and nothing here
    depends on what ran before.
    """
    import server
    from hydra import HydraError

    def refuse(*a, **kw):
        raise HydraError(
            "HTTPConnectionPool(host='hydradb-internal', port=8443): Max "
            "retries exceeded with url: /v1/graphs/default/query (Caused by "
            "NewConnectionError('<urllib3.connection.HTTPConnection object at "
            "0x7f8b8c0d1234>: Failed to establish a new connection: "
            "[Errno 111] Connection refused'))")

    for name in ("hydra", "hydra_probe", "hydra_patient"):
        target = getattr(server, name, None)
        if target is not None:
            monkeypatch.setattr(target, "query", refuse)
            monkeypatch.setattr(target, "timed", refuse, raising=False)

    monkeypatch.setattr(server, "DEV_HINTS", False)      # production default
    return TestClient(server.app, raise_server_exceptions=False)


# `public_url` is this site's own address — the one the caller typed to get
# here. It is 127.0.0.1 in development and the real hostname in production, and
# echoing it back discloses nothing. Excluded by key rather than by loosening
# the pattern, so a 127.0.0.1 appearing anywhere else still fails.
PUBLIC_BY_DESIGN = ("public_url",)


def _scrub(text: str) -> str:
    try:
        body = json.loads(text)
    except ValueError:
        return text

    def strip(node):
        if isinstance(node, dict):
            return {k: ("" if k in PUBLIC_BY_DESIGN else strip(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [strip(x) for x in node]
        return node

    return json.dumps(strip(body))


def _body(client, path):
    r = client.get(path)
    return r.status_code, _scrub(r.text)


@pytest.mark.parametrize("path", [
    "/api/health",
    "/api/stats",
    "/api/blast?name=debug&depth=3",
    "/api/constraints",
    "/api/platform-stats",
    "/api/live/status",
])
def test_no_internal_detail_in_any_response(client, path):
    _, text = _body(client, path)
    found = FORBIDDEN.findall(text)
    assert not found, f"{path} leaked {sorted(set(f[0] for f in found))}"


def test_a_graph_failure_says_why_without_saying_where(client):
    """The caller still needs to know it was a connection problem rather than
    bad input — the classification carries that, the address does not."""
    status, text = _body(client, "/api/blast?name=debug&depth=3")
    assert status == 424
    body = json.loads(text)
    assert body["error"] == "graph_unavailable"
    assert body["reason"] == "connection refused"
    assert "hydra_url" not in body


def test_dev_hints_put_the_address_back(client, monkeypatch):
    """The hints are useful on a laptop. The point is that they are off by
    default, not that they are gone — a flag that cannot be turned on gets
    worked around by putting the exception back in the response."""
    import server
    monkeypatch.setattr(server, "DEV_HINTS", True)
    body = json.loads(client.get("/api/blast?name=debug&depth=3").text)
    assert body.get("hydra_url")


def test_the_database_host_is_not_published(client):
    """It named the managed instance and its region on a public endpoint,
    which is a free first move for anyone choosing a target."""
    import sidecar
    assert "host" not in sidecar.describe()
    if sidecar.IS_POSTGRES:
        assert "host" in sidecar.describe(include_host=True)


def test_a_parse_error_still_quotes_the_users_own_input(client):
    """The opposite failure. These messages are about the file the caller just
    uploaded, not about us, and stripping them would make a malformed lockfile
    impossible to debug from the outside."""
    r = client.post("/api/audit", content="{ not json")
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["message"]


def test_reason_never_returns_text_from_the_exception():
    """The guarantee the whole approach rests on: the vocabulary is closed, so
    an unfamiliar exception cannot introduce a new string."""
    import errors
    secret = "host=db.internal port=5432 password=hunter2 /app/server.py"
    for exc in (RuntimeError(secret), ValueError(secret), OSError(secret)):
        out = errors.reason(exc)
        assert secret not in out
        assert out in {
            errors.UNREACHABLE, errors.DNS, errors.TIMEOUT, errors.TLS,
            errors.AUTH, errors.NOT_FOUND, errors.RATE_LIMITED,
            errors.BAD_RESPONSE, errors.DATA, errors.UNAVAILABLE,
            errors.INTERNAL,
        }
