"""End-to-end verification of the running system, against real data.

This is not a unit test suite — tests/test_all.py is that. This drives the
actually-running stack the way a user does, many times, and reports a measured
success rate and latency distribution per endpoint. Nothing here is mocked:
every package name is read out of the live graph, and every assertion is made
against what the server really returned.

  py verify.py                 # one full pass
  py verify.py --loops 5       # sustained: repeat the whole pass 5 times
  py verify.py --soak 300      # hammer the API for 300s and report the rate

Exit code is 0 only if every check passed.
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import blast
from hydra import Hydra, nid

BASE = os.environ.get("BLAST_BASE", "http://127.0.0.1:8000")
ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "deps.db")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

# The three chips on the landing page. If any of these fail, the first thing a
# judge clicks is broken, so they are checked explicitly rather than sampled.
PRESETS = [("debug", "4.4.2"), ("event-stream", "3.3.6"), ("ua-parser-js", "0.7.29")]

# The Windows console is cp1252 and package data is full of characters it
# cannot encode; without this a stray arrow in a page string aborts the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Results:
    def __init__(self):
        self.checks: list[tuple[str, bool, str]] = []
        self.timings: dict[str, list[float]] = {}

    def check(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), detail))
        mark = f"{GREEN}PASS{OFF}" if ok else f"{RED}FAIL{OFF}"
        print(f"  [{mark}] {name}" + (f"  {DIM}{detail}{OFF}" if detail else ""))
        return ok

    def time(self, label, ms):
        self.timings.setdefault(label, []).append(ms)

    @property
    def failed(self):
        return [c for c in self.checks if not c[1]]

    def summary(self):
        total, bad = len(self.checks), len(self.failed)
        print(f"\n{'=' * 74}")
        if self.timings:
            print(f"{'endpoint':<34}{'n':>5}{'p50':>9}{'p95':>9}{'max':>9}")
            print("-" * 74)
            for label, xs in sorted(self.timings.items()):
                xs = sorted(xs)
                p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
                print(f"{label:<34}{len(xs):>5}{statistics.median(xs):>8.0f}ms"
                      f"{p95:>8.0f}ms{max(xs):>8.0f}ms")
            print("-" * 74)
        rate = 100.0 * (total - bad) / total if total else 0.0
        colour = GREEN if bad == 0 else RED
        print(f"{colour}{total - bad}/{total} checks passed "
              f"({rate:.1f}%){OFF}")
        if bad:
            print(f"\n{RED}failures:{OFF}")
            for name, _, detail in self.failed:
                print(f"  - {name}: {detail}")
        return bad == 0


def get(path, **params):
    t0 = time.perf_counter()
    r = requests.get(f"{BASE}{path}", params=params, timeout=120)
    return r, (time.perf_counter() - t0) * 1000.0


def post(path, body, **params):
    t0 = time.perf_counter()
    r = requests.post(f"{BASE}{path}", params=params, data=body, timeout=120)
    return r, (time.perf_counter() - t0) * 1000.0


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

def check_infrastructure(res):
    print(f"\n{YELLOW}infrastructure{OFF}")
    try:
        h = Hydra()
        rows = h.query("MATCH (p:Package) RETURN count(*)")
        n = rows[0]["count(*)"] if rows else 0
        res.check("hydradb answers a query", n > 0, f"{n:,} packages")
    except Exception as e:
        res.check("hydradb answers a query", False, str(e)[:150])
        return None
    ok = os.path.exists(DB_PATH)
    db = sqlite3.connect(DB_PATH, timeout=10) if ok else None
    if db:
        n = db.execute("SELECT count(*) FROM packages").fetchone()[0]
        res.check("sidecar has rows", n > 0, f"{n:,} package rows")
        coll = db.execute("SELECT count(*) FROM collisions").fetchone()[0]
        res.check("no nid collisions", coll == 0, f"{coll} recorded")
    else:
        res.check("sidecar present", False, "deps.db missing")
    try:
        r, ms = get("/api/stats")
        res.check("server is serving", r.status_code == 200, f"{ms:.0f}ms")
    except Exception as e:
        res.check("server is serving", False, str(e)[:150])
    return db


def check_consistency(res, db):
    """The two stores must agree, or every number on the page is suspect."""
    print(f"\n{YELLOW}store consistency{OFF}")
    # The graph count in /api/stats is a cached background measurement — a full
    # count takes seconds, so the header cannot block on it. Comparing that
    # cached value against a live sidecar read used to be fine and is not any
    # more: continuous ingestion writes to both, so the two numbers are simply
    # taken at different instants and drift by whatever landed in between.
    # Measuring the graph here, beside the sidecar read, makes this a
    # consistency check again rather than a comparison of two clocks.
    h = Hydra()
    try:
        graph_packages = h.query("MATCH (p:Package) RETURN count(*)")[0]["count(*)"]
    except Exception as exc:
        res.check("graph counts measured", False, f"{type(exc).__name__}: {exc}")
        return
    conn = sqlite3.connect(DB_PATH, timeout=15)
    sidecar_packages = conn.execute("SELECT count(*) FROM packages").fetchone()[0]
    conn.close()

    # Ingestion writes the sidecar row first and the vertex immediately after,
    # so a few rows are legitimately in flight at any instant. A real
    # divergence is orders of magnitude larger than that.
    drift = abs(graph_packages - sidecar_packages)
    allowed = max(50, sidecar_packages // 1000)
    res.check("graph vertex count tracks sidecar package count",
              drift <= allowed,
              f"graph {graph_packages:,} vs sidecar {sidecar_packages:,} "
              f"(drift {drift}, allowed {allowed})")


def check_presets(res):
    """Exactly what happens when someone clicks the three chips."""
    print(f"\n{YELLOW}landing-page presets{OFF}")
    for name, version in PRESETS:
        r, ms = get("/api/blast", name=name, depth=5)
        res.time("GET /api/blast", ms)
        if not res.check(f"blast {name}", r.status_code == 200,
                         f"HTTP {r.status_code} {r.text[:80]}"):
            continue
        d = r.json()
        res.check(f"blast {name} returns a real radius", d["total"] > 0,
                  f"{d['total']:,} exposed in {d['latency_ms']:.0f}ms")
        res.check(f"blast {name} list matches count",
                  d["truncated"] or len(d["victims"]) == d["total"],
                  f"{len(d['victims'])} listed vs {d['total']} counted")
        res.check(f"blast {name} histogram sums to total",
                  sum(h["packages"] for h in d["histogram"]) == d["total"])

        r, ms = get("/api/resolve", name=name, bad_version=version)
        res.time("GET /api/resolve", ms)
        if res.check(f"resolve {name}@{version}", r.status_code == 200,
                     f"HTTP {r.status_code}"):
            d = r.json()
            res.check(f"resolve {name} split is sound",
                      all(any(blast.satisfies(version, x) for x in e["ranges"])
                          for e in d["exposed"]),
                      f"{d['exposed_count']} exposed / "
                      f"{d['shielded_count']} pinned of {d['checked']} ranges")

        r, ms = get("/api/maintainers", name=name)
        res.time("GET /api/maintainers", ms)
        res.check(f"maintainers {name}", r.status_code == 200,
                  f"{r.json().get('sibling_count', 0)} siblings")


def check_sampled_packages(res, db, n):
    """Real names pulled from the graph, not a curated list."""
    print(f"\n{YELLOW}sampled packages (n={n}){OFF}")
    names = [r[0] for r in db.execute(
        "SELECT name FROM packages WHERE crawled = 1 "
        "ORDER BY nid LIMIT ? OFFSET 17", (n,))]
    bad = []
    for name in names:
        r, ms = get("/api/blast", name=name, depth=5)
        res.time("GET /api/blast", ms)
        if r.status_code != 200:
            bad.append(f"{name} -> {r.status_code}")
            continue
        d = r.json()
        if not (d["truncated"] or len(d["victims"]) == d["total"]):
            bad.append(f"{name} list/count mismatch")
        if sum(h["packages"] for h in d["histogram"]) != d["total"]:
            bad.append(f"{name} histogram mismatch")
    res.check(f"all {n} sampled packages answered correctly", not bad,
              "; ".join(bad[:4]) if bad else f"{n}/{n} clean")


def check_map(res):
    """The drawable slice, for each preset — this is what the map renders."""
    print(f"\n{YELLOW}blast map{OFF}")
    for name, _ in PRESETS:
        r, ms = get("/api/subgraph", name=name, depth=3)
        res.time("GET /api/subgraph", ms)
        if not res.check(f"subgraph {name}", r.status_code == 200,
                         f"HTTP {r.status_code}"):
            continue
        d = r.json()
        names = {n["name"] for n in d["nodes"]}
        depth_of = {n["name"]: n["depth"] for n in d["nodes"]}
        res.check(f"subgraph {name} is drawable",
                  bool(names) and all(e["from"] in names and e["to"] in names
                                      for e in d["edges"]),
                  f"{len(d['nodes'])} nodes, {len(d['edges'])} edges "
                  f"of {d['total_exposed']:,} exposed")
        orphans = [n["name"] for n in d["nodes"] if n["depth"] > 0
                   and not any(e["to"] == n["name"]
                               and depth_of[e["from"]] == n["depth"] - 1
                               for e in d["edges"])]
        res.check(f"subgraph {name} has no floating nodes", not orphans,
                  "; ".join(orphans[:3]))


def check_events(res):
    """Server-sent events: a real frame, not just a 200."""
    print(f"\n{YELLOW}live event stream{OFF}")
    try:
        t0 = time.perf_counter()
        with requests.get(f"{BASE}/api/events", stream=True, timeout=30) as r:
            ok = r.status_code == 200 and "text/event-stream" in r.headers.get(
                "content-type", "")
            payload = None
            for raw in r.iter_lines(decode_unicode=True):
                if raw and raw.startswith("data: "):
                    payload = json.loads(raw[6:])
                    break
                if time.perf_counter() - t0 > 20:
                    break
        res.time("GET /api/events (first frame)", (time.perf_counter() - t0) * 1000)
        res.check("event stream opens", ok)
        res.check("event stream pushes a real frame",
                  bool(payload) and payload.get("packages", 0) > 0,
                  f"{payload.get('packages', 0):,} packages, "
                  f"warmup={payload.get('warmup')}" if payload else "no frame")
    except Exception as e:
        res.check("event stream opens", False, f"{e.__class__.__name__}: {e}"[:150])


def check_typosquats(res):
    print(f"\n{YELLOW}typosquat ring{OFF}")
    for name, _ in PRESETS:
        r, ms = get("/api/typosquats", name=name)
        res.time("GET /api/typosquats", ms)
        if not res.check(f"typosquats {name}", r.status_code == 200,
                         f"HTTP {r.status_code}"):
            continue
        d = r.json()
        res.check(f"typosquats {name} checked the registry", d["checked_live"],
                  f"{len(d['existing'])} of {d['candidates']} variants are real")


def check_search(res, db):
    print(f"\n{YELLOW}search{OFF}")
    for q in ("deb", "expr", "@types/", "react", "lodash"):
        r, ms = get("/api/search", q=q)
        res.time("GET /api/search", ms)
        ok = r.status_code == 200 and r.json()["results"]
        res.check(f"search {q!r}", ok,
                  f"{len(r.json().get('results', []))} hits")
    r, _ = get("/api/search", q="%")
    res.check("search escapes SQL wildcards", r.json()["results"] == [])


def check_lockfiles(res):
    print(f"\n{YELLOW}lockfile verdicts{OFF}")
    cases = [
        ("lock-v3.json", "debug", "2.6.9", "EXPOSED"),
        ("lock-v3.json", "debug", "9.9.9", "SHIELDED"),
        ("lock-v1.json", "debug", "2.6.9", "EXPOSED"),
        ("lock-clean.json", "debug", None, "CLEAR"),
    ]
    for fname, name, version, expect in cases:
        path = os.path.join(FIXTURES, fname)
        if not os.path.exists(path):
            res.check(f"lockfile {fname}", False, "fixture missing")
            continue
        params = {"name": name}
        if version:
            params["bad_version"] = version
        r, ms = post("/api/lockfile", open(path, "rb").read(), **params)
        res.time("POST /api/lockfile", ms)
        got = r.json().get("verdict") if r.status_code == 200 else r.status_code
        res.check(f"{fname} + {name}@{version or '-'} -> {expect}", got == expect,
                  f"got {got}")


def check_failure_modes(res):
    """Bad input must degrade cleanly. A 500 here is a real defect."""
    print(f"\n{YELLOW}failure modes{OFF}")
    r, _ = get("/api/blast", name="no-such-package-zzz-9182", depth=3)
    res.check("unknown package -> 404 with explanation",
              r.status_code == 404 and r.json().get("error") == "not_in_graph",
              f"HTTP {r.status_code}")
    r, _ = get("/api/blast")
    res.check("missing param -> 422", r.status_code == 422, f"HTTP {r.status_code}")
    for depth in (0, 99, -1):
        r, _ = get("/api/blast", name="debug", depth=depth)
        res.check(f"depth={depth} -> 422", r.status_code == 422,
                  f"HTTP {r.status_code}")
    r, _ = post("/api/lockfile", b"not json", name="debug")
    res.check("malformed lockfile -> 400", r.status_code == 400,
              f"HTTP {r.status_code}")
    r, _ = post("/api/lockfile", b"", name="debug")
    res.check("empty body -> 400", r.status_code == 400, f"HTTP {r.status_code}")
    r, _ = get("/api/blast", name="x" * 500)
    res.check("absurdly long name -> 422", r.status_code == 422,
              f"HTTP {r.status_code}")
    # Injection through the one field that reaches Cypher as text.
    r, _ = get("/api/blast", name="debug", depth="3; MATCH (n) DETACH DELETE n")
    res.check("depth injection attempt -> 422", r.status_code == 422,
              f"HTTP {r.status_code}")


def check_static(res):
    print(f"\n{YELLOW}console assets{OFF}")
    for path, needle in (("/", b"blast radius"), ("/app.js", b"draggable"),
                         ("/style.css", b"--paper"), ("/api/docs", b"swagger")):
        r, ms = get(path)
        res.time("GET static", ms)
        res.check(f"serves {path}",
                  r.status_code == 200 and needle.lower() in r.content.lower(),
                  f"HTTP {r.status_code} {len(r.content)}b")


def check_concurrency(res, db, workers, each):
    """Everything at once, the way a page load plus an impatient user does it."""
    print(f"\n{YELLOW}concurrency ({workers} workers x {each}){OFF}")
    names = [r[0] for r in db.execute(
        "SELECT dst FROM deps WHERE kind='prod' GROUP BY dst "
        "ORDER BY count(*) DESC LIMIT 8")]
    jobs = []
    for i in range(each):
        jobs.append(("/api/blast", {"name": names[i % len(names)], "depth": 5}))
        jobs.append(("/api/stats", {}))
        jobs.append(("/api/search", {"q": "re"}))
        jobs.append(("/api/maintainers", {"name": names[i % len(names)]}))

    failures, times = [], []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(get, p, **q): (p, q) for p, q in jobs}
        for f in as_completed(futs):
            path, q = futs[f]
            try:
                r, ms = f.result()
                times.append(ms)
                if r.status_code != 200:
                    failures.append(f"{path} -> {r.status_code}")
            except Exception as e:
                failures.append(f"{path} -> {e.__class__.__name__}")
    wall = time.perf_counter() - t0
    for t in times:
        res.time("concurrent (mixed)", t)
    res.check(f"{len(jobs)} concurrent requests all succeeded", not failures,
              f"{len(jobs) - len(failures)}/{len(jobs)} in {wall:.1f}s"
              + (f" — {failures[:3]}" if failures else ""))


def check_browser(res):
    print(f"\n{YELLOW}console in a real browser{OFF}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        res.check("playwright available", False, "not installed (skipping)")
        return
    errors = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(channel="chrome", headless=True)
            pg = b.new_page(viewport={"width": 1600, "height": 1100})
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(BASE, wait_until="domcontentloaded")
            pg.wait_for_selector("#peek-hist .skel", state="detached", timeout=60_000)
            # The histogram and the header are fed by different endpoints, so
            # the skeleton detaching says nothing about whether the header's
            # stats have landed. Waiting only on the first made every header
            # assertion below a race that it usually, but not always, won.
            pg.wait_for_function(
                "!document.querySelector('#statline').textContent.includes('connecting')",
                timeout=60_000)
            # ...and the hero preview is a third endpoint again, so it gets its
            # own wait rather than inheriting somebody else's timing.
            pg.wait_for_function(
                "/\\d/.test(document.querySelector('#peek-graph').textContent)",
                timeout=60_000)
            res.check("hero previews loaded from live queries",
                      any(c.isdigit() for c in pg.text_content("#peek-graph")))
            res.check("header shows live graph size",
                      "packages" in pg.text_content("#statline"))
            pg.click(".chip")
            pg.wait_for_function(
                "document.querySelector('#latency').textContent !== '—'",
                timeout=120_000)
            pg.wait_for_timeout(1200)
            res.check("query renders a latency figure",
                      pg.text_content("#latency").endswith("ms"),
                      pg.text_content("#latency"))
            widths = pg.eval_on_selector_all(
                ".hrow .fill", "els => els.map(e => getComputedStyle(e).width)")
            res.check("depth bars painted", any(w not in ("0px", "auto") for w in widths),
                      str(widths[:3]))
            res.check("victim list populated",
                      pg.eval_on_selector_all("#victims .r", "e => e.length") > 0)
            pg.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            pg.wait_for_timeout(300)
            # Every .win is draggable by its title bar. The .scatter container
            # these used to be measured through was removed in the landing
            # redesign. It has to be a *visible* window: the first .win in the
            # document sits inside the `hidden` results section and reports no
            # bounding box, so a drag aimed at it silently does nothing.
            win = pg.locator(".win:visible").first
            win.scroll_into_view_if_needed()
            pg.wait_for_timeout(400)
            before = win.evaluate("el => el.style.transform")
            bb = win.locator(".bar").first.bounding_box()
            pg.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            pg.mouse.down()
            pg.mouse.move(bb["x"] + bb["width"] / 2 + 100,
                          bb["y"] + bb["height"] / 2 + 60, steps=8)
            pg.mouse.up()
            after = win.evaluate("el => el.style.transform")
            res.check("windows drag", before != after, f"{before!r} -> {after!r}")
            lock = os.path.join(FIXTURES, "lock-v3.json")
            if os.path.exists(lock):
                pg.fill("#pkg", "debug")
                pg.fill("#ver", "2.6.9")
                pg.set_input_files("#file", lock)
                pg.wait_for_function(
                    "document.querySelector('#verdict .word')?.textContent"
                    "?.match(/EXPOSED|SHIELDED|CLEAR/)", timeout=120_000)
                res.check("lockfile drop renders a verdict",
                          pg.text_content("#verdict .word") == "EXPOSED",
                          pg.text_content("#verdict .word"))
            pg.wait_for_selector("#map .node", timeout=120_000)
            res.check("blast map drew nodes and edges",
                      pg.eval_on_selector_all("#map .node", "e => e.length") > 1
                      and pg.eval_on_selector_all("#map .edge", "e => e.length") > 0,
                      f'{pg.eval_on_selector_all("#map .node", "e => e.length")} nodes')
            box = pg.eval_on_selector(
                "#map",
                "el => { const b = el.getBBox();"
                " return [b.x, b.y, b.x + b.width, b.y + b.height]; }")
            res.check("map stays inside its viewBox",
                      box[0] >= -2 and box[1] >= -2 and box[2] <= 762 and box[3] <= 762,
                      str([round(v) for v in box]))
            res.check("status cards are live",
                      pg.eval_on_selector_all("#syscards .card", "e => e.length") >= 6)
            before = pg.input_value("#pkg")
            pg.eval_on_selector_all(
                "#map .node",
                "els => els.find(e => !e.classList.contains('root') &&"
                " !e.classList.contains('label')).dispatchEvent("
                "new MouseEvent('click', {bubbles: true}))")
            try:
                pg.wait_for_function(
                    "v => document.querySelector('#pkg').value !== v",
                    arg=before, timeout=60_000)
                pivoted = pg.input_value("#pkg")
            except Exception:
                pivoted = before
            res.check("clicking a node pivots the console", pivoted != before,
                      f"{before} -> {pivoted}")
            res.check("zero console errors", not errors, str(errors[:2]))
            b.close()
    except Exception as e:
        res.check("browser flow completed", False, f"{e.__class__.__name__}: {e}"[:200])


def soak(res, db, seconds):
    """Sustained real traffic. Reports the measured success rate over time."""
    print(f"\n{YELLOW}soak ({seconds}s){OFF}")
    names = [r[0] for r in db.execute(
        "SELECT name FROM packages WHERE crawled = 1 ORDER BY nid LIMIT 200")]
    sent = failed = 0
    times = []
    # Aggregated so the diagnosis survives scrollback: what failed, why, when,
    # and on which package — a bare failure count is not actionable.
    reasons: dict[str, int] = {}
    examples: dict[str, str] = {}
    first_fail_at = last_fail_at = None
    deadline = time.time() + seconds
    started = time.time()
    i = 0

    def record(path, q, status, body, name):
        nonlocal failed, first_fail_at, last_fail_at
        failed += 1
        key = f"{path} -> {status}"
        if isinstance(body, dict) and body.get("error"):
            key += f" ({body['error']})"
        reasons[key] = reasons.get(key, 0) + 1
        examples.setdefault(key, f"{name}: {str(body)[:160]}")
        now = time.time() - started
        if first_fail_at is None:
            first_fail_at = now
        last_fail_at = now

    while time.time() < deadline:
        name = names[i % len(names)]
        i += 1
        for path, q in (("/api/blast", {"name": name, "depth": 5}),
                        ("/api/stats", {}),
                        ("/api/search", {"q": name[:3]})):
            try:
                r, ms = get(path, **q)
                sent += 1
                times.append(ms)
                if r.status_code not in (200, 404):
                    try:
                        body = r.json()
                    except Exception:
                        body = r.text[:160]
                    record(path, q, r.status_code, body, name)
            except Exception as e:
                sent += 1
                record(path, q, e.__class__.__name__, None, name)
        if sent % 300 == 0:
            print(f"    {DIM}{sent} requests, {failed} failed, "
                  f"{int(deadline - time.time())}s left{OFF}")
    for t in times:
        res.time("soak (mixed)", t)
    rate = 100.0 * (sent - failed) / sent if sent else 0
    res.check(f"soak success rate over {sent} requests", failed == 0,
              f"{rate:.2f}% ({sent - failed}/{sent})")
    if reasons:
        print(f"    {RED}failure breakdown{OFF} "
              f"{DIM}(first at t+{first_fail_at:.0f}s, "
              f"last at t+{last_fail_at:.0f}s of {seconds}s){OFF}")
        for key, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"      {n:>5}x {key}")
            print(f"            {DIM}{examples[key]}{OFF}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loops", type=int, default=1)
    p.add_argument("--sample", type=int, default=25)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--each", type=int, default=6)
    p.add_argument("--soak", type=int, default=0, help="seconds of sustained load")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--soak-only", action="store_true",
                   help="skip the functional pass and just apply load")
    args = p.parse_args()

    overall = True
    for loop in range(args.loops):
        if args.loops > 1:
            print(f"\n{'#' * 74}\n# pass {loop + 1} of {args.loops}\n{'#' * 74}")
        res = Results()
        started = time.time()
        db = check_infrastructure(res)
        if db is None:
            res.summary()
            return 1
        if args.soak_only:
            soak(res, db, args.soak or 60)
            print(f"{DIM}pass took {time.time() - started:.0f}s{OFF}")
            overall = res.summary() and overall
            continue
        check_consistency(res, db)
        check_presets(res)
        check_sampled_packages(res, db, args.sample)
        check_map(res)
        check_events(res)
        check_typosquats(res)
        check_search(res, db)
        check_lockfiles(res)
        check_failure_modes(res)
        check_static(res)
        check_concurrency(res, db, args.workers, args.each)
        if not args.no_browser:
            check_browser(res)
        if args.soak:
            soak(res, db, args.soak)
        print(f"\n{DIM}pass took {time.time() - started:.0f}s{OFF}")
        overall = res.summary() and overall
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
