"""Live verification of HydraDB 0.1.0's constraint map.

The organizers asked people to surface what does not work. This turns that from
a section of a README — which is a claim — into a page that re-derives the claim
against the running database every few minutes.

Two kinds of finding are checked, and they are not equally valuable.

**The query surface** is the cheap half: constructs HydraDB rejects at parse
time. They are annoying, they are discovered in seconds, and the error message
tells you what happened. `probe_constraints.TESTS` is imported rather than
copied, so this page and the README table are provably the same evidence.

**The silent failures** are the expensive half, and they are the reason this
page exists. Nothing raises. The query returns, the shape looks right, and the
answer is wrong — an unknown package reports an empty blast radius, which reads
as *safe*. Those cost days. Each one below is re-run live, with the wrong
version and the corrected version side by side, so the difference is visible
rather than described.

Everything is cleaned up after itself, and a probe that cannot run says so
rather than being omitted — an absent row would read as a passing one.
"""

from __future__ import annotations

import json
import os
import threading
import time

import requests
from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

import apimeta
import probe_constraints
from hydra import HYDRA_CELL, HYDRA_GRAPH, HYDRA_TOKEN, HYDRA_URL, _rows

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")

TTL = 300.0                  # a full sweep is ~30 writes; do not do it per request
PROBE_TIMEOUT = 45.0

# Well above anything nid() has produced in this graph, and deleted on the way
# out. The write probes need ids that are guaranteed absent, because "does an
# unknown id return a row" is precisely what trap 1 is about.
BASE_ID = 999_999_999_999_900

router = APIRouter()
_cache: dict = {"at": 0.0, "value": None, "measuring": False, "error": ""}
_flight = apimeta.SingleFlight()
_lock = threading.Lock()


def _post(query: str, params: dict | None = None) -> tuple[bool, object, float]:
    """(ok, payload_or_error, ms) — the raw wire response, not unwrapped.

    Deliberately not routed through `Hydra`: the client's whole job is to hide
    these edges (it retries, it pages, it unwraps typed nulls), and hiding them
    is exactly what this page must not do. Reading the token and URL from the
    same config the client uses is the only thing borrowed, so a rotated
    production token still works.
    """
    body: dict = {"cell_id": HYDRA_CELL, "query": query, "consistency": "causal"}
    if params:
        body["parameters"] = params
    url = f"{HYDRA_URL}/v1/graphs/{HYDRA_GRAPH}/query"
    headers = {"Authorization": f"Bearer {HYDRA_TOKEN}",
               "X-Graph-Namespace": HYDRA_GRAPH,
               "Content-Type": "application/json"}
    t0 = time.perf_counter()
    try:
        r = requests.post(url, data=json.dumps(body), headers=headers,
                          timeout=PROBE_TIMEOUT)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:200]}", ms
        payload = r.json()
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return False, str(msg)[:200], ms
        return True, payload, ms
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return False, f"{type(exc).__name__}: {exc}"[:200], ms


# --------------------------------------------------------------------------
# what we did instead — the half of the table that is not automatable
# --------------------------------------------------------------------------

INSTEAD = {
    "string vertex id":
        "nid() derives a 51-bit integer from the name, so ids need no lookup.",
    "CREATE INDEX":
        "No index is needed — every id is derived from the name, not searched for.",
    "MATCH ... MERGE edge inside UNWIND":
        "CREATE with two explicit ids; the sidecar's primary key stops duplicates.",
    "length(path) in RETURN":
        "Difference count(*) at each depth instead of grouping paths by length.",
    "var-length MATCH with no fixed source":
        "Reverse the edge, so every traversal starts from one known package.",
    "filter on edge property during traversal":
        "Semver ranges live in SQLite; the graph holds topology only.",
    "bare MATCH (n) with no predicate":
        "Always scope by label or id — which turned out to matter for correctness "
        "as well as for parsing. See trap 1.",
    "count(DISTINCT x)":
        "RETURN DISTINCT, then count client-side.",
    "$param as upper bound in *1..$n":
        "Interpolate a clamped integer — the one piece of user input that reaches "
        "Cypher as text, so it is bounded to 1..8 before it gets there.",
}

GROUP_TITLE = {
    "rejected": "Rejected at parse time",
    "supported": "Load-bearing, and confirmed still working",
    "characterised": "Unknown until measured",
}


def _query_surface() -> list[dict]:
    """Re-run the probe set that produced the README's constraint table."""
    out = []
    for label, expect, query, params in probe_constraints.TESTS:
        ok, payload, ms = _post(query, params)
        observed = "WORKS" if ok else "FAILS"
        detail = (json.dumps(payload)[:220] if ok else str(payload)[:220])
        if expect == "FAILS":
            group = "rejected"
        elif expect == "WORKS":
            group = "supported"
        else:
            group = "characterised"
        out.append({
            "label": label,
            "group": group,
            "expected": expect,
            "observed": observed,
            # A "?" was never a prediction, so it cannot be contradicted.
            "holds": (expect == observed) if expect in ("WORKS", "FAILS") else None,
            "query": query.strip(),
            "detail": detail,
            "instead": INSTEAD.get(label, ""),
            "ms": round(ms, 1),
        })
    for query, params in probe_constraints.CLEANUP:
        _post(query, params)
    return out


# --------------------------------------------------------------------------
# the silent failures — each shown wrong first, then corrected
# --------------------------------------------------------------------------

def _trap_unknown_id() -> dict:
    """An id-only MATCH cannot say "no", and that reads as safety.

    Until this was found, every package missing from the crawl reported an
    empty blast radius. Not an error — an empty result, which any operator
    reads as "nothing depends on it, you are fine".
    """
    ghost = BASE_ID + 1                      # never written, by construction
    ok_loose, loose, ms1 = _post("MATCH (p {id: $id}) RETURN p.name", {"id": ghost})
    ok_scoped, scoped, ms2 = _post("MATCH (p:Package {id: $id}) RETURN p.name",
                                   {"id": ghost})
    if not (ok_loose and ok_scoped):
        return _unavailable("an id-only MATCH can never say no",
                            loose if not ok_loose else scoped)

    loose_rows, scoped_rows = _rows(loose), _rows(scoped)
    holds = len(loose_rows) > 0 and len(scoped_rows) == 0
    return {
        "label": "An id-only MATCH can never say \"no\"",
        "holds": holds,
        "cost": "Every uncrawled package reported an empty blast radius.",
        "wrong": {
            "query": "MATCH (p {id: $id}) RETURN p.name",
            "result": f"{len(loose_rows)} row(s): {json.dumps(loose_rows)[:120]}",
            "reading": "a row of nulls — indistinguishable from a real answer",
            "ms": round(ms1, 1),
        },
        "right": {
            "query": "MATCH (p:Package {id: $id}) RETURN p.name",
            "result": f"{len(scoped_rows)} row(s)",
            "reading": "empty — an actual existence test",
            "ms": round(ms2, 1),
        },
    }


def _trap_typed_null() -> dict:
    """`{"type":"null"}` has no "value" key, so a client that unwraps only
    two-key dicts leaves it as a truthy dict. This compounded trap 1 exactly:
    the absent property read as present."""
    ghost = BASE_ID + 2
    ok, payload, ms = _post("MATCH (p {id: $id}) RETURN p.name", {"id": ghost})
    if not ok:
        return _unavailable("a typed null has no value key", payload)

    raw = json.dumps(payload)
    naive = "type" in raw and "null" in raw and '"value"' not in raw.split("null")[-1][:40]
    return {
        "label": "A typed null has no \"value\" key",
        "holds": '"null"' in raw,
        "cost": "An absent property survived as a truthy dict and read as present.",
        "wrong": {
            "query": "value.get(\"value\")  # the obvious unwrap",
            "result": raw[:160],
            "reading": "every other value is {\"type\":…,\"value\":…}; this one is not",
            "ms": round(ms, 1),
        },
        "right": {
            "query": "if v.get(\"type\") == \"null\" and \"value\" not in v: return None",
            "result": "None",
            "reading": "absent is absent",
            "ms": 0.0,
        },
        "_naive": naive,
    }


def _trap_pagination() -> dict:
    """Results page at 1024 rows. Ignoring `next_cursor` silently truncates
    every large answer — and a truncated blast radius is an under-count of who
    is exposed, which is the direction that gets somebody paged too late."""
    ok, payload, ms = _post(
        "MATCH (p:Package) RETURN p.name LIMIT $limit", {"limit": 5000})
    if not ok:
        return _unavailable("results page at 1024 rows", payload)

    rows = _rows(payload)
    cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
    return {
        "label": "Results page at 1024 rows",
        "holds": len(rows) <= 1024 and bool(cursor),
        "cost": "Every answer over 1024 rows was silently cut off.",
        "wrong": {
            "query": "RETURN p.name LIMIT 5000",
            "result": f"{len(rows)} rows returned",
            "reading": ("asked for 5000, got a page — and the page does not "
                        "announce itself"),
            "ms": round(ms, 1),
        },
        "right": {
            "query": "follow next_cursor WITH query_id until it is absent",
            "result": f"next_cursor present: {bool(cursor)}",
            "reading": "continuing a page needs BOTH the cursor and the query_id",
            "ms": 0.0,
        },
    }


def _trap_count_semantics() -> dict:
    """count(*) over a variable-length match counts distinct reachable
    vertices, not paths. This one is good news, and the depth histogram is
    built on it — so it is worth re-proving rather than trusting.

    Builds a deliberately diamond-shaped graph where the two numbers differ:

        a -> b -> c -> d,  a -> e,  b -> e

    From a, depth 1..2 reaches {b, e, c} = 3 vertices along 4 distinct paths.
    """
    ids = {k: BASE_ID + 10 + i for i, k in enumerate("abcde")}
    edges = [("a", "b"), ("b", "c"), ("c", "d"), ("a", "e"), ("b", "e")]
    try:
        ok, err, _ = _post(
            "UNWIND $rows AS row MERGE (n {id: row.id}) SET n:_Probe",
            {"rows": [{"id": v} for v in ids.values()]})
        if not ok:
            return _unavailable("count(*) counts vertices, not paths", err)
        _post("UNWIND $rows AS row CREATE (a {id: row.src})-[:_PROBE]->(b {id: row.dst})",
              {"rows": [{"src": ids[s], "dst": ids[d]} for s, d in edges]})

        ok, payload, ms = _post(
            "MATCH (t {id: $id})-[:_PROBE*1..2]->(v) RETURN count(*)",
            {"id": ids["a"]})
        if not ok:
            return _unavailable("count(*) counts vertices, not paths", payload)
        rows = _rows(payload)
        got = rows[0].get("count(*)") if rows else None
        return {
            "label": "count(*) on a variable-length match counts vertices, not paths",
            "holds": got == 3,
            "cost": "Good news, and load-bearing: it makes the depth histogram exact.",
            "wrong": {
                "query": "a->b->c->d, a->e, b->e   —   count(*) at depth 1..2",
                "result": "4 if it counted paths",
                "reading": "a diamond makes the two numbers disagree on purpose",
                "ms": 0.0,
            },
            "right": {
                "query": "MATCH (t {id: $a})-[:_PROBE*1..2]->(v) RETURN count(*)",
                "result": f"{got} — distinct vertices {{b, e, c}}",
                "reading": ("so the histogram differences cumulative counts "
                            "instead of enumerating paths"),
                "ms": round(ms, 1),
            },
        }
    finally:
        for v in ids.values():
            _post("MATCH (p {id: $id}) DETACH DELETE p", {"id": v})


def _trap_readonly() -> dict:
    """A restarted store is permanently read-only. Reads answer perfectly and
    every write returns 500, so nothing surfaces it until you try to write."""
    probe = BASE_ID + 3
    ok, payload, ms = _post(
        "UNWIND $rows AS row MERGE (p {id: row.id}) SET p:_Probe",
        {"rows": [{"id": probe}]})
    if ok:
        _post("MATCH (p {id: $id}) DETACH DELETE p", {"id": probe})
    ok_read, _read, ms_read = _post("MATCH (p:Package) RETURN count(*)")
    return {
        "label": "A restarted store is permanently read-only",
        # This trap *holding* means writes fail. Right now we want them to work,
        # so `holds` is inverted here on purpose and the page says which it is.
        "holds": not ok,
        "writable": ok,
        "cost": ("SlateDB cannot update an existing manifest on the local "
                 "filesystem backend, so only the first boot can write."),
        "wrong": {
            "query": "MATCH (p:Package) RETURN count(*)",
            "result": "answers perfectly" if ok_read else "read failed",
            "reading": "reads never reveal it",
            "ms": round(ms_read, 1),
        },
        "right": {
            "query": "UNWIND $rows AS row MERGE (p {id: row.id}) SET p:_Probe",
            "result": "write round-trips" if ok else f"write failed: {str(payload)[:90]}",
            "reading": ("/api/health round-trips a write on a timer; rebuild.py "
                        "replays the graph from the sidecar in about a minute"),
            "ms": round(ms, 1),
        },
    }


def _unavailable(label: str, why: object) -> dict:
    """A probe that could not run says so. Omitting it would read as a pass."""
    return {"label": label, "holds": None, "unavailable": True,
            "cost": "", "detail": str(why)[:200],
            "wrong": None, "right": None}


# Findings that are real and documented but are not a boolean a probe can
# answer. Listing them as narrative — and saying so — beats either dropping
# them or dressing them up as live checks.
NARRATIVE = [
    {"label": "/readyz is not a readiness signal for queries",
     "detail": ("It returns 200 within a second of a restart while deep "
                "traversals still exceed the engine's own 30s query ceiling. "
                "The server warms itself by walking depths 1→5 in order, "
                "because each depth caches the pages the next one needs.")},
    {"label": "Traversal cost scales with total store size, not edges walked",
     "detail": ("Measured here: between 96k and 104k edges, depth-5 latency was "
                "flat, while latency tracked the reachable set — 3,731 reachable "
                "took 2.8s, 1,988 took 1.4s, and a package nothing depends on "
                "answered in 19ms.")},
]


def probe_all() -> dict:
    surface = _query_surface()
    traps = [_trap_unknown_id(), _trap_typed_null(), _trap_pagination(),
             _trap_count_semantics(), _trap_readonly()]

    groups = []
    for kind in ("rejected", "supported", "characterised"):
        rows = [r for r in surface if r["group"] == kind]
        if rows:
            groups.append({"kind": kind, "title": GROUP_TITLE[kind], "rows": rows})

    predicted = [r for r in surface if r["holds"] is not None]
    surprises = [r for r in predicted if not r["holds"]]
    return {
        "measured_at": time.time(),
        "hydra_url": HYDRA_URL,
        "summary": {
            "probes": len(surface) + len(traps),
            "predictions": len(predicted),
            "surprises": len(surprises),
            "traps_confirmed": sum(1 for t in traps if t.get("holds")),
            "traps_total": len(traps),
            "writable": next((t.get("writable") for t in traps
                              if "writable" in t), None),
        },
        "surprises": [{"label": r["label"], "expected": r["expected"],
                       "observed": r["observed"], "detail": r["detail"]}
                      for r in surprises],
        "groups": groups,
        "traps": traps,
        "narrative": NARRATIVE,
    }


def _sweep() -> None:
    """Run one sweep and store it. Never raises into the caller's thread."""
    try:
        value, _shared = _flight.run("constraints", probe_all)
        with _lock:
            _cache.update(value=value, at=time.time(), error="")
    except Exception as exc:
        with _lock:
            _cache["error"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        with _lock:
            _cache["measuring"] = False


def _kick() -> None:
    with _lock:
        if _cache["measuring"]:
            return
        _cache["measuring"] = True
    threading.Thread(target=_sweep, name="constraints-sweep", daemon=True).start()


def cached(force: bool = False) -> dict:
    """Stale-while-revalidate.

    A full sweep takes about a minute — most of it one `count(*)` over a label,
    which is a full scan with no index to lean on, which is itself finding
    number seven. Blocking a page load on that would be a poor way to make the
    point, so a stale result is served immediately while a fresh one is
    measured behind it, and the response says which it is rather than
    presenting an hour-old reading as current.
    """
    now = time.time()
    with _lock:
        value, at, err = _cache["value"], _cache["at"], _cache.get("error", "")
        measuring = _cache["measuring"]

    if value is None:
        _kick()
        return {"ready": False, "measuring": True, "error": err,
                "message": ("probing HydraDB — a full sweep takes about a "
                            "minute, most of it a single count(*) over a label")}

    age = now - at
    if force or age >= TTL:
        _kick()
    return {**value, "ready": True, "age_s": round(age, 1),
            "stale": age >= TTL, "measuring": measuring or age >= TTL,
            "error": err}


def warm() -> None:
    """Take the first reading in the background at startup, so the first
    visitor gets a page rather than a spinner."""
    _kick()


@router.get("/api/constraints")
def api_constraints(refresh: bool = Query(False)):
    """Re-derive the constraint map against the running database.

    Cached for five minutes because a sweep writes about thirty times; pass
    `refresh=1` to force one.
    """
    try:
        return {"ok": True, **cached(force=refresh)}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": "probe_failed",
             "message": f"could not probe HydraDB: {type(exc).__name__}: {exc}"[:300]},
            status_code=503)


@router.get("/constraints")
def constraints_page():
    return FileResponse(os.path.join(WEB, "constraints.html"))
