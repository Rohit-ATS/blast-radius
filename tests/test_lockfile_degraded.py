"""The lockfile verdict must not depend on the dependency graph.

A package-lock.json is a complete, flattened record of everything an install
resolved. "Is this package in my tree, and at which version" is therefore
answerable from the file alone, and absence is conclusive rather than merely
unobserved. The graph only supplies the paths that *explain* the verdict.

This was a real outage: with HydraDB unreachable the whole section returned
424 and the user saw nothing at all, despite the answer sitting in the file
they had just uploaded. These tests pin the split.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient


LOCK = json.dumps({
    "name": "probe",
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "probe"},
        "node_modules/lodash": {"version": "4.17.20"},
        "node_modules/event-stream": {"version": "3.3.6"},
    },
})


@pytest.fixture(scope="module")
def client(monkeypatch_session=None):
    """A server whose graph is guaranteed unreachable — port 9 is discard."""
    import os
    os.environ["HYDRA_URL"] = "http://127.0.0.1:9"
    os.environ["HYDRA_TOKEN"] = "x"
    os.environ["LIVE_INGEST"] = "0"
    os.environ["LIVE_FEED"] = "0"
    import importlib
    import server
    importlib.reload(server)
    server.note_graph_failure()      # as the health probe would have
    return TestClient(server.app)


def _post(client, query):
    r = client.post(f"/api/lockfile?{query}", content=LOCK)
    return r.status_code, r.json()


def test_a_package_in_the_tree_is_still_reported_with_the_graph_down(client):
    status, d = _post(client, "name=event-stream&bad_version=3.3.6")
    assert status == 200
    assert d["verdict"] == "EXPOSED"
    assert d["direct"]["version"] == "3.3.6"


def test_a_resolved_version_that_is_not_the_bad_one_is_shielded(client):
    status, d = _post(client, "name=event-stream&bad_version=3.3.9")
    assert status == 200
    assert d["verdict"] == "SHIELDED"


def test_absence_from_the_lockfile_is_a_sound_clear(client):
    """The lockfile lists every resolved package, so not being in it is proof
    of absence — not an unanswered question."""
    status, d = _post(client, "name=left-pad")
    assert status == 200
    assert d["verdict"] == "CLEAR"


def test_a_degraded_answer_admits_the_paths_are_missing(client):
    """The verdict is trustworthy; the explanation is not there. If this flag
    were dropped the UI would print "no path reaches it" without having
    looked — an outage rendered as an all-clear."""
    for q in ("name=event-stream&bad_version=3.3.6", "name=left-pad"):
        _, d = _post(client, q)
        assert d["paths_complete"] is False
        assert d["affected"] == []
        assert "graph" in d["degraded"].lower()


def test_a_malformed_lockfile_is_rejected_before_any_graph_work(client):
    status, d = _post(client, "name=lodash")  # overridden below
    assert status == 200                       # sanity: the good body works
    r = client.post("/api/lockfile?name=lodash", content="{ not json")
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["message"]


def test_the_degraded_path_does_not_wait_out_the_retry_backoff(client):
    """Five jittered retries cost ~20s. Paying that to rediscover an outage the
    previous request already found made the section look broken."""
    client.post("/api/lockfile?name=event-stream", content=LOCK)  # trip it
    t0 = time.perf_counter()
    status, _ = _post(client, "name=event-stream&bad_version=3.3.6")
    assert status == 200
    assert (time.perf_counter() - t0) < 5.0, "the breaker is not short-circuiting"


def test_an_uncrawled_package_still_gets_a_verdict(client):
    """The other half of the same bug. A fresh instance has crawled almost
    nothing, and the endpoint used to answer "not crawled yet" to every
    question — including ones the uploaded lockfile already settled. Crawl
    coverage governs how complete the paths are, never whether there is an
    answer."""
    for q, expected in (("name=event-stream&bad_version=3.3.6", "EXPOSED"),
                        ("name=some-package-nobody-has-crawled", "CLEAR")):
        status, d = _post(client, q)
        assert status == 200, f"{q} was refused: {d}"
        assert d["verdict"] == expected
        assert "error" not in d
