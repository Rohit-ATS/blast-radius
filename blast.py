"""Incident-response queries over the HydraDB npm graph.

Five questions, one per feature in the demo:

  1. blast_radius()        Which packages are transitively exposed, and how deep?
  2. would_resolve()       Whose version *ranges* would have pulled the bad
                           version — not just who lists the package.
  3. lockfile_exposure()   Drop in a package-lock.json: are you hit, and via
                           which exact path?
  4. maintainer_pivot()    What else does the compromised maintainer control?
                           (i.e. what gets attacked next)
  5. typosquat_ring()      Which near-miss names sit next to the target?

Everything returns (result, latency_ms) so the UI can show real numbers. The
latency is measured around the actual call, not estimated.

Which store answers which question
----------------------------------
Reachability — "who is transitively exposed" — is the graph's job, and the only
part of this that a relational database does badly. Everything that is a
*predicate* rather than a *traversal* (does this range admit 4.4.2, who else
does this maintainer publish) is answered from the deps.db sidecar, because
HydraDB 0.1.0 cannot filter on edge properties mid-traversal. See ingest.py.
"""

import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from hydra import Hydra, RESULT_LIMIT, nid

MAX_DEPTH = 8


def _depth(depth: int) -> int:
    """HydraDB rejects a parameter as the hop bound ("unbounded variable-length
    MATCH requires an explicit max hop"), so the depth is interpolated into the
    query string. That makes it the one piece of user input that reaches Cypher
    as text — clamp it to a small integer and never pass it through raw."""
    d = int(depth)
    if d < 1 or d > MAX_DEPTH:
        raise ValueError(f"depth must be 1..{MAX_DEPTH}, got {depth}")
    return d


# --------------------------------------------------------------------------
# 0. is this package even in the graph yet?
# --------------------------------------------------------------------------

# The label matters. `MATCH (p {id: $id})` addresses a vertex slot directly and
# happily returns a row of nulls for an id that was never written, so it can
# never say "no". Scoping to :Package makes it an actual existence test.
EXISTS = "MATCH (p:Package {id: $id}) RETURN p.name"


def resolve_package(h: Hydra, name: str):
    """(known, latency_ms). The crawl runs in the background, so 'not here yet'
    is a normal answer during a demo, not an error — the API says so plainly
    instead of returning an empty blast radius that looks like safety."""
    rows, ms = h.timed(EXISTS, {"id": nid(name)})
    return bool(rows and rows[0].get("p.name")), ms


# --------------------------------------------------------------------------
# 1. blast radius — one traversal from the compromised package
# --------------------------------------------------------------------------
#
# The edge is stored (dependency)-[:REQUIRED_BY]->(dependent), so this walks
# *outward* from the compromised package to everything that transitively pulls
# it. See ingest.py for why the edge points that way.
#
# count(*) on a variable-length match returns the number of distinct reachable
# vertices, not the number of paths — verified against a diamond-shaped graph
# in probe_counts.py. That is what makes the depth histogram cheap: ask for the
# cumulative count at each bound and difference the series, rather than
# enumerating paths and grouping by length (which needs length(path), and
# HydraDB 0.1.0 only returns <binding>.<property> or count(*)).

REACH_COUNT = "MATCH (t {id: $id})-[:REQUIRED_BY*1..%d]->(v) RETURN count(*)"
REACH_NAMES = "MATCH (t {id: $id})-[:REQUIRED_BY*1..%d]->(v) RETURN DISTINCT v.name LIMIT $limit"


def blast_radius(h: Hydra, name: str, depth: int = 5, limit: int = 5000):
    """Everything that transitively depends on `name`, with a per-depth
    breakdown. Returns ({total, histogram, victims, truncated}, latency_ms).

    The d+1 queries are independent — each is its own bounded traversal from
    the same fixed source — so they are fired concurrently and the reported
    latency is wall-clock for the whole set, not the sum. Serially this is
    dominated by the deepest traversal repeated d times.
    """
    d = _depth(depth)
    target = nid(name)
    limit = min(limit, RESULT_LIMIT)

    def count_at(k: int) -> int:
        rows = h.query(REACH_COUNT % k, {"id": target})
        return rows[0]["count(*)"] if rows else 0

    def names() -> list[str]:
        rows = h.query(REACH_NAMES % d, {"id": target, "limit": limit})
        return sorted(r["v.name"] for r in rows if r.get("v.name"))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=d + 1) as pool:
        count_jobs = [pool.submit(count_at, k) for k in range(1, d + 1)]
        names_job = pool.submit(names)
        cumulative = [j.result() for j in count_jobs]
        victims = names_job.result()
    ms_total = (time.perf_counter() - t0) * 1000.0

    # Differencing the cumulative reach gives packages *first* reached at k.
    histogram = []
    prev = 0
    for k, total in enumerate(cumulative, start=1):
        histogram.append({"depth": k, "packages": max(total - prev, 0)})
        prev = total

    return {
        "total": cumulative[-1] if cumulative else 0,
        "histogram": histogram,
        "victims": victims,
        "truncated": len(victims) >= limit,
        "depth": d,
        "queries": d + 1,
    }, ms_total


def victim_set(h: Hydra, name: str, depth: int = 5, limit: int = RESULT_LIMIT):
    """Just the reachable names, as a set. Used by the lockfile check."""
    d = _depth(depth)
    rows, ms = h.timed(REACH_NAMES % d, {"id": nid(name),
                                         "limit": min(limit, RESULT_LIMIT)})
    return {r["v.name"] for r in rows if r.get("v.name")}, ms


def graph_stats(h: Hydra):
    """Graph size straight from HydraDB. Both counts are real, and both are
    full scans — HydraDB says so in its own logs ("query plan warrants
    attention: full_scan, access_path AllVertexScan") and there is no
    CREATE INDEX to fix it with. At ~23k packages the pair takes well over a
    minute, so this must never sit on a request path: server.py refreshes it on
    a background timer and serves the last measurement with its age."""
    ms_total = 0.0
    rows, ms = h.timed("MATCH (p:Package) RETURN count(*)")
    ms_total += ms
    packages = rows[0]["count(*)"] if rows else 0
    rows, ms = h.timed("MATCH ()-[r:REQUIRED_BY]->() RETURN count(*)")
    ms_total += ms
    edges = rows[0]["count(*)"] if rows else 0
    return {"packages": packages, "edges": edges, "measured_ms": round(ms_total, 1)}


def quick_stats(db: sqlite3.Connection):
    """The same two numbers, from the sidecar, in microseconds.

    This is not a second opinion: the crawler writes a sidecar row and a graph
    vertex from the same batch, and only `prod` dependencies become edges, so
    these counts track what was written to HydraDB exactly. graph_stats()
    verifies that on a slow timer; this is what the live header polls."""
    t0 = time.perf_counter()
    meta = dict(db.execute("SELECT key, value FROM meta"))
    packages = db.execute("SELECT count(*) FROM packages").fetchone()[0]
    edges = db.execute("SELECT count(*) FROM deps WHERE kind = 'prod'").fetchone()[0]
    crawled = db.execute("SELECT count(*) FROM packages WHERE crawled = 1").fetchone()[0]
    return {
        "packages": packages,
        "edges": edges,
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "crawl": {
            "crawled": crawled,
            "known": packages,
            "queued": int(meta.get("queued", 0)),
            "running": meta.get("running") == "1",
            "collisions": db.execute("SELECT count(*) FROM collisions").fetchone()[0],
        },
    }


def stats(h: Hydra, db: sqlite3.Connection | None = None):
    """Kept for check.py and the benchmark: the authoritative, slow version."""
    out = graph_stats(h)
    out["latency_ms"] = out["measured_ms"]
    if db is not None:
        out["crawl"] = quick_stats(db)["crawl"]
    return out


# --------------------------------------------------------------------------
# 2. would-resolve — the query a vector index cannot express at all
# --------------------------------------------------------------------------

DEPENDERS_SQL = """
SELECT name, version, range, kind
FROM release_deps
WHERE dep = ?
ORDER BY name, version
"""


def would_resolve(db: sqlite3.Connection, name: str, bad_version: str, limit: int = 20000):
    """Of everyone who depends on the package, whose declared semver range
    actually admits the malicious version? Listing a dependency is not the
    same as pulling it. This is the difference between a scary number and a
    true one.

    Ranges live in SQLite rather than on the graph edges because HydraDB 0.1.0
    cannot filter on edge properties during a traversal — the range would be
    unreachable exactly when it matters.
    """
    import time
    t0 = time.perf_counter()
    rows = db.execute(DEPENDERS_SQL, (name,)).fetchall()[:limit]

    exposed, shielded = {}, {}
    for dependent, version, rng, kind in rows:
        if dependent == name:
            continue
        bucket = exposed if satisfies(bad_version, rng or "") else shielded
        bucket.setdefault(dependent, []).append(
            {"version": version, "range": rng, "kind": kind})

    # A package counts as exposed if *any* of its releases would have pulled it.
    for dependent in list(shielded):
        if dependent in exposed:
            del shielded[dependent]

    ms = (time.perf_counter() - t0) * 1000.0
    return {
        "bad_version": bad_version,
        "exposed_count": len(exposed),
        "shielded_count": len(shielded),
        "checked": len(rows),
        "exposed": [{"name": k, "ranges": sorted({r["range"] for r in v})}
                    for k, v in sorted(exposed.items())],
        "shielded": [{"name": k, "ranges": sorted({r["range"] for r in v})}
                     for k, v in sorted(shielded.items())],
    }, ms


# --------------------------------------------------------------------------
# 3. lockfile exposure
# --------------------------------------------------------------------------


def parse_lockfile(text: str) -> dict[str, str]:
    """package-lock.json v1/v2/v3 -> {name: resolved_version}.

    v2/v3 carry a flat `packages` map keyed by install path; v1 carries a
    *nested* `dependencies` tree, so that branch recurses. Both shapes appear
    in the wild and npm still emits v1 for older projects.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("lockfile must be a JSON object")
    out: dict[str, str] = {}

    for path, meta in (data.get("packages") or {}).items():
        if not path or not isinstance(meta, dict):
            continue
        name = meta.get("name") or path.split("node_modules/")[-1]
        if name and meta.get("version"):
            out[name] = meta["version"]

    def walk(tree: dict) -> None:
        for name, meta in (tree or {}).items():
            if not isinstance(meta, dict):
                continue
            if meta.get("version"):
                out.setdefault(name, meta["version"])
            walk(meta.get("dependencies") or {})

    walk(data.get("dependencies") or {})
    return out


def shortest_path(db: sqlite3.Connection, roots: list[str], target: str,
                  max_depth: int = 8) -> list[str] | None:
    """Reconstruct one concrete root -> ... -> target chain.

    The graph answers *whether* you are exposed. It cannot hand back the path:
    HydraDB 0.1.0 has no path binding in RETURN. The chain is rebuilt from the
    sidecar's edge table, walking backwards from the compromised package to
    whichever of your direct dependencies reaches it first.
    """
    if target in roots:
        return [target]
    seen = {target}
    frontier = [target]
    parent: dict[str, str] = {}
    rootset = set(roots)
    for _ in range(max_depth):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        rows = db.execute(
            f"SELECT src, dst FROM deps WHERE dst IN ({placeholders}) AND kind = 'prod'",
            frontier).fetchall()
        nxt = []
        for src, dst in rows:
            if src in seen:
                continue
            seen.add(src)
            parent[src] = dst
            if src in rootset:
                chain = [src]
                while chain[-1] in parent:
                    chain.append(parent[chain[-1]])
                return chain
            nxt.append(src)
        frontier = nxt
    return None


def lockfile_exposure(h: Hydra, db: sqlite3.Connection, lock_text: str, name: str,
                      bad_version: str | None = None, depth: int = 5,
                      max_paths: int = 12):
    """Verdict for a real package-lock.json: EXPOSED / SHIELDED / CLEAR.

    EXPOSED  the compromised package is in your tree (directly or transitively)
    SHIELDED it is in your tree, but the version you resolved is not the bad one
    CLEAR    it is not in your tree at all
    """
    resolved = parse_lockfile(lock_text)
    victims, ms = victim_set(h, name, depth)

    affected = sorted(set(resolved) & victims)
    direct = None
    if name in resolved:
        pinned = resolved[name]
        direct = {"version": pinned,
                  "malicious": bad_version is not None and pinned == bad_version}

    if name in resolved:
        verdict = "EXPOSED" if (bad_version is None or direct["malicious"]) else "SHIELDED"
    elif affected:
        verdict = "EXPOSED"
    else:
        verdict = "CLEAR"

    paths = []
    for entry in affected[:max_paths]:
        chain = shortest_path(db, [entry], name)
        if chain:
            paths.append({"entry": entry, "path": chain, "depth": len(chain) - 1})
    if name in resolved and not paths:
        paths.append({"entry": name, "path": [name], "depth": 0})

    return {
        "verdict": verdict,
        "resolved_count": len(resolved),
        "compromised": name,
        "bad_version": bad_version,
        "direct": direct,
        "affected": affected,
        "affected_count": len(affected),
        "paths": paths,
    }, ms


# --------------------------------------------------------------------------
# 4. maintainer pivot — where the attacker goes next
# --------------------------------------------------------------------------


def maintainer_pivot(db: sqlite3.Connection, name: str, limit: int = 200):
    """Everything else the compromised package's maintainers publish. One
    stolen npm token is rarely one package — this is the next blast radius.

    Ownership is a two-hop join, not a transitive walk, so it belongs in the
    sidecar. `direct_dependents` is a real count from the same edge table the
    graph was built from.
    """
    import time
    t0 = time.perf_counter()
    owners = [r[0] for r in db.execute(
        "SELECT maintainer FROM maintainers WHERE package = ?", (name,))]
    siblings = []
    if owners:
        placeholders = ",".join("?" * len(owners))
        rows = db.execute(
            f"""SELECT m.package, group_concat(DISTINCT m.maintainer),
                       (SELECT count(*) FROM deps d WHERE d.dst = m.package)
                FROM maintainers m
                WHERE m.maintainer IN ({placeholders}) AND m.package <> ?
                GROUP BY m.package
                ORDER BY 3 DESC, 1 ASC
                LIMIT ?""",
            (*owners, name, limit)).fetchall()
        siblings = [{"package": p, "maintainers": (o or "").split(","),
                     "direct_dependents": n} for p, o, n in rows]
    ms = (time.perf_counter() - t0) * 1000.0
    return {"maintainers": owners, "also_controls": siblings,
            "sibling_count": len(siblings)}, ms


# --------------------------------------------------------------------------
# 5. typosquat ring
# --------------------------------------------------------------------------


def edit1(name: str) -> set[str]:
    """Deletions, transpositions, and homoglyph swaps — the three that show up
    in real npm typosquat campaigns."""
    out = set()
    for i in range(len(name)):
        out.add(name[:i] + name[i + 1:])
        if i + 1 < len(name):
            out.add(name[:i] + name[i + 1] + name[i] + name[i + 2:])
    for a, b in (("l", "1"), ("i", "1"), ("o", "0"), ("-", ""), ("rn", "m")):
        if a in name:
            out.add(name.replace(a, b, 1))
    out.discard(name)
    return {n for n in out if len(n) > 2}


def typosquat_ring(db: sqlite3.Connection, name: str):
    """Which one-edit neighbours of this name actually exist on npm."""
    import time
    t0 = time.perf_counter()
    cands = sorted(edit1(name))
    hits = []
    if cands:
        placeholders = ",".join("?" * len(cands))
        hits = [{"name": n, "latest": v,
                 "direct_dependents": db.execute(
                     "SELECT count(*) FROM deps WHERE dst = ?", (n,)).fetchone()[0]}
                for n, v in db.execute(
                    f"SELECT name, latest FROM packages WHERE name IN ({placeholders})",
                    cands)]
    ms = (time.perf_counter() - t0) * 1000.0
    return {"candidates": len(cands), "existing": sorted(
        hits, key=lambda r: -r["direct_dependents"])}, ms


def search(db: sqlite3.Connection, q: str, limit: int = 12):
    """Autocomplete over every package name the crawl has seen."""
    import time
    t0 = time.perf_counter()
    rows = db.execute(
        """SELECT name, latest, crawled FROM packages
           WHERE name LIKE ? ESCAPE '\\'
           ORDER BY crawled DESC, length(name) ASC, name ASC LIMIT ?""",
        (q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit)
    ).fetchall()
    ms = (time.perf_counter() - t0) * 1000.0
    return [{"name": n, "latest": v, "crawled": bool(c)} for n, v, c in rows], ms


# --------------------------------------------------------------------------
# semver: enough of npm's range grammar to be honest
# --------------------------------------------------------------------------

_V = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def _parse(v: str):
    m = _V.match(v.strip())
    return tuple(int(g) for g in m.groups()) if m else None


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


@lru_cache(maxsize=200_000)
def satisfies(version: str, rng: str) -> bool:
    """True if `version` satisfies npm range `rng`.

    Handles ^ ~ >= > <= < = * x || and hyphen ranges, which covers the
    overwhelming majority of real npm manifests. Anything unrecognised
    (git URLs, `file:`, `npm:` aliases, workspace protocols) returns False
    rather than guessing — under-reporting exposure is the safer error.
    """
    v = _parse(version)
    if v is None:
        return False
    rng = (rng or "").strip()
    if rng in ("", "*", "x", "latest", "next"):
        return True
    if "||" in rng:
        return any(satisfies(version, part) for part in rng.split("||"))
    if " - " in rng:
        lo, hi = rng.split(" - ", 1)
        return satisfies(version, f">={lo.strip()}") and satisfies(version, f"<={hi.strip()}")
    for comparator in rng.split():
        if not _one(v, comparator.strip()):
            return False
    return True


def _one(v, c: str) -> bool:
    if not c or c in ("*", "x"):
        return True
    if c.startswith("^"):
        b = _parse(c[1:])
        if not b:
            return False
        if b[0] > 0:
            return _cmp(v, b) >= 0 and v[0] == b[0]
        if b[1] > 0:
            return _cmp(v, b) >= 0 and v[:2] == b[:2]
        return v == b
    if c.startswith("~"):
        b = _parse(c[1:])
        return bool(b) and _cmp(v, b) >= 0 and v[:2] == b[:2]
    for op in (">=", "<=", ">", "<", "="):
        if c.startswith(op):
            b = _parse(c[len(op):])
            if not b:
                return False
            r = _cmp(v, b)
            return {">=": r >= 0, "<=": r <= 0, ">": r > 0,
                    "<": r < 0, "=": r == 0}[op]
    b = _parse(c)
    return bool(b) and v == b
