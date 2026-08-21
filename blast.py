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
from urllib.parse import quote

from hydra import Hydra, RESULT_LIMIT, pkg_id

MAX_DEPTH = 8

# The depth the console asks for, and the depth the server warms up to.
MAX_DEPTH_DEFAULT = 5

# A crawl whose last sidecar write is older than this is treated as stopped.
CRAWL_HEARTBEAT = 90.0


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


def resolve_package(h: Hydra, name: str, ecosystem: str = "npm"):
    """(known, latency_ms). The crawl runs in the background, so 'not here yet'
    is a normal answer during a demo, not an error — the API says so plainly
    instead of returning an empty blast radius that looks like safety."""
    rows, ms = h.timed(EXISTS, {"id": pkg_id(name, ecosystem)})
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


def blast_radius(h: Hydra, name: str, depth: int = 5, limit: int = 5000,
                 ecosystem: str = "npm"):
    """Everything that transitively depends on `name`, with a per-depth
    breakdown. Returns ({total, histogram, victims, truncated}, latency_ms).

    The d+1 queries are independent — each is its own bounded traversal from
    the same fixed source — so they are fired concurrently and the reported
    latency is wall-clock for the whole set, not the sum. Serially this is
    dominated by the deepest traversal repeated d times.
    """
    d = _depth(depth)
    target = pkg_id(name, ecosystem)
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

    # These d+1 queries run concurrently against a graph that continuous
    # ingestion is writing to, so they do not all observe the same instant: a
    # dependent added between two of them makes the count and the list disagree
    # by one. When nothing was truncated the enumerated list *is* the answer, so
    # it becomes the ground truth and the cumulative counts are clamped to it.
    # That keeps the histogram summing to the headline instead of shipping a
    # visible off-by-one that reads as a bug. Only a truncated page has to trust
    # the count instead, and it says so.
    truncated = len(victims) >= limit
    total = cumulative[-1] if cumulative else 0
    if not truncated:
        total = len(victims)
        cumulative = [min(c, total) for c in cumulative]

    # Differencing the cumulative reach gives packages *first* reached at k.
    histogram = []
    prev = 0
    for k, reached in enumerate(cumulative, start=1):
        histogram.append({"depth": k, "packages": max(reached - prev, 0)})
        prev = reached

    return {
        "total": total,
        "histogram": histogram,
        "victims": victims,
        "truncated": truncated,
        "depth": d,
        "queries": d + 1,
    }, ms_total


def victim_set(h: Hydra, name: str, depth: int = 5, limit: int = RESULT_LIMIT,
               ecosystem: str = "npm"):
    """Just the reachable names, as a set. Used by the lockfile check."""
    d = _depth(depth)
    rows, ms = h.timed(REACH_NAMES % d, {"id": pkg_id(name, ecosystem),
                                         "limit": min(limit, RESULT_LIMIT)})
    return {r["v.name"] for r in rows if r.get("v.name")}, ms


def subgraph(h: Hydra, db: sqlite3.Connection, name: str, depth: int = 3,
             per_level: int = 28, max_nodes: int = 160, ecosystem: str = "npm"):
    """A drawable slice of the blast radius: nodes tagged with the depth they
    are first reached at, plus the edges between them.

    Two layers again, each doing what it is good at. HydraDB supplies the
    authoritative reachable set at every depth — that is the traversal, and the
    `total` it reports is the real, untruncated number. The sidecar supplies
    the concrete edge list, because HydraDB 0.1.0 cannot return a path or a
    relationship binding, only endpoint properties.

    A depth-3 radius around a popular package reaches thousands of packages,
    which is not a picture. Each level is therefore capped at the most-depended
    -upon `per_level` nodes, expanded only from nodes already kept, so what is
    drawn is always a connected slice rather than scattered dots. The response
    says how much it left out; the UI says so too.
    """
    d = _depth(depth)
    target = pkg_id(name, ecosystem)
    t0 = time.perf_counter()

    # Authoritative reach per depth, from the graph.
    cumulative, reach = [], []
    with ThreadPoolExecutor(max_workers=d) as pool:
        jobs = [pool.submit(
            lambda k: {r["v.name"] for r in
                       h.query(REACH_NAMES % k, {"id": target,
                                                 "limit": RESULT_LIMIT})
                       if r.get("v.name")}, k) for k in range(1, d + 1)]
        for j in jobs:
            reach.append(j.result())
            cumulative.append(len(reach[-1]))

    # First-reached depth per package: in reach[k] but not reach[k-1].
    depth_of: dict[str, int] = {}
    seen: set[str] = set()
    for k, names in enumerate(reach, start=1):
        for n in names - seen:
            depth_of[n] = k
        seen |= names

    degree = {r[0]: r[1] for r in db.execute(
        "SELECT dst, count(*) FROM deps WHERE kind = 'prod' GROUP BY dst")}

    # Grow level by level from the root, keeping the best-connected nodes that
    # actually attach to something already drawn.
    kept: dict[str, int] = {name: 0}
    frontier = [name]
    edges: list[tuple[str, str]] = []
    for level in range(1, d + 1):
        if not frontier or len(kept) >= max_nodes:
            break
        placeholders = ",".join("?" * len(frontier))
        rows = db.execute(
            f"SELECT src, dst FROM deps WHERE dst IN ({placeholders}) "
            f"AND kind = 'prod'", frontier).fetchall()
        candidates = {}
        for src, dst in rows:
            if src in kept or depth_of.get(src) != level:
                continue
            candidates.setdefault(src, []).append(dst)
        ranked = sorted(candidates, key=lambda n: -degree.get(n, 0))
        room = min(per_level, max_nodes - len(kept))
        chosen = ranked[:max(room, 0)]
        for n in chosen:
            kept[n] = level
            for parent in candidates[n]:
                edges.append((parent, n))
        frontier = chosen

    # Edges among everything kept, not just the ones that introduced a node —
    # the cross-links are what make it look like a graph instead of a tree.
    if kept:
        placeholders = ",".join("?" * len(kept))
        names = list(kept)
        extra = db.execute(
            f"SELECT src, dst FROM deps WHERE kind = 'prod' "
            f"AND src IN ({placeholders}) AND dst IN ({placeholders})",
            names + names).fetchall()
        seen_edges = set(edges)
        for src, dst in extra:
            if (dst, src) not in seen_edges and src != dst:
                seen_edges.add((dst, src))
                edges.append((dst, src))

    ms = (time.perf_counter() - t0) * 1000.0
    total = cumulative[-1] if cumulative else 0
    return {
        "root": name,
        "depth": d,
        "total_exposed": total,
        "shown": len(kept) - 1,
        "truncated": (len(kept) - 1) < total,
        "nodes": [{"name": n, "depth": lvl, "dependents": degree.get(n, 0)}
                  for n, lvl in sorted(kept.items(), key=lambda kv: (kv[1], kv[0]))],
        "edges": [{"from": a, "to": b} for a, b in edges],
        "histogram": [{"depth": k, "packages": max(c - (cumulative[k - 2] if k > 1 else 0), 0)}
                      for k, c in enumerate(cumulative, start=1)],
    }, ms


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
    # A crawler that was killed rather than finished would leave its flag set
    # forever, so a stale heartbeat counts as stopped.
    try:
        heartbeat = time.time() - float(meta.get("updated_at", 0))
    except (TypeError, ValueError):
        heartbeat = 1e9
    running = meta.get("running") == "1" and heartbeat < CRAWL_HEARTBEAT

    return {
        "packages": packages,
        "edges": edges,
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "crawl": {
            "crawled": crawled,
            "known": packages,
            "queued": int(meta.get("queued", 0)),
            "running": running,
            "last_write_s": round(heartbeat, 1) if heartbeat < 1e8 else None,
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


_REGISTRY = "https://registry.npmjs.org"
_SLIM = {"Accept": "application/vnd.npm.install-v1+json",
         "User-Agent": "blast-radius-hackhydra/0.1"}


def _npm_exists(session, candidate: str, timeout: float):
    """Does this name exist on npm at all? Returns (name, latest) or None."""
    try:
        r = session.get(f"{_REGISTRY}/{quote(candidate, safe='@')}",
                        headers=_SLIM, timeout=timeout)
        if r.status_code != 200:
            return None
        return candidate, (r.json().get("dist-tags", {}) or {}).get("latest", "")
    except Exception:
        return None


def typosquat_ring(db: sqlite3.Connection, name: str, live: bool = True,
                   timeout: float = 4.0):
    """Which one-edit neighbours of this name are real packages.

    The crawled corpus is popularity-weighted, and a typosquat is by definition
    a package nobody depends on — so checking only the sidecar reliably returns
    nothing, which is worse than useless: it reads as "you are safe". The
    candidates are therefore checked against the npm registry itself, in
    parallel, and each hit is marked with whether it is also in our graph.

    If the registry is unreachable the corpus answer is still returned, with
    `live` false, rather than silently downgrading to "nothing found".
    """
    t0 = time.perf_counter()
    cands = sorted(edit1(name))
    in_corpus = {}
    if cands:
        placeholders = ",".join("?" * len(cands))
        in_corpus = {n: v for n, v in db.execute(
            f"SELECT name, latest FROM packages WHERE name IN ({placeholders})",
            cands)}

    found: dict[str, str] = dict(in_corpus)
    checked_live = False
    if live and cands:
        try:
            import requests
            session = requests.Session()
            with ThreadPoolExecutor(max_workers=min(10, len(cands))) as pool:
                for hit in pool.map(lambda c: _npm_exists(session, c, timeout), cands):
                    if hit:
                        found.setdefault(hit[0], hit[1])
            checked_live = True
        except Exception:
            checked_live = False

    hits = [{"name": n,
             "latest": v or "",
             "in_graph": n in in_corpus,
             "direct_dependents": db.execute(
                 "SELECT count(*) FROM deps WHERE dst = ?", (n,)).fetchone()[0]}
            for n, v in found.items()]
    ms = (time.perf_counter() - t0) * 1000.0
    return {"candidates": len(cands),
            "checked_live": checked_live,
            "existing": sorted(hits, key=lambda r: (-r["direct_dependents"], r["name"]))}, ms


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
# semver
# --------------------------------------------------------------------------
#
# npm's range grammar moved to ecosystems/npm.py when the adapter layer landed.
# Every ecosystem has its own, and they disagree in ways that silently invert
# an answer, so there is no shared implementation to fall back on. Re-exported
# here because callers and tests predate the move.

from ecosystems.npm import satisfies                              # noqa: E402,F401
