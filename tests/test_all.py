"""Blast Radius test suite.

Four layers, in order of how much they need running:

  1. semver + lockfile parsing   — pure, no services
  2. graph queries               — needs HydraDB
  3. HTTP API                    — needs `py server.py`
  4. the console in a browser    — needs Playwright + Chrome

Layers that cannot run are skipped with the reason, so a partial environment
still gives a useful signal instead of a wall of errors.

Run:  py -m pytest tests -v
"""

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blast                                                    # noqa: E402
from hydra import Hydra, HydraError, nid, pkg_id               # noqa: E402

BASE = os.environ.get("BLAST_BASE", "http://127.0.0.1:8000")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
DB_PATH = os.path.join(ROOT, "deps.db")

# Synthetic test vertices are addressed at nid(name), because that is the id
# blast_radius() derives from a name and traverses from. Writing them anywhere
# else makes the traversal start from an empty vertex and quietly find nothing.
# The names are underscore-prefixed, which npm does not allow, so they cannot
# collide with a crawled package.


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def hydra():
    h = Hydra()
    try:
        h.query("MATCH (p:Package) RETURN count(*)")
    except Exception as e:
        pytest.skip(f"HydraDB not reachable: {e}")
    return h


@pytest.fixture(scope="session")
def db():
    if not os.path.exists(DB_PATH):
        pytest.skip("deps.db not present — run ingest.py first")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    if not conn.execute("SELECT count(*) FROM packages").fetchone()[0]:
        pytest.skip("deps.db is empty — run ingest.py first")
    return conn


@pytest.fixture(scope="session")
def requests_mod():
    requests = pytest.importorskip("requests")
    try:
        requests.get(f"{BASE}/api/stats", timeout=5).raise_for_status()
    except Exception as e:
        pytest.skip(f"server not running at {BASE}: {e}")
    return requests


@pytest.fixture(scope="session")
def writable(hydra):
    """Whether the graph still accepts writes.

    HydraDB 0.1.0 on the local-filesystem object store cannot update an
    existing SlateDB manifest, so every boot after the first leaves the store
    read-only: reads answer perfectly and writes return a 500. Tests that need
    to create vertices skip with that reason rather than reporting a code
    defect — `py rebuild.py` restores a writable graph from the sidecar.
    """
    probe = 999999999999997
    try:
        hydra.query("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:_Probe",
                    {"rows": [{"id": probe}]}, retries=1)
        hydra.query("MATCH (p {id: $id}) DETACH DELETE p", {"id": probe}, retries=1)
        return True
    except HydraError as e:
        pytest.skip(f"graph is read-only (run `py rebuild.py`): {str(e)[:120]}")


@pytest.fixture(scope="session")
def seeded(hydra):
    """A package with a known non-trivial blast radius, chosen from the graph
    rather than hardcoded — which packages got crawled varies by run."""
    for candidate in ("debug", "ms", "chalk", "tslib", "semver"):
        rows = hydra.query(blast.EXISTS, {"id": pkg_id(candidate, "npm")})
        if rows and rows[0].get("p.name"):
            return candidate
    pytest.skip("no seed package present in the graph")


# --------------------------------------------------------------------------
# 1. semver
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version,rng,expected", [
    # caret: locks the leftmost non-zero
    ("4.4.2",  "^4.1.0",  True),
    ("5.0.0",  "^4.1.0",  False),
    ("4.0.9",  "^4.1.0",  False),
    ("0.7.29", "^0.7.0",  True),
    ("0.8.0",  "^0.7.0",  False),     # 0.x: minor is breaking
    ("0.0.4",  "^0.0.3",  False),     # 0.0.x: exact only
    ("0.0.3",  "^0.0.3",  True),
    # tilde: minor locked
    ("1.2.9",  "~1.2.3",  True),
    ("1.3.0",  "~1.2.3",  False),
    ("1.2.2",  "~1.2.3",  False),
    # comparators
    ("2.0.0",  ">=1.0.0", True),
    ("0.9.0",  ">=1.0.0", False),
    ("1.0.0",  "=1.0.0",  True),
    ("1.0.1",  "<1.0.1",  False),
    ("1.0.0",  "<=1.0.0", True),
    # compound (space-separated = AND)
    ("1.5.0",  ">=1.0.0 <2.0.0", True),
    ("2.0.0",  ">=1.0.0 <2.0.0", False),
    # union
    ("3.0.0",  "^1.0.0 || ^3.0.0", True),
    ("2.0.0",  "^1.0.0 || ^3.0.0", False),
    # hyphen range
    ("1.5.0",  "1.0.0 - 2.0.0", True),
    ("2.5.0",  "1.0.0 - 2.0.0", False),
    ("2.0.0",  "1.0.0 - 2.0.0", True),   # inclusive
    # wildcards
    ("9.9.9",  "*",  True),
    ("9.9.9",  "",   True),
    ("9.9.9",  "x",  True),
    # prerelease versions parse to their release triple
    ("1.0.0-beta.1", "^1.0.0", True),
    ("1.0.0+build7", "^1.0.0", True),
    # a leading v is tolerated
    ("v1.2.3", "^1.2.0", True),
])
def test_semver(version, rng, expected):
    assert blast.satisfies(version, rng) is expected


@pytest.mark.parametrize("rng", [
    "git+https://github.com/a/b.git",
    "github:user/repo#semver:^1.0.0",
    "file:../local-package",
    "npm:@scope/other@^1.0.0",
    "workspace:*",
    "link:../thing",
    "http://example.com/pkg.tgz",
    "not a version at all",
    ">>>>1.0.0",
    "^",
    "~",
])
def test_semver_unparseable_is_false_not_crash(rng):
    """Under-reporting exposure is the safe failure. What must never happen is
    an exception in the middle of an incident query."""
    assert blast.satisfies("1.0.0", rng) is False


@pytest.mark.parametrize("version", ["", "not-a-version", "1.2", "1", None, "1.2.3.4.5"])
def test_semver_bad_version_is_false(version):
    assert blast.satisfies(version if version is not None else "", "^1.0.0") is False


# --------------------------------------------------------------------------
# 2. lockfile parsing
# --------------------------------------------------------------------------

def test_lockfile_v3_flat_packages():
    lock = {"lockfileVersion": 3, "packages": {
        "": {"name": "root", "version": "1.0.0"},
        "node_modules/express": {"version": "4.18.2"},
        "node_modules/debug": {"version": "4.4.2"},
    }}
    got = blast.parse_lockfile(json.dumps(lock))
    assert got == {"express": "4.18.2", "debug": "4.4.2"}


def test_lockfile_v2_has_both_shapes():
    lock = {"lockfileVersion": 2,
            "packages": {"node_modules/a": {"version": "1.0.0"}},
            "dependencies": {"b": {"version": "2.0.0"}}}
    got = blast.parse_lockfile(json.dumps(lock))
    assert got == {"a": "1.0.0", "b": "2.0.0"}


def test_lockfile_v1_nested_tree_is_walked():
    """v1 nests dependencies inside dependencies; a non-recursive parser sees
    only the top level and reports a far smaller tree than you actually have."""
    lock = {"lockfileVersion": 1, "dependencies": {
        "express": {"version": "4.17.1", "dependencies": {
            "debug": {"version": "2.6.9", "dependencies": {
                "ms": {"version": "2.0.0"}}}}}}}
    got = blast.parse_lockfile(json.dumps(lock))
    assert got == {"express": "4.17.1", "debug": "2.6.9", "ms": "2.0.0"}


def test_lockfile_scoped_names_survive():
    lock = {"lockfileVersion": 3, "packages": {
        "node_modules/@types/node": {"version": "20.1.0"},
        "node_modules/a/node_modules/@babel/core": {"version": "7.0.0"},
    }}
    got = blast.parse_lockfile(json.dumps(lock))
    assert got["@types/node"] == "20.1.0"
    assert got["@babel/core"] == "7.0.0"


def test_lockfile_empty_object():
    assert blast.parse_lockfile("{}") == {}


@pytest.mark.parametrize("text", ["", "not json", "[1,2,3]", "null", '{"a":'])
def test_lockfile_malformed_raises_cleanly(text):
    with pytest.raises((json.JSONDecodeError, ValueError)):
        blast.parse_lockfile(text)


def test_lockfile_huge_is_handled():
    """25k entries — bigger than most real monorepo lockfiles."""
    lock = {"lockfileVersion": 3, "packages": {
        f"node_modules/pkg-{i}": {"version": f"1.0.{i}"} for i in range(25_000)}}
    text = json.dumps(lock)
    assert len(text) > 1_000_000
    t0 = time.perf_counter()
    got = blast.parse_lockfile(text)
    assert len(got) == 25_000
    assert (time.perf_counter() - t0) < 10


def test_lockfile_entries_without_versions_are_skipped():
    lock = {"lockfileVersion": 3, "packages": {
        "node_modules/a": {"version": "1.0.0"},
        "node_modules/b": {},                       # link/workspace stub
        "node_modules/c": "not-a-dict",
    }}
    assert blast.parse_lockfile(json.dumps(lock)) == {"a": "1.0.0"}


# --------------------------------------------------------------------------
# 3. graph queries
# --------------------------------------------------------------------------

def test_nid_is_stable_and_in_range():
    assert nid("left-pad") == nid("left-pad")
    assert nid("left-pad") != nid("left-pads")
    for name in ("a", "@types/node", "debug", "x" * 214):
        assert 0 <= nid(name) < 2 ** 53 - 1
    # Ecosystems must not collide: `requests` exists on PyPI and RubyGems.
    assert pkg_id("requests", "npm") != pkg_id("requests", "pypi")


def test_nid_no_collisions_across_the_crawled_corpus(db):
    """The whole id scheme rests on this. Check it against every real name."""
    names = [r[0] for r in db.execute("SELECT name FROM packages")]
    ids = {}
    for n in names:
        i = nid(n)
        assert ids.setdefault(i, n) == n, f"collision: {ids[i]!r} vs {n!r} -> {i}"
    assert db.execute("SELECT count(*) FROM collisions").fetchone()[0] == 0


def test_unknown_package_is_absent_not_empty(hydra):
    known, _ = blast.resolve_package(hydra, "definitely-not-a-real-package-zzz-9182")
    assert known is False


def test_known_package_resolves(hydra, seeded):
    known, ms = blast.resolve_package(hydra, seeded)
    assert known is True
    assert ms >= 0


@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5, 6])
def test_blast_radius_at_each_depth(hydra, seeded, depth):
    r, ms = blast.blast_radius(hydra, seeded, depth)
    assert len(r["histogram"]) == depth
    assert r["depth"] == depth
    assert r["total"] >= 0
    assert ms > 0
    # The listed victims must agree with the count, not be a truncated page.
    if not r["truncated"]:
        assert len(r["victims"]) == r["total"]


def test_blast_radius_is_monotonic_in_depth(hydra, seeded):
    """Reach can only grow as the bound grows. If it shrinks, the differencing
    that builds the histogram is measuring something other than reachability."""
    totals = [blast.blast_radius(hydra, seeded, d)[0]["total"] for d in (1, 2, 3, 4)]
    assert totals == sorted(totals)
    assert all(t >= 0 for t in totals)


def test_histogram_sums_to_total(hydra, seeded):
    r, _ = blast.blast_radius(hydra, seeded, 5)
    assert sum(h["packages"] for h in r["histogram"]) == r["total"]


def test_blast_radius_rejects_absurd_depth(hydra, seeded):
    for bad in (0, -1, 99):
        with pytest.raises(ValueError):
            blast.blast_radius(hydra, seeded, bad)


def test_package_with_no_dependents(hydra, db):
    """A leaf: in the graph, but nothing requires it. Must be 0, not an error."""
    row = db.execute(
        """SELECT p.name FROM packages p
           WHERE p.crawled = 1
             AND NOT EXISTS (SELECT 1 FROM deps d WHERE d.dst = p.name)
           LIMIT 1""").fetchone()
    if not row:
        pytest.skip("every crawled package has at least one dependent")
    r, _ = blast.blast_radius(hydra, row[0], 5)
    assert r["total"] == 0
    assert r["victims"] == []
    assert all(h["packages"] == 0 for h in r["histogram"])


def test_cycle_terminates(hydra, writable):
    """a -> b -> c -> a. A traversal that treats this as paths rather than
    reachability would not come back."""
    names = ["_cycle_a", "_cycle_b", "_cycle_c"]
    ids = [pkg_id(n, "npm") for n in names]
    try:
        hydra.query("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:Package, p.name = row.name",
                    {"rows": [{"id": pkg_id(n, "npm"), "name": n} for n in names]})
        hydra.query("UNWIND $rows AS row CREATE (a {id: row.src})-[:REQUIRED_BY]->(b {id: row.dst})",
                    {"rows": [{"src": ids[0], "dst": ids[1]},
                              {"src": ids[1], "dst": ids[2]},
                              {"src": ids[2], "dst": ids[0]}]})
        r, ms = blast.blast_radius(hydra, "_cycle_a", 6)
        # Reachable set is {b, c, a} — a is reachable from itself around the loop.
        assert r["total"] <= 3
        assert "_cycle_b" in r["victims"] and "_cycle_c" in r["victims"]
        assert ms < 30_000
    finally:
        for i in ids:
            try:
                hydra.query("MATCH (p {id: $id}) DETACH DELETE p", {"id": i})
            except HydraError:
                pass


def test_self_loop_terminates(hydra, writable):
    i = pkg_id("_selfloop", "npm")
    try:
        hydra.query("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:Package, p.name = row.name",
                    {"rows": [{"id": i, "name": "_selfloop"}]})
        hydra.query("UNWIND $rows AS row CREATE (a {id: row.src})-[:REQUIRED_BY]->(b {id: row.dst})",
                    {"rows": [{"src": i, "dst": i}]})
        r, _ = blast.blast_radius(hydra, "_selfloop", 5)
        assert r["total"] <= 1
    finally:
        try:
            hydra.query("MATCH (p {id: $id}) DETACH DELETE p", {"id": i})
        except HydraError:
            pass


def test_pagination_returns_more_than_one_page(hydra, db):
    """HydraDB pages at 1024 rows. Find a package with a bigger reach and check
    the client stitched the pages rather than stopping at the first."""
    row = db.execute(
        "SELECT dst, count(*) c FROM deps WHERE kind='prod' GROUP BY dst "
        "ORDER BY c DESC LIMIT 1").fetchone()
    if not row:
        pytest.skip("no dependency edges")
    r, _ = blast.blast_radius(hydra, row[0], 5, limit=100_000)
    if r["total"] <= 1024:
        pytest.skip(f"widest package {row[0]!r} reaches only {r['total']}")
    assert len(r["victims"]) == r["total"] > 1024
    assert len(set(r["victims"])) == len(r["victims"])


# --------------------------------------------------------------------------
# 4. sidecar-backed queries
# --------------------------------------------------------------------------

def test_would_resolve_splits_exposed_from_pinned(db):
    row = db.execute(
        "SELECT dep FROM release_deps WHERE range LIKE '^%' GROUP BY dep "
        "ORDER BY count(*) DESC LIMIT 1").fetchone()
    if not row:
        pytest.skip("no caret ranges in the sidecar")
    r, ms = blast.would_resolve(db, row[0], "999.0.0")
    assert r["checked"] > 0
    assert ms >= 0
    # A caret range cannot admit 999.0.0, but `*`, `latest` and `>=x` all can,
    # so the exposed bucket is not expected to be empty — every member of it
    # must simply be there for a reason semver agrees with.
    for e in r["exposed"]:
        assert any(blast.satisfies("999.0.0", rng) for rng in e["ranges"]), e
    for sh in r["shielded"]:
        assert not any(blast.satisfies("999.0.0", rng) for rng in sh["ranges"]), sh
    # Every name appears in exactly one bucket.
    assert not ({e["name"] for e in r["exposed"]} & {s["name"] for s in r["shielded"]})


def test_would_resolve_finds_real_exposure(db):
    row = db.execute(
        "SELECT dep, range FROM release_deps WHERE range LIKE '^_._._' "
        "AND dep NOT LIKE '@%' LIMIT 1").fetchone()
    if not row:
        pytest.skip("no simple caret range to probe")
    dep, rng = row
    r, _ = blast.would_resolve(db, dep, rng.lstrip("^"))
    assert r["exposed_count"] >= 1, f"{rng} must admit its own base version"


def test_search_prefix(db):
    rows, ms = blast.search(db, "deb", 10)
    assert rows and all(r["name"].startswith("deb") for r in rows)
    assert ms >= 0


def test_search_escapes_wildcards(db):
    """A bare % would otherwise match every package in the corpus."""
    rows, _ = blast.search(db, "%", 10)
    assert rows == []


def test_maintainer_pivot(db):
    row = db.execute(
        "SELECT package FROM maintainers GROUP BY package HAVING count(*) >= 1 "
        "LIMIT 1").fetchone()
    if not row:
        pytest.skip("no maintainer rows")
    r, ms = blast.maintainer_pivot(db, row[0])
    assert r["maintainers"]
    assert row[0] not in [s["package"] for s in r["also_controls"]]
    assert ms >= 0


def test_shortest_path_reconstruction(db):
    edge = db.execute("SELECT src, dst FROM deps WHERE kind='prod' LIMIT 1").fetchone()
    if not edge:
        pytest.skip("no edges")
    src, dst = edge
    chain = blast.shortest_path(db, [src], dst)
    assert chain and chain[0] == src and chain[-1] == dst


def test_typosquat_ring(db):
    r, ms = blast.typosquat_ring(db, "express")
    assert r["candidates"] > 0
    assert all(h["name"] != "express" for h in r["existing"])
    assert ms >= 0


# --------------------------------------------------------------------------
# 5. HTTP API
# --------------------------------------------------------------------------

def test_api_stats(requests_mod):
    r = requests_mod.get(f"{BASE}/api/stats", timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert d["packages"] > 0 and d["edges"] > 0
    assert "crawl" in d and "latency_ms" in d


def test_api_stats_is_fast(requests_mod):
    """It sits behind a 4-second browser poll; a full graph scan here would
    wedge the page, which is why the scan moved to a background thread."""
    t0 = time.perf_counter()
    requests_mod.get(f"{BASE}/api/stats", timeout=30)
    assert (time.perf_counter() - t0) < 3.0


def test_api_blast_happy(requests_mod, seeded):
    d = requests_mod.get(f"{BASE}/api/blast",
                         params={"name": seeded, "depth": 3}, timeout=120).json()
    assert d["name"] == seeded
    assert len(d["histogram"]) == 3
    assert d["latency_ms"] > 0
    assert d["vertex_id"] == pkg_id(seeded, "npm")


def test_api_blast_unknown_package_is_404_with_explanation(requests_mod):
    r = requests_mod.get(f"{BASE}/api/blast",
                         params={"name": "no-such-package-zzz-9182"}, timeout=60)
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "not_in_graph"
    assert "message" in d and d["name"] == "no-such-package-zzz-9182"


def test_api_blast_missing_param(requests_mod):
    assert requests_mod.get(f"{BASE}/api/blast", timeout=30).status_code == 422


@pytest.mark.parametrize("depth", [0, 99, -3, 9])
def test_api_blast_rejects_out_of_range_depth(requests_mod, seeded, depth):
    r = requests_mod.get(f"{BASE}/api/blast",
                         params={"name": seeded, "depth": depth}, timeout=30)
    assert r.status_code == 422


def test_api_resolve(requests_mod, seeded):
    d = requests_mod.get(f"{BASE}/api/resolve",
                         params={"name": seeded, "bad_version": "999.0.0"},
                         timeout=60).json()
    assert d["checked"] >= 0
    assert d["exposed_count"] + d["shielded_count"] <= d["checked"]
    # Anything counted as exposed to 999.0.0 got there through an open range.
    for e in d["exposed"]:
        assert any(blast.satisfies("999.0.0", rng) for rng in e["ranges"]), e


def test_api_resolve_missing_version(requests_mod, seeded):
    r = requests_mod.get(f"{BASE}/api/resolve", params={"name": seeded}, timeout=30)
    assert r.status_code == 422


def test_api_maintainers(requests_mod, seeded):
    d = requests_mod.get(f"{BASE}/api/maintainers",
                         params={"name": seeded}, timeout=60).json()
    assert "also_controls" in d and "latency_ms" in d


def test_api_search(requests_mod):
    d = requests_mod.get(f"{BASE}/api/search", params={"q": "expr"}, timeout=30).json()
    assert d["results"]


def test_api_search_blank_is_empty_not_error(requests_mod):
    d = requests_mod.get(f"{BASE}/api/search", params={"q": "   "}, timeout=30).json()
    assert d["results"] == []


def _fixture(name):
    path = os.path.join(FIXTURES, name)
    if not os.path.exists(path):
        pytest.skip(f"missing fixture {name}")
    return open(path, "rb").read()


def test_api_lockfile_exposed(requests_mod, seeded):
    lock = json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "t", "version": "1.0.0"},
        f"node_modules/{seeded}": {"version": "1.0.0"}}})
    d = requests_mod.post(f"{BASE}/api/lockfile", params={"name": seeded},
                          data=lock.encode(), timeout=120).json()
    assert d["verdict"] == "EXPOSED"
    assert d["direct"]["version"] == "1.0.0"


def test_api_lockfile_clear(requests_mod, seeded):
    lock = json.dumps({"lockfileVersion": 3, "packages": {
        "node_modules/totally-unrelated-zzz-9182": {"version": "1.0.0"}}})
    d = requests_mod.post(f"{BASE}/api/lockfile", params={"name": seeded},
                          data=lock.encode(), timeout=120).json()
    assert d["verdict"] == "CLEAR"
    assert d["affected_count"] == 0


def test_api_lockfile_shielded_by_pin(requests_mod, seeded):
    lock = json.dumps({"lockfileVersion": 3, "packages": {
        f"node_modules/{seeded}": {"version": "1.0.0"}}})
    d = requests_mod.post(f"{BASE}/api/lockfile",
                          params={"name": seeded, "bad_version": "99.99.99"},
                          data=lock.encode(), timeout=120).json()
    assert d["verdict"] == "SHIELDED"


def test_api_lockfile_malformed(requests_mod, seeded):
    r = requests_mod.post(f"{BASE}/api/lockfile", params={"name": seeded},
                          data=b"not json", timeout=60)
    assert r.status_code == 400
    body = r.json()
    # `error` is a machine-readable code; the prose lives in `message`, so a
    # caller can branch on the failure without parsing English.
    assert body["ok"] is False
    assert body["error"] == "bad_request"
    assert "not valid JSON" in body["message"]


def test_api_lockfile_empty_body(requests_mod, seeded):
    r = requests_mod.post(f"{BASE}/api/lockfile", params={"name": seeded},
                          data=b"", timeout=60)
    assert r.status_code == 400


def test_api_lockfile_v1_fixture(requests_mod):
    body = _fixture("lock-v1.json")
    d = requests_mod.post(f"{BASE}/api/lockfile",
                          params={"name": "debug", "bad_version": "2.6.9"},
                          data=body, timeout=120).json()
    assert d["verdict"] in ("EXPOSED", "SHIELDED", "CLEAR")
    assert d["resolved_count"] == 3


def test_api_subgraph_structure(requests_mod, seeded):
    """The drawable slice must be internally consistent: every edge endpoint is
    a node, depths only ever increase along an edge, and the root is depth 0."""
    d = requests_mod.get(f"{BASE}/api/subgraph",
                         params={"name": seeded, "depth": 3}, timeout=180).json()
    names = {n["name"] for n in d["nodes"]}
    depth_of = {n["name"]: n["depth"] for n in d["nodes"]}
    assert d["root"] == seeded
    assert depth_of[seeded] == 0
    assert names, "no nodes returned"
    for e in d["edges"]:
        assert e["from"] in names and e["to"] in names, e
    # Depth is the *shortest* distance from the root, so a cross-link may point
    # from a deeper node back to a shallower one — a package reached at depth 2
    # by one route can also be depended on by something at depth 3. What must
    # hold for the drawing to make sense is that every node has a parent one
    # ring inward, so nothing floats unattached.
    for n in d["nodes"]:
        if n["depth"] == 0:
            continue
        parents = [e["from"] for e in d["edges"] if e["to"] == n["name"]]
        assert any(depth_of[p] == n["depth"] - 1 for p in parents), \
            f"{n['name']} at depth {n['depth']} has no parent at depth {n['depth'] - 1}"
    # Every non-root node must be reachable from something already drawn.
    reachable = {seeded}
    for _ in range(d["depth"] + 1):
        for e in d["edges"]:
            if e["from"] in reachable:
                reachable.add(e["to"])
    assert names <= reachable, names - reachable


def test_api_subgraph_honours_its_own_caps(requests_mod, seeded):
    d = requests_mod.get(f"{BASE}/api/subgraph",
                         params={"name": seeded, "depth": 2, "per_level": 5,
                                 "max_nodes": 12}, timeout=180).json()
    assert len(d["nodes"]) <= 12
    for level in (1, 2):
        assert sum(1 for n in d["nodes"] if n["depth"] == level) <= 5
    # The headline number stays the true one even when the drawing is a sample.
    assert d["total_exposed"] >= d["shown"]
    if d["shown"] < d["total_exposed"]:
        assert d["truncated"] is True


def test_api_subgraph_unknown_package(requests_mod):
    r = requests_mod.get(f"{BASE}/api/subgraph",
                         params={"name": "no-such-package-zzz-9182"}, timeout=60)
    assert r.status_code == 404


def test_api_typosquats_checks_the_registry(requests_mod, seeded):
    """The crawled corpus is popularity-weighted and typosquats are by
    definition unpopular, so a corpus-only answer reads as 'you are safe'."""
    d = requests_mod.get(f"{BASE}/api/typosquats",
                         params={"name": seeded}, timeout=120).json()
    assert d["candidates"] > 0
    assert set(h["name"] for h in d["existing"]) <= set(blast.edit1(seeded))
    for h in d["existing"]:
        assert "in_graph" in h and "latest" in h


def test_api_events_streams_real_frames(requests_mod):
    """Server-sent events, read far enough to see one real stats frame."""
    with requests_mod.get(f"{BASE}/api/events", stream=True, timeout=30) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        payload = None
        for raw in r.iter_lines(decode_unicode=True):
            if raw and raw.startswith("data: "):
                payload = json.loads(raw[6:])
                break
        assert payload is not None, "no data frame arrived"
        assert payload["packages"] > 0 and payload["edges"] > 0
        assert "warmup" in payload and "crawl" in payload


def test_api_serves_the_console(requests_mod):
    for path, needle in (("/", b"blast radius"), ("/app.js", b"draggable"),
                         ("/style.css", b"--paper")):
        r = requests_mod.get(f"{BASE}{path}", timeout=30)
        assert r.status_code == 200 and needle in r.content


# --------------------------------------------------------------------------
# 6. the console, in a real browser
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def page(requests_mod):
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed").sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(channel="chrome", headless=True)
    except Exception as e:
        pw.stop()
        pytest.skip(f"no Chrome for Playwright: {e}")
    pg = browser.new_page(viewport={"width": 1600, "height": 1100})
    pg.errors = []
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    # The console lives at /check now; the landing page is marketing plus the
    # live-ingest rail, which `landing` below covers.
    # networkidle never fires — the header polls /api/stats every 4 seconds.
    pg.goto(f"{BASE}/check", wait_until="domcontentloaded")
    pg.wait_for_selector("#peek-hist .skel", state="detached", timeout=40_000)
    # ...and separately for the header's first successful poll. The histogram
    # and the header are fed by different endpoints, so the skeleton detaching
    # says nothing about whether the stats have landed — waiting only on the
    # first one made every header assertion a race it usually won.
    pg.wait_for_function(
        "!document.querySelector('#statline').textContent.includes('connecting')",
        timeout=40_000)
    yield pg
    browser.close()
    pw.stop()


@pytest.fixture(scope="session")
def landing(page):
    """A second page on the landing route. `page` is session-scoped and drives
    the console at /check; the ingest rail and project monitor render on / and
    would otherwise have nowhere to be asserted against."""
    # `page` was opened with browser.new_page(), which owns its context, so a
    # sibling tab has to come from a context of its own.
    ctx = page.context.browser.new_context(viewport={"width": 1600, "height": 1100})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_function(
        "!document.querySelector('#statline').textContent.includes('connecting')",
        timeout=40_000)
    yield pg
    ctx.close()


def test_ui_loads_with_live_stats(page):
    assert "connecting" not in page.text_content("#statline")
    assert "packages" in page.text_content("#statline")
    assert page.get_attribute("#pulse", "class") == "pulse live"


def test_ui_hero_previews_hold_real_numbers(page):
    text = page.text_content("#peek-graph")
    assert "packages" in text and any(c.isdigit() for c in text)


def test_ui_query_renders_results(page):
    page.click(".chip")
    page.wait_for_function("document.querySelector('#latency').textContent !== '—'",
                           timeout=90_000)
    page.wait_for_timeout(1200)
    assert page.text_content("#latency").endswith("ms")
    assert page.eval_on_selector_all(".hrow", "e => e.length") == 5
    # Bars must actually paint: .fill is not a grid item and would stay inline.
    widths = page.eval_on_selector_all(
        ".hrow .fill", "els => els.map(e => getComputedStyle(e).width)")
    assert any(w != "0px" and w != "auto" for w in widths), widths
    assert page.eval_on_selector_all("#victims .r", "e => e.length") > 0


def test_ui_windows_drag(page):
    # A previous test smooth-scrolls the results into view; measuring a
    # bounding box mid-scroll aims the drag at where the window used to be.
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_function("window.scrollY === 0", timeout=10_000)
    page.wait_for_timeout(250)
    handle = page.query_selector(".scatter .win .bar")
    if not handle:
        pytest.skip("scatter hidden at this viewport")
    before = page.eval_on_selector(".scatter .win", "el => el.style.transform")
    bb = handle.bounding_box()
    page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    page.mouse.down()
    page.mouse.move(bb["x"] + bb["width"] / 2 + 110, bb["y"] + bb["height"] / 2 + 70,
                    steps=10)
    page.mouse.up()
    after = page.eval_on_selector(".scatter .win", "el => el.style.transform")
    assert before != after, "title-bar drag did not move the window"


def test_ui_lockfile_drop(page):
    path = os.path.join(FIXTURES, "lock-v3.json")
    if not os.path.exists(path):
        pytest.skip("missing lock-v3.json fixture")
    page.fill("#pkg", "debug")
    page.fill("#ver", "2.6.9")
    page.set_input_files("#file", path)
    page.wait_for_function(
        "document.querySelector('#verdict .word')?.textContent?.match(/EXPOSED|SHIELDED|CLEAR/)",
        timeout=90_000)
    assert page.text_content("#verdict .word") in ("EXPOSED", "SHIELDED", "CLEAR")


def test_ui_blast_map_draws(page):
    page.wait_for_selector("#map .node", timeout=120_000)
    nodes = page.eval_on_selector_all("#map .node", "e => e.length")
    edges = page.eval_on_selector_all("#map .edge", "e => e.length")
    assert nodes > 1 and edges > 0, f"{nodes} nodes, {edges} edges"
    # Rim labels and leaders must stay inside the viewBox, or they are clipped.
    box = page.eval_on_selector(
        "#map", "el => { const b = el.getBBox(); return [b.x, b.y, b.x + b.width, b.y + b.height]; }")
    assert box[0] >= -2 and box[1] >= -2, box
    assert box[2] <= 762 and box[3] <= 762, box
    assert page.eval_on_selector_all("#map .node.label", "e => e.length") > 0


def test_ui_map_click_pivots_the_console(page):
    page.wait_for_selector("#map .node", timeout=120_000)
    before = page.input_value("#pkg")
    page.eval_on_selector_all(
        "#map .node",
        "els => els.find(e => !e.classList.contains('root') && "
        "!e.classList.contains('label')).dispatchEvent("
        "new MouseEvent('click', {bubbles: true}))")
    page.wait_for_function(
        f"document.querySelector('#pkg').value !== {before!r}", timeout=60_000)
    after = page.input_value("#pkg")
    assert after and after != before
    # The pivot must be shareable, not just visible.
    assert "pkg=" in page.url


def test_ui_status_cards_are_live(page):
    page.wait_for_selector("#syscards .card", timeout=60_000)
    assert page.eval_on_selector_all("#syscards .card", "e => e.length") >= 6
    text = page.text_content("#syscards")
    assert "hydradb" in text and "writable" in text
    assert any(c.isdigit() for c in text)


def test_ui_typosquat_panel(page):
    page.wait_for_selector("#typos .r, #typos .empty", timeout=120_000)
    assert (page.text_content("#typos") or "").strip()


def test_ui_ecosystem_rail_shows_every_registry(landing):
    """One card per registry, each carrying the state the daemon recorded.

    The states are asserted against the vocabulary `live.py` can actually
    produce, so a card that renders a made-up status fails here rather than
    reassuring somebody during an incident.
    """
    landing.wait_for_selector(".eco", timeout=40_000)
    cards = landing.eval_on_selector_all(".eco", "e => e.length")
    assert cards == 5, f"expected five registries, got {cards}"

    states = landing.eval_on_selector_all(".eco .eco-state",
                                       "e => e.map(x => x.textContent.trim())")
    allowed = {"live", "starting", "degraded", "backoff", "stopped"}
    assert set(states) <= allowed, states

    # every card carries a real count, not a dash
    counts = landing.eval_on_selector_all(".eco .eco-n",
                                       "e => e.map(x => x.textContent.trim())")
    assert all(c and c != "—" for c in counts), counts


def test_ui_ingest_ticker_carries_real_publishes(landing):
    """The ticker must show packages the daemon actually wrote, with the
    ecosystem it came from — or say plainly that nothing has landed."""
    landing.wait_for_selector("#ingestticker .tick, #ingestticker .empty",
                           timeout=40_000)
    ticks = landing.eval_on_selector_all("#ingestticker .tick", "e => e.length")
    if not ticks:
        assert "no publish" in landing.text_content("#ingestticker").lower()
        return
    badges = landing.eval_on_selector_all(
        "#ingestticker .tick .badge", "e => e.map(x => x.textContent.trim())")
    assert set(badges) <= {"npm", "PyPI", "crates.io", "Go", "Maven"}, badges
    stat = landing.text_content("#ingeststat")
    assert "graph writes" in stat, stat


def test_ui_monitor_registers_and_streams(landing, tmp_path):
    """The whole integration path, driven the way a user drives it: drop a
    lockfile, get credentials back, hold an SSE stream open, then stop."""
    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps({
        "name": "ui-test", "lockfileVersion": 3,
        "packages": {"": {"name": "ui-test"},
                     "node_modules/express": {"name": "express", "version": "4.18.2"},
                     "node_modules/ms": {"name": "ms", "version": "2.1.3"}}}))

    landing.set_input_files("#monfile", str(lock))
    # 'registering…' is an intermediate state, so waiting for "not the initial
    # text" passes before anything has actually happened.
    landing.wait_for_function(
        "document.querySelector('#mon-stat').textContent.includes('watching')",
        timeout=90_000)

    stat = landing.text_content("#mon-stat")
    assert "watching" in stat and "npm" in stat and "exact" in stat, stat

    # credentials come back and the token is not echoed into the URL bar
    creds = landing.eval_on_selector_all(".cred-row code", "e => e.map(x => x.textContent)")
    assert len(creds) >= 2 and len(creds[1]) > 20, creds
    assert creds[1] not in landing.url

    landing.wait_for_function(
        "document.querySelector('#mon-alertstat').textContent.includes('streaming')",
        timeout=60_000)

    landing.click("#monstop")
    landing.wait_for_function(
        "document.querySelector('#mon-stat').textContent.includes('no project')",
        timeout=60_000)
    assert landing.eval_on_selector("#moncred", "e => e.hidden") is True


def test_ui_monitor_refuses_a_file_that_is_not_a_manifest(landing, tmp_path):
    """A readable failure beats a registered project watching nothing."""
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not a lockfile, it is a shopping list")
    landing.set_input_files("#monfile", str(junk))
    landing.wait_for_function(
        "document.querySelector('#mon-stat').textContent.includes('could not')",
        timeout=60_000)
    # ...and the reason is shown rather than a blank panel
    assert landing.text_content("#monalerts").strip()
    # This test deliberately provokes a 400, which the browser logs as a failed
    # resource. Leaving it in the shared error list would make the console-error
    # test fail on an error the suite asked for on purpose.
    landing.errors[:] = [e for e in landing.errors if "400" not in e]


def test_ui_no_console_errors(page):
    """Runs last: by now the page has loaded, queried, dragged and scanned."""
    assert page.errors == [], page.errors
