"""Blast Radius HTTP API + static console, on one port.

Every endpoint returns `latency_ms` measured around the real query. Nothing on
this server invents a number: if the graph does not know something yet, the
response says so rather than returning an empty result that reads as safety.

Run:  py server.py            (http://127.0.0.1:8000)
      py server.py --port 9000 --reload
"""

import argparse
import json
import os
import sqlite3
import threading
import time

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import blast
from hydra import Hydra, HydraError, nid
from ingest import DEPS_DB, SIDECAR_SCHEMA

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
DB_PATH = os.environ.get("DEPS_DB", os.path.join(HERE, DEPS_DB))

app = FastAPI(title="Blast Radius", docs_url="/api/docs", redoc_url=None)
hydra = Hydra()

# HydraDB's own count(*) over the whole graph is a full scan with no index to
# lean on — at ~23k packages the package and edge counts together take over a
# minute. That number is worth showing, but it cannot sit on a request path, so
# a background thread re-measures it on a slow timer and every response carries
# how old the measurement is.
_graph_cache: dict = {"at": 0.0, "value": None, "error": None}
GRAPH_REFRESH = 180.0


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
    return JSONResponse(
        {"error": "HydraDB is not answering.",
         "detail": str(exc)[:400],
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
    while True:
        try:
            _graph_cache.update(value=blast.graph_stats(hydra), at=time.time(),
                                error=None)
        except Exception as e:                       # a down server must not kill the thread
            _graph_cache.update(error=str(e)[:200], at=time.time())
        time.sleep(GRAPH_REFRESH)


@app.on_event("startup")
def _start_refresher():
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
