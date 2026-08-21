"""The real-time layer: multi-ecosystem project files, and alert routing.

Two things are being pinned here.

**Precision.** A lockfile is a resolved tree and a manifest is not, and the
difference decides whether an answer is complete or merely as complete as our
crawl. Every parser reports which one it was handed, and a range is never
recorded as a resolved version.

**Routing.** Alerts are routed by traversing HydraDB from the package that just
published to the projects that install it. The tests that need the graph skip
cleanly when it is absent, but when it is there they assert the thing that
actually matters: a project that does not depend on a compromised package gets
*no* alert, and one that does gets a critical one.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lockfiles                                               # noqa: E402
from hydra import Hydra, HydraError, nid, pkg_id               # noqa: E402

# ==========================================================================
# project files — five ecosystems, fourteen formats
# ==========================================================================

REQUIREMENTS_PINNED = """
# runtime
requests==2.31.0
urllib3==2.0.7
Django[argon2]==4.2.7 ; python_version >= "3.8"
-r constraints.txt
"""

REQUIREMENTS_RANGED = "requests>=2.28\nflask~=2.3.0\n"

POETRY_LOCK = """
[[package]]
name = "requests"
version = "2.31.0"
python-versions = ">=3.7"

[[package]]
name = "urllib3"
version = "2.0.7"
python-versions = ">=3.7"
"""

UV_LOCK = """
version = 1

[[package]]
name = "httpx"
version = "0.27.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/x.whl" }]
"""

PIPFILE_LOCK = json.dumps({
    "_meta": {"hash": {"sha256": "x"}},
    "default": {"requests": {"version": "==2.31.0"}},
    "develop": {"pytest": {"version": "==8.0.0"}},
})

CARGO_LOCK = """
version = 3

[[package]]
name = "serde"
version = "1.0.197"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aa"

[[package]]
name = "tokio"
version = "1.36.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "bb"
"""

GO_SUM = """
github.com/gorilla/mux v1.8.0 h1:i40aqfkR1h2SlN9hojwV5ZA91wcXFOvkdNIeFDP5koI=
github.com/gorilla/mux v1.8.0/go.mod h1:DVbg23sWSpFRCP0SfiEN6jmj59UnW/n46BH5rLB71So=
github.com/stretchr/testify v1.8.4 h1:CcVxjf4T2CAHzgZ3pRpwpen0gUWTvt0=
"""

GO_MOD = """
module github.com/example/app

go 1.21

require (
    github.com/gorilla/mux v1.8.0
    github.com/sirupsen/logrus v1.9.3 // indirect
)
"""

POM = """<?xml version="1.0"?>
<project>
  <properties><jackson.version>2.17.0</jackson.version></properties>
  <dependencies>
    <dependency><groupId>com.fasterxml.jackson.core</groupId>
      <artifactId>jackson-databind</artifactId>
      <version>${jackson.version}</version></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId>
      <version>4.13.2</version><scope>test</scope></dependency>
  </dependencies>
</project>"""

GRADLE_LOCKFILE = """
# This is a Gradle generated file for dependency locking.
com.google.guava:guava:33.0.0-jre=compileClasspath,runtimeClasspath
org.slf4j:slf4j-api:2.0.9=runtimeClasspath
empty=annotationProcessor
"""

NPM_LOCK = json.dumps({
    "name": "app", "lockfileVersion": 3,
    "packages": {"": {"name": "app"},
                 "node_modules/express": {"name": "express", "version": "4.18.2"}},
})

PACKAGE_JSON = json.dumps({
    "name": "app", "dependencies": {"express": "^4.18.2", "debug": "4.3.4"}})


@pytest.mark.parametrize("filename,text,ecosystem,precision", [
    ("requirements.txt", REQUIREMENTS_PINNED, "pypi", "exact"),
    ("requirements.txt", REQUIREMENTS_RANGED, "pypi", "inferred"),
    ("poetry.lock", POETRY_LOCK, "pypi", "exact"),
    ("uv.lock", UV_LOCK, "pypi", "exact"),
    ("Pipfile.lock", PIPFILE_LOCK, "pypi", "exact"),
    ("Cargo.lock", CARGO_LOCK, "crates", "exact"),
    ("go.sum", GO_SUM, "go", "exact"),
    ("go.mod", GO_MOD, "go", "inferred"),
    ("pom.xml", POM, "maven", "inferred"),
    ("gradle.lockfile", GRADLE_LOCKFILE, "maven", "exact"),
    ("package-lock.json", NPM_LOCK, "npm", "exact"),
    ("package.json", PACKAGE_JSON, "npm", "inferred"),
])
def test_project_files_route_to_the_right_ecosystem(filename, text,
                                                    ecosystem, precision):
    resolved, _kind, eco, prec = lockfiles.parse_project(text, filename=filename)
    assert eco == ecosystem
    assert prec == precision
    assert resolved


def test_a_range_is_never_recorded_as_a_resolved_version():
    """The same rule as `satisfies`: listing a dependency and pulling a specific
    version are different facts. A ranged requirement gets an empty version, not
    the range text masquerading as one."""
    resolved, _k, _e, precision = lockfiles.parse_project(
        REQUIREMENTS_RANGED, filename="requirements.txt")
    assert precision == "inferred"
    assert resolved["requests"] == ""
    assert resolved["flask"] == ""

    pinned, _k, _e, precision = lockfiles.parse_project(
        REQUIREMENTS_PINNED, filename="requirements.txt")
    assert precision == "exact"
    assert pinned["requests"] == "2.31.0"


def test_pom_drops_test_scope():
    """A test-scoped JUnit is not on anybody else's classpath, so it is not in
    anybody else's blast radius."""
    resolved, _k, _e, _p = lockfiles.parse_project(POM, filename="pom.xml")
    assert "com.fasterxml.jackson.core:jackson-databind" in resolved
    assert "junit:junit" not in resolved
    # the property really was substituted rather than stored raw
    assert resolved["com.fasterxml.jackson.core:jackson-databind"] == "2.17.0"


def test_go_sum_ignores_the_go_mod_hash_lines():
    """Every module appears twice in a go.sum — once for the module and once for
    its go.mod. Counting both double-counts every dependency."""
    resolved, _k, _e, _p = lockfiles.parse_project(GO_SUM, filename="go.sum")
    assert resolved == {"github.com/gorilla/mux": "v1.8.0",
                        "github.com/stretchr/testify": "v1.8.4"}


@pytest.mark.parametrize("text,expected", [
    (CARGO_LOCK, "crates"),
    (UV_LOCK, "pypi"),
    (POETRY_LOCK, "pypi"),
    (GO_SUM, "go"),
    (GO_MOD, "go"),
    (POM, "maven"),
    (PIPFILE_LOCK, "pypi"),
    (NPM_LOCK, "npm"),
])
def test_content_sniffing_without_a_filename(text, expected):
    """Uploads frequently arrive with no name. Cargo.lock, poetry.lock and
    uv.lock are all `[[package]]` TOML, so getting this wrong would watch
    packages that do not exist in the ecosystem it picked."""
    eco, _kind, _precision = lockfiles.detect_project(text)
    assert eco == expected


@pytest.mark.parametrize("text", ["", "   ", "just some prose", "<<<>>>"])
def test_unparseable_input_raises_rather_than_guessing(text):
    with pytest.raises(lockfiles.LockfileError):
        lockfiles.parse_project(text, filename="mystery.txt")


# ==========================================================================
# alert routing — needs a writable graph
# ==========================================================================

@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    watch = pytest.importorskip("watch")
    h = Hydra()
    try:
        h.query("MATCH (p:Package) RETURN count(*)")
    except Exception as exc:
        pytest.skip(f"HydraDB not reachable: {exc}")
    probe = 999999999999996
    try:
        h.query("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:_Probe",
                {"rows": [{"id": probe}]}, retries=1)
        h.query("MATCH (p {id: $id}) DETACH DELETE p", {"id": probe}, retries=1)
    except HydraError as exc:
        pytest.skip(f"graph is read-only (run `py rebuild.py`): {str(exc)[:100]}")

    db = tmp_path_factory.mktemp("watch") / "watch.db"
    return watch.Registry(hydra=h, db_path=str(db))


@pytest.fixture
def project(registry):
    created = []

    def make(name, resolved, ecosystem="npm", **kw):
        p = registry.register(name, resolved, ecosystem, **kw)
        created.append(p)
        return p

    yield make
    for p in created:
        try:
            registry.unregister(p["project_id"], p["token"])
        except Exception:
            pass


def test_a_publish_routes_only_to_projects_that_install_it(registry, project):
    """The single most important property of the router.

    Over-alerting is how a monitoring tool gets muted, and a muted tool is
    worth nothing during the incident it was bought for.
    """
    victim = project("victim", {"ua-parser-js": "0.7.29", "express": "4.18.2"})
    bystander = project("bystander", {"lodash": "4.17.21"})

    alerts = registry.route({
        "id": pkg_id("ua-parser-js", "npm"), "ecosystem": "npm",
        "name": "ua-parser-js", "version": "0.7.29", "deps": 0, "maintainers": []})

    hit = {a["project_id"] for a in alerts}
    assert victim["project_id"] in hit
    assert bystander["project_id"] not in hit


def test_a_known_malicious_release_is_critical(registry, project):
    """ua-parser-js 0.7.29 is a real compromise with a real OSV advisory. If
    this comes back `info`, the severity path is broken and every alert is
    worthless."""
    project("compromised", {"ua-parser-js": "0.7.29"})
    alerts = registry.route({
        "id": pkg_id("ua-parser-js", "npm"), "ecosystem": "npm",
        "name": "ua-parser-js", "version": "0.7.29", "deps": 0, "maintainers": []})
    assert alerts, "a project that installs it must be alerted"
    a = alerts[0]
    assert a["severity"] == "critical"
    assert a["kind"] == "malware"
    assert a["detail"]["advisories"], "the advisory itself must travel with the alert"


def test_a_routine_publish_is_info_not_an_incident(registry, project):
    """A new version of something clean is still the event you want to see —
    it is the only warning that exists before an advisory does — but calling it
    critical would make critical meaningless."""
    # ms@2.1.3 has no OSV advisory. express does, at every version anyone
    # actually runs, which is exactly why it is the wrong package to assert
    # "clean" against.
    project("routine", {"ms": "2.1.3"})
    alerts = registry.route({
        "id": pkg_id("ms", "npm"), "ecosystem": "npm",
        "name": "ms", "version": "2.1.3", "deps": 0, "maintainers": []})
    assert alerts
    assert alerts[0]["severity"] == "info"
    assert alerts[0]["kind"] == "publish"


def test_a_publish_carrying_a_real_advisory_is_not_info(registry, project):
    """The other half of the same rule. express 4.18.2 has two genuine
    advisories, so a publish of it must outrank a routine release."""
    project("has-advisories", {"express": "4.18.2"})
    alerts = registry.route({
        "id": pkg_id("express", "npm"), "ecosystem": "npm",
        "name": "express", "version": "4.18.2", "deps": 30, "maintainers": []})
    assert alerts
    assert alerts[0]["kind"] == "advisory"
    assert alerts[0]["severity"] != "info"
    assert alerts[0]["detail"]["advisories"]


def test_exact_projects_do_not_traverse(registry, project):
    """A lockfile is the whole installed tree, so depth 1 is complete. Going
    deeper would alert on packages the project does not have installed."""
    p = project("locked", {"express": "4.18.2"}, precision="exact")
    assert p["depth"] == 1
    row = registry.db.execute("SELECT * FROM projects WHERE id = ?",
                              (p["project_id"],)).fetchone()
    assert row["precision"] == "exact"


def test_inferred_projects_reach_transitively(registry, project):
    """A manifest names only direct dependencies, so the graph has to supply
    the rest — and the hop count has to come back with the alert."""
    p = project("manifest-only", {"express": "4.18.2"},
                precision="inferred", depth=3)
    assert p["depth"] == 3
    alerts = registry.route({
        "id": pkg_id("body-parser", "npm"), "ecosystem": "npm",
        "name": "body-parser", "version": "1.20.1", "deps": 0, "maintainers": []})
    mine = [a for a in alerts if a["project_id"] == p["project_id"]]
    if not mine:
        pytest.skip("express -> body-parser edge is not in this crawl")
    assert mine[0]["hops"] >= 2, "a transitive hit must not report as direct"
    assert mine[0]["precision"] == "inferred"


def test_tokens_are_required_and_compared_safely(registry, project):
    p = project("private", {"express": "4.18.2"})
    assert registry.authenticate(p["project_id"], p["token"])
    assert registry.authenticate(p["project_id"], "") is None
    assert registry.authenticate(p["project_id"], p["token"][:-1] + "x") is None
    assert registry.authenticate("no-such-project", p["token"]) is None


def test_unregister_removes_the_project_from_the_graph(registry):
    p = registry.register("temporary", {"express": "4.18.2"}, "npm")
    vid = nid(f"proj:{p['project_id']}")
    assert registry.hydra.query("MATCH (n:Project {id: $i}) RETURN n.pid",
                                {"i": vid})
    assert registry.unregister(p["project_id"], p["token"]) is True
    assert not registry.hydra.query("MATCH (n:Project {id: $i}) RETURN n.pid",
                                    {"i": vid})
    # ...and stops routing to it
    alerts = registry.route({
        "id": pkg_id("express", "npm"), "ecosystem": "npm", "name": "express",
        "version": "4.18.2", "deps": 0, "maintainers": []})
    assert p["project_id"] not in {a["project_id"] for a in alerts}


def test_registering_nothing_is_refused(registry):
    with pytest.raises(ValueError):
        registry.register("empty", {}, "npm")


# ==========================================================================
# the ingestion daemon's own bookkeeping
# ==========================================================================

def test_status_never_invents_a_timestamp():
    """An ecosystem that has never seen a publish must say so. A monitoring
    tool that reports a plausible last-seen time it did not observe is worse
    than one that reports nothing."""
    import live
    st = live.EcosystemState("npm")
    snap = st.snapshot()
    assert snap["last_event_at"] is None
    assert snap["seconds_since_event"] is None
    assert snap["state"] == "starting"
    assert snap["events_seen"] == 0


def test_backoff_is_visible_in_status():
    import live
    st = live.EcosystemState("pypi")
    st.polls = 3
    st.consecutive_errors = 2
    st.backoff_until = __import__("time").time() + 30
    snap = st.snapshot()
    assert snap["state"] == "backoff"
    assert 0 < snap["backoff_seconds"] <= 30


def test_a_degraded_feed_does_not_report_live():
    import live
    st = live.EcosystemState("crates")
    st.polls = 10
    st.consecutive_errors = 1
    assert st.snapshot()["state"] == "degraded"


def test_the_cooldown_stops_refetching_the_same_publish():
    """Every registry feed returns its last N publishes on every poll, so
    without a cooldown each poll re-fetches everything the previous one did."""
    import live
    r = live._Recent(cap=3)
    assert r.seen_recently("npm:debug") is False
    r.mark("npm:debug")
    assert r.seen_recently("npm:debug") is True
    for n in ("a", "b", "c"):
        r.mark(n)
    assert r.seen_recently("npm:debug") is False       # evicted by the cap


def test_sidecar_keys_keep_ecosystems_apart():
    """`packages.name` is UNIQUE and predates multi-ecosystem support. PyPI's
    `requests` and npm's `requests` are unrelated packages and must not collide
    into one row."""
    import live
    assert live.qualified("npm", "requests") == "requests"
    assert live.qualified("pypi", "requests") == "pypi:requests"
    assert live.qualified("npm", "requests") != live.qualified("pypi", "requests")
    assert pkg_id("requests", "npm") != pkg_id("requests", "pypi")


# ==========================================================================
# single-flight — concurrent identical traversals share one walk
# ==========================================================================

def test_single_flight_runs_the_work_once():
    """Twenty-four concurrent requests for the same hub package used to become
    twenty-four concurrent traversals, saturate HydraDB, and 503 all of them."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import apimeta

    flight = apimeta.SingleFlight()
    calls = []
    started = threading.Event()

    def produce():
        calls.append(1)
        started.set()
        time.sleep(0.4)          # long enough for the followers to pile up
        return "answer"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: flight.run("k", produce), range(12)))

    assert all(value == "answer" for value, _shared in results)
    assert len(calls) == 1, f"produce ran {len(calls)} times, expected once"
    assert flight.executed == 1
    assert flight.coalesced == 11


def test_single_flight_shares_the_failure_too():
    """A follower must not be told the work succeeded because it was cheap to
    say so. It waited on that call; it gets that call's outcome."""
    from concurrent.futures import ThreadPoolExecutor

    import apimeta

    flight = apimeta.SingleFlight()

    def boom():
        time.sleep(0.3)
        raise RuntimeError("hydra is down")

    def attempt(_):
        try:
            flight.run("k", boom)
            return "ok"
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        out = list(pool.map(attempt, range(6)))
    assert all(o == "hydra is down" for o in out), out


def test_different_keys_do_not_coalesce():
    import apimeta
    flight = apimeta.SingleFlight()
    a, _ = flight.run("a", lambda: 1)
    b, _ = flight.run("b", lambda: 2)
    assert (a, b) == (1, 2)
    assert flight.executed == 2
    assert flight.coalesced == 0
