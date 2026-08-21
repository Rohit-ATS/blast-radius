"""Blast Radius HTTP API + static console, on one port.

Every endpoint returns `latency_ms` measured around the real query. Nothing on
this server invents a number: if the graph does not know something yet, the
response says so rather than returning an empty result that reads as safety.

Run:  py server.py            (http://127.0.0.1:8000)
      py server.py --port 9000 --reload
"""

import argparse
import asyncio
import functools
import json
import os
import queue
import sqlite3
import threading
import time

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

import apimeta
import blast
import chains
import feed as feedmod
import intel
import live as livemod
import lockfiles
import scan
import watch as watchmod
from hydra import Hydra, HydraError, pkg_id
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

# Requests per minute per IP. The server makes OSV and registry calls on the
# caller's behalf, so an unbounded remote client is an unbounded bill for
# someone. 120/min turned out to be far too tight: one preset click fans out to
# six requests, and running the verification harnesses back to back tripped it
# — which is exactly how it would have tripped mid-demo.
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "600"))

# Loopback is the operator's own machine: the console, the CLI, the test
# harnesses. Throttling yourself protects nobody.
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}

# npm's published package count, used only to state coverage honestly. The
# live feed replaces this with the registry's own doc_count once it anchors.
NPM_TOTAL = 4_311_957

# Watch npm publish while the console is open. Disabled in DEMO_MODE, where
# determinism matters more than liveness.
LIVE_FEED = os.environ.get("LIVE_FEED", "1") == "1"
_feed = feedmod.Feed(hydra=hydra, blast_mod=blast)

# Continuous multi-ecosystem ingestion. This is what keeps the graph current;
# without it a blast radius is computed against whenever the last batch crawl
# happened to stop. Off in DEMO_MODE, where a recording has to be deterministic.
LIVE_INGEST = os.environ.get("LIVE_INGEST", "1") == "1"
_live = livemod.LiveIngest(hydra=hydra) if LIVE_INGEST else None

# Registered projects and their alerts. Created either way: the endpoints must
# answer honestly rather than 500 when ingestion is off.
_registry = watchmod.Registry(hydra=hydra)

# Demo safety. DEMO_MODE=1 serves captured real responses for the preset
# incidents, so a recording is deterministic and instant. Independently of that
# flag, a captured response is used as a *fallback* whenever a live call fails:
# better to show the real answer recorded ten minutes ago than a stack trace
# because the wifi dropped. Anything served this way says so — `demo: true` —
# because a fixture presented as a live query would be a lie.
DEMO_MODE = os.environ.get("DEMO_MODE", "") == "1"
DEMO_PATH = os.path.join(HERE, "fixtures", "demo.json")
_demo: dict = {}


def load_demo():
    global _demo
    try:
        with open(DEMO_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        _demo = {v["path"]: v for v in raw.values()}
        return len(_demo)
    except Exception:
        _demo = {}
        return 0


def demo_key(request) -> str:
    q = request.url.query
    return f"{request.url.path}?{q}" if q else request.url.path


def demo_for(request):
    """A captured response for this exact path and query string, or None."""
    hit = _demo.get(demo_key(request))
    if not hit:
        return None
    body = dict(hit["body"])
    body["demo"] = True
    body["demo_captured_at"] = hit.get("captured_at")
    return JSONResponse(body, status_code=hit.get("status", 200))


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


def fail(message: str, status: int = 400, code: str = "bad_request", **extra):
    """Every failure carries a machine-readable code as well as prose."""
    return JSONResponse({"ok": False, "error": code, "message": message, **extra},
                        status_code=status)


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
# cross-cutting: envelope, rate limit, compression, request id, logging
# --------------------------------------------------------------------------

apimeta.setup_logging()
_cache = apimeta.Cache()
_limiter = apimeta.RateLimiter(limit=RATE_LIMIT, window=60.0)

# Which store actually answered each path, so a response can say so rather than
# leaving the reader to guess whether a number came from the graph or a cache.
_SOURCE = {
    "/api/blast": "hydradb", "/api/subgraph": "hydradb", "/api/expand": "hydradb",
    "/api/attack-surface": "hydradb", "/api/why-exposed": "hydradb",
    "/api/blast-advisory": "hydradb", "/api/typosquat-risk": "hydradb",
    "/api/stats": "sidecar", "/api/health": "sidecar", "/api/search": "sidecar",
    "/api/maintainers": "sidecar", "/api/resolve": "sidecar",
    "/api/lockfile": "hydradb",
    "/api/intel": "osv", "/api/audit": "osv", "/api/fix": "osv",
    "/api/typosquats": "registry", "/api/scan": "registry",
}


def graph_coverage():
    """How much of npm the graph actually holds. Published on every response
    because it is the single honest caveat on any traversal answer."""
    try:
        with db() as conn:
            crawled = conn.execute(
                "SELECT count(*) FROM packages WHERE crawled = 1").fetchone()[0]
            known = conn.execute("SELECT count(*) FROM packages").fetchone()[0]
        total = _feed.npm_total or NPM_TOTAL
        return {"packages_in_graph": known, "packages_crawled": crawled,
                "npm_total": total,
                "fraction": round(known / total, 5)}
    except Exception:
        return None


app.add_middleware(GZipMiddleware, minimum_size=800)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"],
    expose_headers=["X-Request-Id", "X-Response-Time-Ms"],
)


@app.middleware("http")
async def envelope(request: Request, call_next):
    rid = apimeta.request_id()
    request.state.request_id = rid
    started = time.perf_counter()
    path = request.url.path

    client_ip = request.client.host if request.client else "?"
    if (path.startswith("/api/") and client_ip not in LOOPBACK
            and path not in ("/api/health", "/api/events")):
        ok, retry = _limiter.check(client_ip)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": "rate_limited",
                 "message": f"more than {RATE_LIMIT} requests a minute from this "
                            f"address; try again in {retry}s.",
                 "retry_after_s": retry, "request_id": rid},
                status_code=429,
                headers={"Retry-After": str(retry), "X-Request-Id": rid})

    if DEMO_MODE and path.startswith("/api/"):
        canned = demo_for(request)
        if canned is not None:
            canned.headers["X-Request-Id"] = rid
            canned.headers["X-Demo-Mode"] = "1"
            return canned

    try:
        response = await call_next(request)
    except HydraError as exc:
        canned = demo_for(request)
        if canned is not None:
            apimeta.log("served_fixture", request_id=rid, path=path,
                        reason="hydra_error")
            canned.headers["X-Request-Id"] = rid
            canned.headers["X-Demo-Fallback"] = "1"
            return canned
        response = await hydra_down(request, exc)
    except Exception as exc:                       # never a bare 500
        canned = demo_for(request)
        if canned is not None:
            apimeta.log("served_fixture", request_id=rid, path=path,
                        reason=exc.__class__.__name__)
            canned.headers["X-Request-Id"] = rid
            canned.headers["X-Demo-Fallback"] = "1"
            return canned
        apimeta.log("unhandled", request_id=rid, path=path,
                    error=f"{exc.__class__.__name__}: {exc}"[:300])
        response = JSONResponse(
            {"ok": False, "error": "internal_error",
             "message": "the server failed to handle this request.",
             "detail": f"{exc.__class__.__name__}: {exc}"[:300],
             "request_id": rid},
            status_code=500)

    took = (time.perf_counter() - started) * 1000
    response.headers["X-Request-Id"] = rid
    response.headers["X-Response-Time-Ms"] = f"{took:.1f}"
    if path.startswith("/api/"):
        apimeta.log("request", request_id=rid, path=path,
                    method=request.method, status=response.status_code,
                    ms=round(took, 1))
    return response


class EnvelopeRoute(APIRoute):
    """Attach the same metadata to every JSON body the API returns."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            response = await original(request)
            ctype = response.headers.get("content-type", "")
            if not ctype.startswith("application/json"):
                return response
            try:
                body = json.loads(response.body)
            except Exception:
                return response
            if not isinstance(body, dict):
                return response

            path = request.url.path
            body.setdefault("ok", response.status_code < 400)
            body.setdefault("source", _SOURCE.get(path, "hydradb"))
            body.setdefault("request_id", getattr(request.state, "request_id", None))
            if path not in ("/api/health",):
                cov = graph_coverage()
                if cov:
                    body.setdefault("graph_coverage", cov)
            body.setdefault("cached", False)
            return JSONResponse(body, status_code=response.status_code,
                                headers={k: v for k, v in response.headers.items()
                                         if k.lower() not in ("content-length",
                                                              "content-type")})

        return handler


app.router.route_class = EnvelopeRoute


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


_warm_done: set[tuple[str, int]] = set()


def _warm_once() -> None:
    """Walk the depths in order for a few high-fan-in packages, incrementally.

    In order on purpose: depth 1 is cheap and caches the pages depth 2 needs,
    and so on. Going straight to depth 5 on a cold store just burns the query
    timeout without making progress.

    Crucially this remembers what already succeeded. The first version
    restarted the whole sequence whenever any depth failed, so a store where
    depth 4 needed three attempts would re-run depths 1-3 forever and never
    converge — it sat at "warming" for 811 seconds in testing while a manual
    retry of depth 4 succeeded on the third try. Each pass now only attempts
    what is still outstanding, so every attempt moves forward.
    """
    outstanding = [(n, d) for n in WARM_PACKAGES
                   for d in range(1, blast.MAX_DEPTH_DEFAULT + 1)
                   if (n, d) not in _warm_done]
    for name, depth in outstanding:
        hydra_patient.query(blast.REACH_COUNT % depth, {"id": pkg_id(name)},
                            retries=1)
        _warm_done.add((name, depth))


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
                total = len(WARM_PACKAGES) * blast.MAX_DEPTH_DEFAULT
                _warm.update(state="warming", detail=str(e)[:160],
                             progress=f"{len(_warm_done)}/{total}",
                             elapsed_s=round(time.time() - started, 1))
                time.sleep(2)
        # Warm. Watch for it going away again.
        while True:
            time.sleep(WARM_PROBE_INTERVAL)
            try:
                hydra_patient.query(blast.REACH_COUNT % 1,
                                    {"id": pkg_id(WARM_PACKAGES[0])}, retries=1)
            except Exception as e:
                _warm_done.clear()
                _warm.update(state="warming", detail=f"probe failed: {e}"[:160])
                break


@app.on_event("startup")
def _start_background_threads():
    n = load_demo()
    apimeta.log("startup", demo_mode=DEMO_MODE, fixtures=n,
                rate_limit=RATE_LIMIT)
    threading.Thread(target=_warm_supervisor, daemon=True, name="warm").start()
    threading.Thread(target=_probe_writable, daemon=True, name="writable").start()
    if LIVE_FEED and not DEMO_MODE:
        _feed.start()
    if _live is not None and not DEMO_MODE:
        # Every package written into the graph is offered to the alert router,
        # which decides — by traversal — whose problem it is.
        _live.subscribe(_registry.route_async)
        _live.start()
        apimeta.log("live_ingest_started",
                    ecosystems=[a.name for a in _live.adapters])
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
                    {"id": pkg_id("debug")}, retries=1)
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

    t_osv = time.perf_counter()
    try:
        probe = intel.osv_query("debug", "4.4.2")
        out["components"]["osv"] = {
            "up": bool(probe.get("ok")),
            "latency_ms": round((time.perf_counter() - t_osv) * 1000, 1),
            "advisories_for_probe": len(probe.get("vulns", [])),
        }
        if not probe.get("ok"):
            out["components"]["osv"]["error"] = probe.get("error")
    except Exception as e:
        out["components"]["osv"] = {"up": False, "error": str(e)[:160]}

    out["uptime_s"] = apimeta.uptime_seconds()
    out["cache"] = _cache.stats()
    out["rate_limit"] = {"per_minute": RATE_LIMIT}

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
    return {**result, "name": name, "vertex_id": pkg_id(name),
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
                    max_detail: int = Query(60, ge=1, le=300),
                    filename: str = Query("", max_length=120,
                                          description="used only to disambiguate "
                                                      "the lockfile format")):
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
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return fail("lockfile is not valid UTF-8", code="bad_encoding")
    try:
        resolved, kind = lockfiles.parse_any(text, filename)
    except lockfiles.LockfileError as e:
        return fail(str(e), code="unreadable_lockfile")

    try:
        result = intel.audit_tree(resolved, max_detail=max_detail)
    except Exception:
        canned = _demo.get("/api/audit")
        if canned is None:
            raise
        body = dict(canned["body"])
        body["demo"] = True
        return JSONResponse(body)
    result["lockfile_format"] = kind
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


@app.get("/api/feed")
def api_feed(limit: int = Query(25, ge=1, le=60)):
    """What npm has published in the last few minutes, and who it reaches.

    npm rejects `feed=continuous` on its replication endpoint, so this polls
    `_changes?since=<seq>` every few seconds — near-real-time rather than
    streaming, which is what the response says.
    """
    return _feed.snapshot(limit)


@app.get("/api/expand")
def api_expand(name: str = Query(..., min_length=1, max_length=214),
               kind: str = Query("package", pattern="^(package|maintainer|advisory)$"),
               limit: int = Query(40, ge=1, le=200)):
    """One node and everything adjacent to it, across every edge type.

    The whole graph explorer runs on this: the browser holds no model of the
    graph, it just asks HydraDB what is next to whatever was clicked. Every
    relationship is stored in both directions so any node can be the fixed
    source a variable-length MATCH requires.
    """
    result = chains.expand(hydra, name, kind, limit)
    return JSONResponse(result, status_code=200 if result.get("found") else 404)


@app.get("/api/attack-surface")
def api_attack_surface(maintainer: str = Query(..., min_length=1, max_length=214),
                       depth: int = Query(4, ge=1, le=blast.MAX_DEPTH)):
    """Everything one npm account can reach: Maintainer -MAINTAINS-> Package
    -REQUIRED_BY*-> the blast radius. Two hops, two edge types.

    The number that matters after an account is phished is not how many
    packages they publish, but how much of npm sits downstream of those.
    """
    return chains.attack_surface(hydra, maintainer, depth)


@app.get("/api/why-exposed")
def api_why_exposed(source: str = Query(..., alias="from", min_length=1, max_length=214),
                    target: str = Query(..., alias="to", min_length=1, max_length=214),
                    depth: int = Query(6, ge=1, le=blast.MAX_DEPTH)):
    """The actual chain, hop by hop — not merely that a chain exists.

    The depth is computed in the graph and is authoritative. The concrete path
    is rebuilt from the sidecar (HydraDB 0.1.0 returns no path binding) and
    then every hop is re-confirmed against the graph.
    """
    with db() as conn:
        return chains.why_exposed(hydra, conn, source, target, depth)


@app.get("/api/blast-advisory")
def api_blast_advisory(osv_id: str = Query(..., min_length=3, max_length=64),
                       depth: int = Query(4, ge=1, le=blast.MAX_DEPTH)):
    """Blast radius of a CVE rather than of a package: Advisory -AFFECTS->
    Package -REQUIRED_BY*-> everyone downstream."""
    return chains.blast_advisory(hydra, osv_id, depth)


@app.get("/api/typosquat-risk")
def api_typosquat_risk(name: str = Query(..., min_length=1, max_length=214),
                       depth: int = Query(3, ge=1, le=blast.MAX_DEPTH)):
    """Similarly-named packages, and how many packages already pull each one.
    A near-miss name with real dependents is an incident, not a curiosity."""
    return chains.typosquat_risk(hydra, name, depth)


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
        last_publish = [None]
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

                # Publishes ride the same stream. Only pushed when the newest
                # one actually changed, so an idle minute costs nothing.
                fresh = _feed.snapshot(limit=6)["events"]
                newest = fresh[0]["at"] if fresh else None
                if newest and newest != last_publish[0]:
                    last_publish[0] = newest
                    yield ("event: publish\ndata: "
                           + json.dumps({"events": fresh}) + "\n\n")
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

# ==========================================================================
# Live ingestion and project monitoring
#
# These are the endpoints a real project integrates against: register your
# lockfile once, then either receive webhooks or hold open an SSE stream. The
# routing behind them is a HydraDB traversal from the package that just
# published to the projects that install it — see watch.py.
# ==========================================================================

@app.get("/api/live/status")
def api_live_status():
    """What the ingestion daemon is actually doing, per registry.

    This is deliberately unflattering: an ecosystem that is backed off, erroring
    or stopped says so, with the last error and the seconds since its last
    successful poll. A monitoring product whose own status page is optimistic is
    not a monitoring product.
    """
    if _live is None:
        return {"ok": True, "running": False,
                "reason": "live ingestion disabled (LIVE_INGEST=0 or DEMO_MODE)",
                "ecosystems": [], "ecosystems_live": 0, "ecosystems_total": 0}
    return {"ok": True, **_live.status()}


@app.get("/api/live/events")
def api_live_events(limit: int = Query(40, ge=1, le=200),
                    ecosystem: str = Query("", max_length=20)):
    """Packages written into the graph in the last few minutes, newest first."""
    if _live is None:
        return {"ok": True, "events": [], "running": False}
    events = _live.recent(limit=200)
    if ecosystem:
        events = [e for e in events if e["ecosystem"] == ecosystem]
    return {"ok": True, "running": True, "events": events[:limit],
            "counts_by_ecosystem": _live.status()["ecosystems"]}


@app.post("/api/watch/register")
async def api_watch_register(request: Request,
                             name: str = Query(..., min_length=1, max_length=120),
                             ecosystem: str = Query("", max_length=20),
                             filename: str = Query("", max_length=120),
                             webhook: str = Query("", max_length=500),
                             depth: int = Query(0, ge=0, le=4)):
    """Register a project from its lockfile or manifest.

    The body is the raw file. Everything it resolves becomes an edge into the
    graph, so a publish anywhere upstream routes here without a subscriber
    table. The returned token is shown once and is required by every other
    endpoint for this project — it is not recoverable, and it is not stored in
    a form we can read back to you.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    if not raw.strip():
        return fail("send the lockfile or manifest as the request body.",
                    code="empty_body")
    if webhook and not webhook.startswith(("http://", "https://")):
        return fail("webhook must be an http(s) URL.", code="bad_webhook")

    try:
        resolved, kind, eco, precision = lockfiles.parse_project(
            raw, filename=filename, ecosystem=ecosystem or None)
    except lockfiles.LockfileError as exc:
        return fail(str(exc), code="unparseable_manifest")

    if not resolved:
        return fail("no dependencies found in that file.", code="no_dependencies")

    try:
        proj = _registry.register(name, resolved, eco, precision=precision,
                                  webhook=webhook or None,
                                  depth=depth or None)
    except ValueError as exc:
        return fail(str(exc), code="invalid_project")

    apimeta.log("project_registered", project=proj["project_id"],
                ecosystem=eco, deps=proj["watching"], precision=precision)
    return {
        "ok": True,
        **proj,
        "source_kind": kind,
        "how_to_poll": f"/api/watch/{proj['project_id']}/alerts?token=…",
        "how_to_stream": f"/api/watch/{proj['project_id']}/stream?token=…",
        "note": ("exact: your lockfile is the resolved tree, so depth 1 is a "
                 "complete answer" if precision == "exact" else
                 "inferred: a manifest names only direct dependencies, so the "
                 "rest is reached by traversing the crawled graph"),
    }


def _auth(pid: str, token: str):
    return _registry.authenticate(pid, token or "")


@app.get("/api/watch/{pid}")
def api_watch_status(pid: str, token: str = Query("", max_length=200)):
    row = _auth(pid, token)
    if not row:
        return fail("unknown project or bad token.", status=403, code="forbidden")
    return {"ok": True, **_registry.project_status(row)}


@app.get("/api/watch/{pid}/alerts")
def api_watch_alerts(pid: str, token: str = Query("", max_length=200),
                     since: int = Query(0, ge=0),
                     limit: int = Query(50, ge=1, le=500),
                     min_severity: str = Query(
                         "info", pattern="^(info|medium|high|critical)$")):
    """Alerts for this project, newest first.

    `since` is the highest alert id you have already handled, which makes this
    safe to poll on a schedule: you get exactly what is new, and nothing twice.
    """
    row = _auth(pid, token)
    if not row:
        return fail("unknown project or bad token.", status=403, code="forbidden")
    alerts = _registry.alerts(pid, since=since, limit=limit,
                              min_severity=min_severity)
    return {"ok": True, "project_id": pid, "alerts": alerts,
            "count": len(alerts),
            "cursor": alerts[0]["id"] if alerts else since}


@app.post("/api/watch/{pid}/ack/{alert_id}")
def api_watch_ack(pid: str, alert_id: int, token: str = Query("", max_length=200)):
    row = _auth(pid, token)
    if not row:
        return fail("unknown project or bad token.", status=403, code="forbidden")
    return {"ok": True, "acked": _registry.ack(pid, alert_id)}


@app.delete("/api/watch/{pid}")
def api_watch_delete(pid: str, token: str = Query("", max_length=200)):
    if not _registry.unregister(pid, token or ""):
        return fail("unknown project or bad token.", status=403, code="forbidden")
    apimeta.log("project_unregistered", project=pid)
    return {"ok": True, "unregistered": pid}


@app.get("/api/watch/{pid}/stream")
async def api_watch_stream(pid: str, token: str = Query("", max_length=200)):
    """Server-sent events: one message per alert, as it is routed.

    A heartbeat every 15 seconds keeps proxies from closing an idle connection
    and lets the client tell "nothing has happened" apart from "the stream
    died", which are very different things to a system you are trusting to
    wake you up.
    """
    row = _auth(pid, token)
    if not row:
        return fail("unknown project or bad token.", status=403, code="forbidden")

    q = _registry.listen(pid)
    loop = asyncio.get_running_loop()

    async def gen():
        try:
            yield ("event: ready\ndata: " + json.dumps({
                "project_id": pid, "watching": row["dep_count"],
                "precision": row["precision"]}) + "\n\n")
            while True:
                try:
                    alert = await loop.run_in_executor(
                        None, functools.partial(q.get, True, 15.0))
                    yield "event: alert\ndata: " + json.dumps(alert) + "\n\n"
                except queue.Empty:
                    yield (": heartbeat " + str(int(time.time())) + "\n\n")
        finally:
            _registry.unlisten(pid, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/watch")
def api_watch_stats():
    """Aggregate monitoring numbers — no project details, no token required."""
    return {"ok": True, **_registry.stats()}


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
