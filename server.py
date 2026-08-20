"""Blast Radius HTTP API + static console, on one port.

Every endpoint returns `latency_ms` measured around the real query. Nothing on
this server invents a number: if the graph does not know something yet, the
response says so rather than returning an empty result that reads as safety.

Run:  py server.py            (http://127.0.0.1:8000)
      py server.py --port 9000 --reload
"""

import argparse
import asyncio
import json
import os
import sqlite3
import threading
import time

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import blast
import intel
import scan
from hydra import Hydra, HydraError, nid
from ingest import DEPS_DB, SIDECAR_SCHEMA

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
DB_PATH = os.environ.get("DEPS_DB", os.path.join(HERE, DEPS_DB))

app = FastAPI(title="Blast Radius", docs_url="/api/docs", redoc_url=None)

# A request handler gets 20 seconds of wall clock including retries. A warm
# traversal takes about a second; anything past 20s means HydraDB is cold or
# gone, and the user is better served by a prompt, honest 503 than by a browser
# that hangs while the client patiently retries a 30-second query timeout.
hydra = Hydra(budget=20.0)

# The crawler and the warm-up thread are not on a request path and want the
# patient behaviour instead.
hydra_patient = Hydra(timeout=180.0, budget=None)

# HydraDB's own count(*) over the whole graph is a full scan with no index to
# lean on — at ~23k packages the package and edge counts together take over a
# minute. That number is worth showing, but it cannot sit on a request path, so
# a background thread re-measures it on a slow timer and every response carries
# how old the measurement is.
_graph_cache: dict = {"at": 0.0, "value": None, "error": None}
GRAPH_REFRESH = 180.0

# High-fan-in packages whose traversals touch a large slice of the graph, used
# to warm HydraDB's cache at startup. See _warm_graph().
WARM_PACKAGES = ("debug", "tslib", "chalk")
WARM_PROBE_INTERVAL = 15.0
_warm: dict = {"state": "cold", "detail": None, "event": threading.Event()}

# Whether the graph still accepts writes. A restarted store answers reads
# perfectly and fails every write, so this is invisible until you try — which
# is why it is probed on a timer and surfaced in /api/health rather than
# discovered in the middle of a crawl. Serving is read-only, so a read-only
# store is reported, not treated as an outage.
_writable: dict = {"ok": None, "detail": "not probed yet", "at": 0.0}
WRITE_PROBE_INTERVAL = 300.0
_WRITE_PROBE_ID = 999999999999998


def db() -> sqlite3.Connection:
    """A fresh read connection per request. WAL means readers never block the
    crawler, which is still writing while the console is being demoed."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    return conn


def ensure_db() -> None:
    """The crawler owns this file, but the server may start first."""
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SIDECAR_SCHEMA)
        conn.commit()
        conn.close()


def fail(message: str, status: int = 400, **extra):
    return JSONResponse({"error": message, **extra}, status_code=status)


@app.exception_handler(HydraError)
async def hydra_down(request: Request, exc: HydraError):
    """Two different 503s, because they mean different things to whoever is
    looking at the console: the database is gone, or it is here but still
    paging the store in and timing out its own 30-second query limit."""
    detail = str(exc)[:400]
    warming = (_warm["state"] != "warm"
               or "query_timeout" in detail
               or "Timeout" in detail)
    if warming:
        return JSONResponse(
            {"error": "graph_warming",
             "message": ("HydraDB is up but still warming its cache — a cold "
                         "store exceeds its own 30s query timeout on deep "
                         "traversals. This clears on its own."),
             "warmup": {k: v for k, v in _warm.items() if k != "event"},
             "detail": detail,
             "hint": "retry in a few seconds; GET /api/health tracks the state"},
            status_code=503)
    return JSONResponse(
        {"error": "HydraDB is not answering.",
         "detail": detail,
         "hint": "docker compose up -d hydradb"},
        status_code=503)


def known(name: str):
    """(is_in_graph, latency_ms) — plus the crawl context needed to explain a
    miss, since 'not crawled yet' and 'does not exist' are different answers."""
    return blast.resolve_package(hydra, name)


def not_yet(name: str, ms: float):
    with db() as conn:
        row = conn.execute("SELECT crawled FROM packages WHERE name = ?",
                           (name,)).fetchone()
        crawled = conn.execute(
            "SELECT count(*) FROM packages WHERE crawled = 1").fetchone()[0]
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    seen = row is not None
    with db() as conn2:
        running = blast.quick_stats(conn2)["crawl"]["running"]
    if seen:
        message = f"'{name}' is a known dependency but has not been crawled yet."
    elif running:
        message = (f"'{name}' is not in the graph yet — the crawl is still "
                   f"running ({crawled} packages so far).")
    else:
        message = (f"'{name}' is not in the graph. The crawl covered {crawled} "
                   f"packages; this package was not among them.")
    return JSONResponse({
        "error": "not_in_graph",
        "name": name,
        "message": message,
        "seen_as_dependency": seen,
        "crawl_running": running,
        "packages_crawled": crawled,
        "latency_ms": round(ms, 1),
    }, status_code=404)


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

def _refresh_graph_counts() -> None:
    """Re-measure the graph with HydraDB's own count(*), forever, slowly."""
    _warm["event"].wait()          # a cold full scan just burns the query timeout
    while True:
        try:
            _graph_cache.update(value=blast.graph_stats(hydra_patient), at=time.time(),
                                error=None)
        except Exception as e:                       # a down server must not kill the thread
            _graph_cache.update(error=str(e)[:200], at=time.time())
        time.sleep(GRAPH_REFRESH)


def _probe_writable() -> None:
    """Round-trip a real write on a slow timer. Reads cannot detect this."""
    while True:
        _warm["event"].wait()
        try:
            hydra_patient.query(
                "UNWIND $rows AS row MERGE (p {id: row.id}) SET p:_Probe",
                {"rows": [{"id": _WRITE_PROBE_ID}]}, retries=1)
            hydra_patient.query("MATCH (p {id: $id}) DETACH DELETE p",
                                {"id": _WRITE_PROBE_ID}, retries=1)
            _writable.update(ok=True, detail="writes round-trip", at=time.time())
        except Exception as e:
            detail = str(e)
            if "PutMode::Update" in detail or "internal query execution error" in detail:
                detail = ("store is read-only: the SlateDB manifest cannot be "
                          "updated on the local filesystem backend after a "
                          "restart. Rebuild with `py rebuild.py`.")
            _writable.update(ok=False, detail=detail[:220], at=time.time())
        time.sleep(WRITE_PROBE_INTERVAL)


def _warm_once() -> None:
    """Walk the depths in order for a few high-fan-in packages.

    In order on purpose: depth 1 is cheap and caches the pages depth 2 needs,
    and so on. Going straight to depth 5 on a cold store just burns the query
    timeout repeatedly without making progress.
    """
    for name in WARM_PACKAGES:
        for depth in range(1, blast.MAX_DEPTH_DEFAULT + 1):
            hydra_patient.query(blast.REACH_COUNT % depth, {"id": nid(name)},
                                retries=1)


def _warm_supervisor() -> None:
    """Keep the graph answerable, and keep an honest record of whether it is.

    Measured on this machine: after a container restart HydraDB serves /readyz
    within a second but cannot complete a depth-5 traversal for ~93 seconds,
    failing its own 30-second query timeout while the store pages in. Warming
    once at startup is not enough — the database can go away and come back at
    any point, and a one-shot warm would still be claiming "warm" while every
    request times out.

    So this supervises: warm, then probe periodically, and on a failed probe go
    back to warming. That re-warms the cache without waiting for a user to
    discover the problem, and it means /api/health and the 503 body describe
    the state the system is actually in.
    """
    while True:
        started = time.time()
        while True:
            try:
                _warm_once()
                _warm.update(state="warm", detail=None,
                             took_s=round(time.time() - started, 1))
                _warm["event"].set()
                break
            except Exception as e:
                _warm.update(state="warming", detail=str(e)[:160],
                             elapsed_s=round(time.time() - started, 1))
                time.sleep(3)
        # Warm. Watch for it going away again.
        while True:
            time.sleep(WARM_PROBE_INTERVAL)
            try:
                hydra_patient.query(blast.REACH_COUNT % 1,
                                    {"id": nid(WARM_PACKAGES[0])}, retries=1)
            except Exception as e:
                _warm.update(state="warming", detail=f"probe failed: {e}"[:160])
                break


@app.on_event("startup")
def _start_background_threads():
    threading.Thread(target=_warm_supervisor, daemon=True, name="warm").start()
    threading.Thread(target=_probe_writable, daemon=True, name="writable").start()
    threading.Thread(target=_refresh_graph_counts, daemon=True,
                     name="graph-counts").start()


@app.get("/api/stats")
def api_stats():
    """Live counts from the sidecar, plus the last full HydraDB measurement.

    The sidecar counts are what the header polls: the crawler writes the graph
    vertex and the sidecar row from the same batch, so they track each other.
    `graph` is the slower, authoritative check, with the age of the reading —
    never a fabricated stand-in when it has not been taken yet.
    """
    with db() as conn:
        value = blast.quick_stats(conn)
    measured = _graph_cache.get("value")
    if measured:
        value["graph"] = {**measured,
                          "age_s": round(time.time() - _graph_cache["at"], 1)}
    elif _graph_cache.get("error"):
        value["graph"] = {"error": _graph_cache["error"]}
    return value


@app.get("/api/health")
def api_health():
    """Component-level liveness, for watching the system in real time.

    `degraded` is a deliberate state rather than a binary up/down: with HydraDB
    unreachable the console still answers search, maintainer pivots and package
    metadata from the sidecar, and saying so is more useful than a red light.
    """
    out: dict = {"status": "ok", "components": {}}

    t0 = time.perf_counter()
    try:
        hydra.query("MATCH (p:Package {id: $id}) RETURN p.name",
                    {"id": nid("debug")}, retries=1)
        out["components"]["hydradb"] = {
            "up": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as e:
        out["components"]["hydradb"] = {"up": False, "error": str(e)[:200]}
        out["status"] = "degraded"

    try:
        with db() as conn:
            q = blast.quick_stats(conn)
        out["components"]["sidecar"] = {
            "up": True, "packages": q["packages"], "edges": q["edges"],
            "latency_ms": q["latency_ms"]}
        out["components"]["crawl"] = q["crawl"]
    except Exception as e:
        out["components"]["sidecar"] = {"up": False, "error": str(e)[:200]}
        out["status"] = "down"

    out["components"]["warmup"] = {k: v for k, v in _warm.items() if k != "event"}
    out["components"]["writable"] = dict(_writable)
    if out["status"] == "ok" and _warm["state"] != "warm":
        out["status"] = "warming"

    measured = _graph_cache.get("value")
    out["components"]["graph_measurement"] = (
        {"taken": True, "age_s": round(time.time() - _graph_cache["at"], 1), **measured}
        if measured else {"taken": False})

    code = {"ok": 200, "warming": 503, "degraded": 503, "down": 503}[out["status"]]
    return JSONResponse(out, status_code=code)


@app.get("/api/blast")
def api_blast(name: str = Query(..., min_length=1, max_length=214),
              depth: int = Query(5, ge=1, le=blast.MAX_DEPTH),
              limit: int = Query(5000, ge=1, le=200_000)):
    """Who is transitively exposed, and at what depth."""
    ok, ms_lookup = known(name)
    if not ok:
        return not_yet(name, ms_lookup)
    result, ms = blast.blast_radius(hydra, name, depth, limit)
    return {**result, "name": name, "vertex_id": nid(name),
            "latency_ms": round(ms, 1), "lookup_ms": round(ms_lookup, 1)}


@app.get("/api/resolve")
def api_resolve(name: str = Query(..., min_length=1, max_length=214),
                bad_version: str = Query(..., min_length=1, max_length=64)):
    """Whose declared range would actually have admitted the bad version."""
    with db() as conn:
        seen = conn.execute("SELECT 1 FROM packages WHERE name = ?", (name,)).fetchone()
        if not seen:
            ok, ms_lookup = known(name)
            if not ok:
                return not_yet(name, ms_lookup)
        result, ms = blast.would_resolve(conn, name, bad_version)
    return {**result, "name": name, "latency_ms": round(ms, 1)}


@app.post("/api/lockfile")
async def api_lockfile(request: Request,
                       name: str = Query(..., min_length=1, max_length=214),
                       bad_version: str | None = Query(None, max_length=64),
                       depth: int = Query(5, ge=1, le=blast.MAX_DEPTH)):
    """The raw package-lock.json is the request body; the incident is the query
    string. Returns EXPOSED / SHIELDED / CLEAR plus the path that reaches it."""
    raw = await request.body()
    if not raw:
        return fail("empty body — POST the package-lock.json as the request body")
    if len(raw) > 64 * 1024 * 1024:
        return fail("lockfile larger than 64MB", status=413)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return fail("lockfile is not valid UTF-8")

    ok, ms_lookup = known(name)
    if not ok:
        return not_yet(name, ms_lookup)
    try:
        with db() as conn:
            result, ms = blast.lockfile_exposure(hydra, conn, text, name,
                                                 bad_version, depth)
    except json.JSONDecodeError as e:
        return fail(f"not valid JSON: {e}")
    except ValueError as e:
        return fail(str(e))
    return {**result, "latency_ms": round(ms, 1)}


@app.post("/api/audit")
async def api_audit(request: Request,
                    max_detail: int = Query(60, ge=1, le=300)):
    """Scan a whole package-lock.json against the live advisory database.

    Distinct from /api/lockfile, which answers "am I exposed to *this* named
    incident". This asks the question you actually have before anyone tells you
    there is an incident: is anything in my tree already known to be malicious?
    It needs no graph coverage at all — the lockfile is the tree.
    """
    raw = await request.body()
    if not raw:
        return fail("empty body — POST the package-lock.json as the request body")
    if len(raw) > 64 * 1024 * 1024:
        return fail("lockfile larger than 64MB", status=413)
    try:
        resolved = blast.parse_lockfile(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return fail("lockfile is not valid UTF-8")
    except json.JSONDecodeError as e:
        return fail(f"not valid JSON: {e}")
    except ValueError as e:
        return fail(str(e))
    if not resolved:
        return fail("no resolved packages found in that lockfile")

    result = intel.audit_tree(resolved, max_detail=max_detail)
    verdict = ("COMPROMISED" if result["malicious_count"]
               else "VULNERABLE" if result["vulnerable_count"] else "CLEAN")
    return {**result, "verdict": verdict}


@app.get("/api/maintainers")
def api_maintainers(name: str = Query(..., min_length=1, max_length=214),
                    limit: int = Query(200, ge=1, le=2000)):
    """What else the compromised maintainers publish — the next blast radius."""
    with db() as conn:
        result, ms = blast.maintainer_pivot(conn, name, limit)
    if not result["maintainers"]:
        ok, ms_lookup = known(name)
        if not ok:
            return not_yet(name, ms_lookup)
        result["message"] = (f"'{name}' is in the graph, but the crawl has not "
                             f"recorded its maintainers yet.")
    return {**result, "name": name, "latency_ms": round(ms, 1)}


@app.get("/api/intel")
def api_intel(name: str = Query(..., min_length=1, max_length=214),
              version: str | None = Query(None, max_length=64)):
    """Is this package real, current, and compromised? — live, for any of npm.

    This is the half the graph cannot answer. The crawl covers 27k packages;
    npm has 4.3 million, and none of them carry a "this is malware" flag in
    their dependency edges. Registry and OSV are queried at request time, so
    this works for a package published a minute ago.
    """
    result = intel.assess(name, version)
    with db() as conn:
        row = conn.execute("SELECT crawled FROM packages WHERE name = ?",
                           (name,)).fetchone()
    result["in_graph"] = row is not None
    result["blast_radius_available"] = bool(row and row[0])
    return JSONResponse(result, status_code=200 if result.get("exists") else 404)


@app.get("/api/scan")
def api_scan(name: str = Query(..., min_length=1, max_length=214),
             version: str = Query(..., min_length=1, max_length=64),
             against: str | None = Query(None, max_length=64)):
    """Download the published tarball and read it. Optionally diff a release.

    Slow by nature — it fetches and unpacks real bytes — so it is deliberately
    a separate call the console makes on demand rather than part of a query.
    """
    result = scan.scan(name, version, against)
    return JSONResponse(result, status_code=200 if result.get("ok") else 422)


@app.get("/api/fix")
def api_fix(name: str = Query(..., min_length=1, max_length=214),
            bad_version: str = Query(..., min_length=1, max_length=64),
            depth: int = Query(5, ge=1, le=blast.MAX_DEPTH)):
    """A remediation bundle: the safe version, the exact commands, the
    `overrides` block, and a self-contained brief for a coding agent.

    Knowing you are exposed is only half of it. This is the half that closes
    the incident — and the safe version it names is checked against OSV rather
    than assumed to be `latest`.
    """
    dependents: list[str] = []
    try:
        ok, _ = known(name)
        if ok:
            victims, _ = blast.victim_set(hydra, name, depth)
            dependents = sorted(victims)
    except HydraError:
        pass                       # remediation must work with the graph down
    result = intel.remediation(name, bad_version, dependents=dependents)
    return JSONResponse(result,
                        status_code=200 if result.get("verdict") != "unknown_package" else 404)


@app.get("/api/subgraph")
def api_subgraph(name: str = Query(..., min_length=1, max_length=214),
                 depth: int = Query(3, ge=1, le=blast.MAX_DEPTH),
                 per_level: int = Query(28, ge=1, le=120),
                 max_nodes: int = Query(160, ge=2, le=600)):
    """A drawable slice of the radius: nodes by depth, plus the edges."""
    ok, ms_lookup = known(name)
    if not ok:
        return not_yet(name, ms_lookup)
    with db() as conn:
        result, ms = blast.subgraph(hydra, conn, name, depth, per_level, max_nodes)
    return {**result, "latency_ms": round(ms, 1)}


@app.get("/api/events")
async def api_events(request: Request):
    """Server-sent events: the live state of the system, pushed.

    The header used to poll /api/stats every four seconds, which is both
    laggier and more work. This pushes a frame whenever something changes and a
    keepalive otherwise, and the console falls back to polling if the stream
    cannot be established.
    """
    async def stream():
        last = None
        while True:
            if await request.is_disconnected():
                return
            try:
                with db() as conn:
                    payload = blast.quick_stats(conn)
                payload["warmup"] = _warm["state"]
                payload["hydradb"] = _warm["state"] == "warm"
                payload["writable"] = _writable["ok"]
                measured = _graph_cache.get("value")
                if measured:
                    payload["graph"] = measured
                frame = json.dumps(payload, sort_keys=True)
                if frame != last:
                    last = frame
                    yield f"event: stats\ndata: {frame}\n\n"
                else:
                    yield ": keepalive\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)[:200]})}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@app.get("/api/typosquats")
def api_typosquats(name: str = Query(..., min_length=1, max_length=214)):
    """One-edit neighbours of this name that actually exist on npm."""
    with db() as conn:
        result, ms = blast.typosquat_ring(conn, name)
    return {**result, "name": name, "latency_ms": round(ms, 1)}


@app.get("/api/search")
def api_search(q: str = Query("", max_length=214),
               limit: int = Query(12, ge=1, le=50)):
    """Autocomplete over every name the crawl has seen."""
    if not q.strip():
        return {"results": [], "latency_ms": 0.0}
    with db() as conn:
        rows, ms = blast.search(conn, q.strip(), limit)
    return {"results": rows, "latency_ms": round(ms, 1)}


# --------------------------------------------------------------------------
# static console — same origin, same port, no build step
# --------------------------------------------------------------------------

@app.get("/")
def index():
    path = os.path.join(WEB, "index.html")
    if not os.path.exists(path):
        return fail("web/index.html is missing", status=500)
    return FileResponse(path)


if os.path.isdir(WEB):
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")


def main():
    import uvicorn
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()
    ensure_db()
    uvicorn.run("server:app" if args.reload else app, host=args.host,
                port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
