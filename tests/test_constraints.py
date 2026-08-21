"""The constraints page must never flatter the database.

This page exists to publish a measurement rather than a claim, which only means
anything if it is genuinely allowed to disagree with us. So the tests here are
mostly about honesty: a probe that could not run must say so instead of being
dropped, a contradicted prediction must be reported as a surprise rather than
quietly relabelled, and the page must not hardcode the answers it renders.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import constraints                                             # noqa: E402
import probe_constraints                                       # noqa: E402
from hydra import Hydra                                        # noqa: E402

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


@pytest.fixture(scope="module")
def report():
    h = Hydra()
    try:
        h.query("MATCH (p:Package) RETURN count(*)")
    except Exception as exc:
        pytest.skip(f"HydraDB not reachable: {exc}")
    return constraints.probe_all()


# ==========================================================================
# the evidence is shared with the README, not duplicated
# ==========================================================================

def test_the_probe_set_is_imported_not_copied():
    """If this page and probe_constraints.py held separate copies of the query
    list they would drift, and the page would start describing a database
    nobody had actually probed."""
    src = open(os.path.join(os.path.dirname(WEB), "constraints.py"),
               encoding="utf-8").read()
    assert "import probe_constraints" in src
    assert "probe_constraints.TESTS" in src
    # ...and no second literal copy of the query list
    assert src.count("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:Package") <= 1


def test_every_probe_is_reported(report):
    surface = sum(len(g["rows"]) for g in report["groups"])
    assert surface == len(probe_constraints.TESTS)
    assert report["summary"]["probes"] == surface + len(report["traps"])


# ==========================================================================
# honesty
# ==========================================================================

def test_a_contradicted_prediction_is_reported_as_a_surprise(report):
    """Every row whose observation disagrees with the documented expectation
    must appear in `surprises`. Silently dropping one would turn the page into
    a page that can only ever agree with itself."""
    contradicted = [r for g in report["groups"] for r in g["rows"]
                    if r["holds"] is False]
    assert len(report["surprises"]) == len(contradicted)
    assert report["summary"]["surprises"] == len(contradicted)
    for r in contradicted:
        assert r["label"] in {s["label"] for s in report["surprises"]}


def test_an_unmeasured_expectation_is_not_scored(report):
    """A probe recorded as "?" was never a prediction, so it cannot be right or
    wrong — it must not be counted as either."""
    for g in report["groups"]:
        for r in g["rows"]:
            if r["expected"] not in ("WORKS", "FAILS"):
                assert r["holds"] is None
    scored = [r for g in report["groups"] for r in g["rows"] if r["holds"] is not None]
    assert report["summary"]["predictions"] == len(scored)


def test_a_probe_that_cannot_run_says_so_rather_than_vanishing():
    """An omitted row reads as a passing one."""
    row = constraints._unavailable("some probe", "connection refused")
    assert row["unavailable"] is True
    assert row["holds"] is None                 # never True, never False
    assert "connection refused" in row["detail"]


def test_traps_carry_the_wrong_version_as_well_as_the_right_one(report):
    """The whole point is showing the failure, not describing it."""
    for trap in report["traps"]:
        assert trap["label"]
        if trap.get("unavailable"):
            continue
        assert trap["wrong"] and trap["right"]
        for side in (trap["wrong"], trap["right"]):
            assert side["query"] and side["result"] and side["reading"]


# ==========================================================================
# the traps themselves — these are the findings with teeth
# ==========================================================================

def test_the_id_only_match_trap_still_holds(report):
    """The most expensive bug in this project: an unknown package reported an
    empty blast radius, which reads as safe."""
    trap = next(t for t in report["traps"] if "id-only" in t["label"])
    if trap.get("unavailable"):
        pytest.skip(trap["detail"])
    assert trap["holds"] is True, (
        "HydraDB now says 'no' to an unknown id — genuinely good news, but the "
        "README and the label scoping in blast.py should be revisited")
    assert "null" in trap["wrong"]["result"]
    assert trap["right"]["result"].startswith("0 row")


def test_count_semantics_are_what_the_histogram_depends_on(report):
    """The depth histogram differences cumulative counts. That is only exact
    because count(*) counts reachable vertices rather than paths, so this is
    load-bearing rather than trivia."""
    trap = next(t for t in report["traps"] if "count(*)" in t["label"])
    if trap.get("unavailable"):
        pytest.skip(trap["detail"])
    assert trap["holds"] is True, (
        "count(*) no longer counts distinct vertices — the depth histogram in "
        "blast.py is wrong if this fails")
    assert "3" in trap["right"]["result"]


def test_the_readonly_trap_reports_which_way_round_it_is(report):
    """This is the one where the trap *not* holding is the good news, so the
    row has to carry the observation rather than a pass/fail."""
    trap = next(t for t in report["traps"] if "read-only" in t["label"])
    assert "writable" in trap
    assert trap["holds"] is not trap["writable"]


def test_probes_clean_up_after_themselves(report):
    """A page that leaves probe vertices behind would slowly pollute the graph
    it is measuring — and traversal cost here scales with total store size."""
    h = Hydra()
    for offset in (1, 2, 3):
        rows = h.query("MATCH (p:_Probe {id: $id}) RETURN p.id",
                       {"id": constraints.BASE_ID + offset})
        assert not rows, f"probe vertex {constraints.BASE_ID + offset} was left behind"
    for name in ("_probe_a", "_probe_b"):
        rows = h.query("MATCH (p:Package {name: $n}) RETURN p.id", {"n": name})
        assert not rows, f"{name} was left behind"


# ==========================================================================
# caching — a sweep is a minute of writes
# ==========================================================================

def test_a_cold_cache_returns_immediately_rather_than_blocking():
    """A full sweep takes about a minute. Blocking the first page load on it
    would be a poor way to make a point about latency."""
    saved = dict(constraints._cache)
    try:
        constraints._cache.update(value=None, at=0.0, measuring=True, error="")
        t0 = time.perf_counter()
        out = constraints.cached()
        assert (time.perf_counter() - t0) < 1.0
        assert out["ready"] is False
        assert out["measuring"] is True
        assert out["message"]
    finally:
        constraints._cache.update(saved)


def test_a_stale_reading_is_labelled_stale():
    saved = dict(constraints._cache)
    try:
        constraints._cache.update(
            value={"summary": {}, "groups": [], "traps": [], "narrative": []},
            at=time.time() - (constraints.TTL + 60), measuring=False, error="")
        out = constraints.cached()
        assert out["ready"] is True
        assert out["stale"] is True
        assert out["age_s"] > constraints.TTL
    finally:
        constraints._cache.update(saved)


# ==========================================================================
# the page itself
# ==========================================================================

def test_the_page_hardcodes_no_results():
    """Every number on the page must come from the sweep. A literal PASS or a
    baked-in count would survive the database changing underneath it."""
    html = open(os.path.join(WEB, "constraints.html"), encoding="utf-8").read()
    for leaked in ("SURPRISE", "WORKS", "FAILS", "1024 rows returned"):
        assert leaked not in html, f"{leaked!r} is baked into the markup"


def test_the_page_ships_its_own_stylesheet():
    """Its own file, so it cannot collide with style.css and can be removed in
    one move if it ever stops being true."""
    assert os.path.exists(os.path.join(WEB, "constraints.css"))
    html = open(os.path.join(WEB, "constraints.html"), encoding="utf-8").read()
    assert "/constraints.css" in html and "/constraints.js" in html


def test_the_page_renders_the_live_sweep():
    """Browser layer: the page must fill from /api/constraints and survive the
    'still measuring' state without throwing.

    Run on its own thread. test_all.py holds a session-scoped sync Playwright
    open for the whole run, and the sync API refuses to start twice in one
    thread — a fresh thread gets a fresh event loop, which keeps this test
    self-contained instead of requiring the other module to be refactored.
    """
    requests = pytest.importorskip("requests")
    base = os.environ.get("BLAST_BASE", "http://127.0.0.1:8000")
    try:
        requests.get(f"{base}/api/constraints", timeout=10).raise_for_status()
    except Exception as exc:
        pytest.skip(f"server not running at {base}: {exc}")
    pytest.importorskip("playwright.sync_api",
                        reason="playwright not installed")

    outcome = {}

    def run():
        from playwright.sync_api import sync_playwright
        errors = []
        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(channel="chrome", headless=True)
                except Exception as exc:
                    outcome["skip"] = f"no Chrome for Playwright: {exc}"
                    return
                try:
                    pg = browser.new_page(viewport={"width": 1500, "height": 1100})
                    pg.on("pageerror", lambda e: errors.append(str(e)))
                    pg.on("console", lambda m: errors.append(m.text)
                          if m.type == "error" else None)
                    pg.goto(f"{base}/constraints", wait_until="domcontentloaded")
                    pg.wait_for_selector(".trap", timeout=120_000)
                    outcome["traps"] = pg.eval_on_selector_all(".trap", "e => e.length")
                    outcome["rows"] = pg.eval_on_selector_all(".grow", "e => e.length")
                    outcome["cards"] = pg.eval_on_selector_all(".cb", "e => e.length")
                    outcome["stat"] = pg.text_content("#cstatline")
                    outcome["badges"] = pg.eval_on_selector_all(
                        ".rbadge", "e => e.map(x => x.textContent.trim())")
                    outcome["errors"] = errors
                finally:
                    browser.close()
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=run, name="constraints-browser")
    t.start()
    t.join(timeout=200)
    assert not t.is_alive(), "browser test hung"

    if outcome.get("skip"):
        pytest.skip(outcome["skip"])
    assert "error" not in outcome, outcome.get("error")

    assert outcome["traps"] >= 4
    assert outcome["rows"] >= 15
    assert outcome["cards"] == 4
    # the header reports a real measurement age, not a placeholder
    assert "probes" in outcome["stat"]
    # and every badge is a state the sweep can actually produce
    assert set(outcome["badges"]) <= {"WORKS", "FAILS", "SURPRISE"}, outcome["badges"]
    assert outcome["errors"] == [], outcome["errors"]
