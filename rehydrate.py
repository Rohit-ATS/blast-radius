"""Rebuild the HydraDB graph from the sidecar.

Why this exists
---------------
The two stores hold different halves of the same crawl. Topology — which
package requires which — lives in the graph, because that is the question
HydraDB is fast at: a depth-5 reverse traversal over a million edges in a few
milliseconds. The predicates that decorate it — declared semver ranges,
maintainers, versions — live in Postgres, because those are joins and counts,
which a graph engine has no reason to be good at.

That split has a useful consequence. `deps` in Postgres is a complete record of
every edge the crawler has ever written, and it is durable. So the graph is
*derived* data: if it is ever lost, it can be rebuilt from the sidecar without
touching the npm registry at all. Recrawling 30,000 packages takes hours and
hammers someone else's API; replaying edges we already hold takes seconds and
hammers nobody.

That turns the graph's storage into a cache, which is what makes the
single-service deployment work: the graph node can run on an ephemeral
filesystem, and a restart costs a rebuild rather than a recrawl.

The load is idempotent by construction. Vertices are MERGEd on their id, so
replaying them is a no-op. Edges are CREATEd — HydraDB 0.1.0 rejects MERGE
inside UNWIND — so this refuses to run against a graph that already holds
edges, rather than silently doubling every one of them. See `needed()`.
"""

from __future__ import annotations

import os
import time

import ingest
import sidecar
from hydra import Hydra, HydraError, pkg_id

# HydraDB's admission control caps a single UNWIND at 1024 items:
#     429 resource_exhausted: client_query_batch_items rejected by admission
#     control: actual 5000 exceeds limit 1024
# It is a hard server-side limit, not a tuning knob, so these sit just under it
# rather than at some round number that happens to fit today.
VERTEX_BATCH = 1_000
EDGE_BATCH = 1_000


# Packages whose dependents are loaded whatever the budget says.
#
# A bounded rebuild spends its edges most-depended-upon first, which is the
# right instinct — during an incident people ask about packages a lot of things
# depend on — but applied alone it is precisely wrong for this tool. Measured at
# REHYDRATE_MAX_EDGES=55000, the cut lands at 28 dependents, and every package
# below that line has *no* edges at all. Look at what that excludes:
#
#     debug           746 dependents    in
#     chalk           871 dependents    in
#     ua-parser-js     16 dependents    OUT
#     rc               12 dependents    OUT
#     event-stream     11 dependents    OUT
#     coa               1 dependent     OUT
#     node-ipc          1 dependent     OUT
#
# That is a list of actual npm supply-chain compromises, and popularity ranking
# drops all of them. It is not bad luck: attackers pick small packages buried in
# the tree, because those are the ones nobody is watching — which is the same
# property that puts them below any popularity cutoff. Ranking by dependents
# systematically evicts exactly the packages this tool was built to answer for.
#
# So they are pinned, and their whole reverse closure is pinned with them — see
# PIN_MAX_EDGES. Measured against the 55,000-edge budget, holding every one of
# the evicted packages complete to depth 5 costs 1,000 edges, under 2% of the
# budget. Which is the whole point: being unpopular is what put them below the
# cut, and it is the same thing that makes them nearly free to keep.
#
# REHYDRATE_PIN (comma-separated) adds to this list; set it when an incident
# breaks and the package is not yet here.
PINNED = (
    "event-stream", "flatmap-stream",   # Nov 2018, the bitcoin-wallet backdoor
    "eslint-scope",                     # Jul 2018, stolen npm credentials
    "getcookies",                       # May 2018, backdoor via express-cookies
    "ua-parser-js",                     # Oct 2021, maintainer account takeover
    "coa", "rc",                        # Nov 2021, the same week, same method
    "node-ipc",                         # Mar 2022, protestware from the author
    "colors", "faker",                  # Jan 2022, sabotaged by the author
    "debug", "chalk",                   # Sep 2025, the phishing-led takeover
)


def pinned_packages() -> list[str]:
    extra = os.environ.get("REHYDRATE_PIN", "")
    names = list(PINNED) + [p.strip() for p in extra.split(",") if p.strip()]
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# How far the pinned set is chased, and how much it may spend doing it.
#
# Direct dependents are not enough. Pinning only those gave event-stream its 11
# direct dependents and stopped, so the console answered 11 against a true blast
# radius of 26 — better than the 0 it answered before, and still wrong, because
# the dependents *of* those 11 were never loaded. A traversal is only as deep as
# the edges under it.
#
# So the closure is chased to exhaustion — not to a fixed depth. A depth limit
# here has to be at least the deepest traversal the API will accept, and that is
# blast.MAX_DEPTH = 8, not the 5 the console asks for; pinning to 5 and then
# telling a depth-8 query its answer was exact is the same false confidence
# this whole mechanism exists to prevent. Running the walk out has no downside
# because it terminates on its own — `seen` closes the cycles — and PIN_MAX_EDGES
# bounds it regardless.
#
# For the packages this list exists for that is nearly free: the entire reverse
# closure of event-stream is 33 edges, ua-parser-js 134, rc 67 — cheap for the
# same reason their direct sets were, which is that nothing much depends on them.
#
# The cap is what makes it safe to put a popular package in PINNED anyway.
# debug's closure is 9,755 edges and chalk's is 4,241, which would be a sixth of
# the entire budget spent re-buying edges the popularity ranking already bought.
# So pins are walked cheapest-first and the walk stops at PIN_MAX_EDGES: the
# small incident packages — the ones the ranking actually drops — are served
# completely, and the popular ones are left to the budget that mostly covers
# them already.
#
# Verified against the sidecar at REHYDRATE_MAX_EDGES=55000. Every package the
# ranking evicted now answers exactly, and the walk stops inside debug:
#
#     coa 3, node-ipc 1, faker 2, event-stream 26, rc 61,
#     eslint-scope 260, ua-parser-js 83, colors 162     — all exact
#     debug, chalk                                      — left to the budget
#
# `debug` and `chalk` stay partial on a bounded instance, and /api/blast says so
# on the response rather than presenting a floor as a total. See coverage_check
# in server.py.
PIN_MAX_EDGES = 1_000
FRONTIER_CHUNK = 400        # keeps the IN list under sqlite's parameter limit


def _pin_order(conn, pins: list[str]) -> list[str]:
    """Pinned packages cheapest-first, by direct dependent count.

    Ascending is the whole trick. The packages at the front are the ones the
    popularity ranking evicted, and they are also the ones whose closures cost
    almost nothing — so they are all served long before the cap is in sight.
    """
    counts = {}
    for i in range(0, len(pins), FRONTIER_CHUNK):
        chunk = pins[i:i + FRONTIER_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for name, n in conn.execute(
                f"SELECT dst, count(*) FROM deps "
                f"WHERE kind = 'prod' AND dst IN ({marks}) GROUP BY dst",
                tuple(chunk)):
            counts[name] = n
    return sorted((p for p in pins if p in counts), key=lambda p: counts[p])


def pinned_edges(conn, have: set, pins: list[str],
                 log=print) -> tuple[list[tuple], list[str]]:
    """Reverse closure of the pinned set, minus what the budget already holds.

    Returns the edges added and the names whose closure was walked to the end.
    That second list is the point of the exercise: it is the set the API can
    describe as exact on an otherwise bounded graph, so `coverage_check` can
    stop warning that a number is a floor when it is not. A pin the cap cut
    short is deliberately absent from it.
    """
    cap = int(os.environ.get("REHYDRATE_PIN_MAX_EDGES") or PIN_MAX_EDGES)
    added: list[tuple] = []
    complete: list[str] = []
    stopped_at = None

    for name in _pin_order(conn, pins):
        if len(added) >= cap:
            stopped_at = stopped_at or name
            break
        seen, frontier = {name}, [name]
        while frontier and len(added) < cap:
            rows = []
            for i in range(0, len(frontier), FRONTIER_CHUNK):
                chunk = frontier[i:i + FRONTIER_CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows.extend(tuple(r) for r in conn.execute(
                    f"SELECT d.dst, d.src FROM deps d "
                    f"WHERE d.kind = 'prod' AND d.dst IN ({marks})",
                    tuple(chunk)))
            nxt = []
            for edge in rows:
                # Checked per edge, not per level. Checking between levels let a
                # single level of a popular package overshoot a 2,500 cap to
                # 4,488 — the cap has to bind where the spending happens.
                if len(added) >= cap:
                    stopped_at = stopped_at or name
                    break
                if edge not in have:
                    have.add(edge)
                    added.append(edge)
                dependent = edge[1]
                if dependent not in seen:
                    seen.add(dependent)
                    nxt.append(dependent)
            frontier = nxt
        # Walked the whole closure without the cap biting, so this one is exact
        # at any depth the API accepts.
        if len(added) < cap:
            complete.append(name)

    if stopped_at:
        # Said out loud rather than left implicit. A silently truncated pin set
        # reads exactly like a complete one from the outside.
        log(f"[rehydrate] pin budget of {cap} edges reached at '{stopped_at}'; "
            f"the popular pins past it are left to the main budget")
    return added, complete


# Anchored on a single id rather than `MATCH ()-[r:REQUIRED_BY]->()`. The
# anonymous form is a full scan — HydraDB says so itself ("access_path
# AllVertexScan") and there is no CREATE INDEX in 0.1.0 to fix it with, so it
# takes over a minute on a graph this size. That is an absurd price for a
# yes/no question asked on every boot, and it would sit in front of the port
# binding while Render waited for the service to come up.
PROBE = "MATCH (t {id: $id})-[:REQUIRED_BY*1..1]->(v) RETURN count(*)"


def _busiest_package(conn) -> str | None:
    """The package with the most dependents, as the sidecar sees it.

    Using the most-connected node makes the probe decisive: if anything at all
    was loaded, this one has edges.
    """
    row = conn.execute(
        "SELECT dst FROM deps GROUP BY dst ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def graph_is_empty(h: Hydra, probe: str | None = None) -> bool:
    """True when the graph holds no edges for the busiest known package.

    Deliberately asks about edges rather than vertices. A half-finished load
    leaves vertices behind, and treating those as "already loaded" would strand
    the graph with nodes and no topology — which answers every traversal with
    zero rather than with an error, the worst possible failure for this tool.
    """
    if probe is None:
        conn = sidecar.connect(read_only=True)
        try:
            probe = _busiest_package(conn)
        finally:
            conn.close()
    if not probe:
        return True                      # nothing in the sidecar either
    rows = h.query(PROBE, {"id": pkg_id(probe)})
    return (rows[0]["count(*)"] if rows else 0) == 0


def needed(h: Hydra) -> bool:
    try:
        return graph_is_empty(h)
    except HydraError:
        return False        # cannot tell; let the caller's readiness wait deal with it


# Pacing, because the graph node is not the only thing in the container.
#
# Writing flat out, graph-node's resident size went 13 -> 129 -> 221 -> 295 ->
# 443MiB and the cgroup killed it two thirds of the way through the load. It
# was not leaking: SlateDB buffers writes in a memtable and flushes and
# compacts them in the background, and a bulk load with no backpressure simply
# produces work faster than the flusher retires it. Nothing logged an error,
# because from the database's point of view nothing went wrong.
#
# So the loader waits. PAUSE_EVERY chunks in, it sleeps long enough for a flush
# to land. That trades a slower rebuild — still well under a minute — for a
# peak that fits the instance, which is the right trade for something that runs
# once at boot.
PAUSE_EVERY = 25
PAUSE_SECONDS = 0.35


def _paced(h: Hydra, cypher, rows, log, label: str) -> int:
    buf, total, chunks = [], 0, 0
    size = VERTEX_BATCH if label == "vertices" else EDGE_BATCH

    def flush():
        nonlocal total, chunks
        if not buf:
            return
        h.query(cypher, {"rows": buf})
        total += len(buf)
        chunks += 1
        buf.clear()
        if chunks % PAUSE_EVERY == 0:
            time.sleep(PAUSE_SECONDS)
            log(f"[rehydrate] {label} {total}")

    for row in rows:
        buf.append(row)
        if len(buf) >= size:
            flush()
    flush()
    return total


# Where the rebuild leaves a note for the API process.
#
# The two share a container but not an address space, and the API cannot work
# out on its own which packages this rebuild managed to hold complete — it would
# have to walk the sidecar's closure per request, which is the traversal the
# graph exists to avoid. So the loader writes down what it did.
#
# Without this the API has to be conservative and warn that *every* answer on a
# bounded graph is a floor, which is true in general and false for exactly the
# packages the pinning went to the trouble of completing. Warning that
# event-stream's 26 might be an undercount, when it is the same 26 the full
# graph gives, trains people to ignore the warning that matters.
MANIFEST = os.environ.get("REHYDRATE_MANIFEST") or os.path.join(
    os.environ.get("GRAPH_DIR", "/data"), "rehydrate.json")


def write_manifest(*, bounded: bool, edges: int, vertices: int,
                   exact: list[str], log=print) -> None:
    import json
    payload = {"bounded": bounded, "edges": edges, "vertices": vertices,
               "exact": sorted(exact), "at": time.time()}
    try:
        os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
        tmp = MANIFEST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, MANIFEST)         # never leave a half-written manifest
    except OSError as e:
        # Not fatal. A missing manifest costs precision in the coverage note,
        # not correctness: the API falls back to warning about the whole graph.
        log(f"[rehydrate] could not write {MANIFEST}: {e}")


def run(h: Hydra, log=print, limit: int | None = None) -> dict:
    """Replay the sidecar's topology into the graph. Returns what it did."""
    if limit is None:
        env = os.environ.get("REHYDRATE_MAX_EDGES", "").strip()
        limit = int(env) if env.isdigit() and int(env) > 0 else None
    t0 = time.perf_counter()
    conn = sidecar.connect(read_only=True)
    try:
        # Two things here must match the crawler exactly, because this writes
        # into the same graph its edges live in.
        #
        # MAX_EDGES bounds the rebuild to what the instance can hold.
        #
        # graph-node's resident size tracks the size of the graph: the full
        # 137,688-edge set settles at ~812MiB, which needs a ~1GB instance. On a
        # 512MiB one it has to be smaller, and *which* edges are dropped decides
        # whether the smaller graph is still worth having.
        #
        # So they are ordered by how many dependents the dependency has. The
        # question this tool answers is "a package was compromised, who is
        # exposed", and the packages anyone asks that about are the ones with
        # many dependents. Ordering by dependent count keeps those complete and
        # drops the long tail of packages nobody depends on, rather than
        # truncating arbitrarily and leaving every answer slightly wrong.
        #
        # The API reports coverage on every response, so a bounded graph is
        # visible to the caller rather than silently partial.
        #
        # kind = 'prod': only `dependencies` are installed transitively. dev and
        # peer dependencies are in the sidecar because the semver questions need
        # them, but they are not blast radius, and the crawler does not make
        # edges from them. Loading all 158,003 rows instead of the 137,688 prod
        # ones would have inflated every traversal by a sixth.
        #
        # The direction is reversed on purpose, and it is reversed relative to
        # this table. A sidecar row reads "src depends on dst"; a graph edge
        # reads (dependency)-[:REQUIRED_BY]->(dependent), which is what makes
        # "who is exposed to X" a forward walk from X. So the graph's src is the
        # row's dst.
        if limit is None:
            cur = conn.execute(
                "SELECT d.dst, d.src FROM deps d "
                "JOIN (SELECT dst, count(*) AS n FROM deps "
                "      WHERE kind = 'prod' GROUP BY dst) pop ON pop.dst = d.dst "
                "WHERE d.kind = 'prod' ORDER BY pop.n DESC")
        else:
            cur = conn.execute(
                "SELECT d.dst, d.src FROM deps d "
                "JOIN (SELECT dst, count(*) AS n FROM deps "
                "      WHERE kind = 'prod' GROUP BY dst) pop ON pop.dst = d.dst "
                "WHERE d.kind = 'prod' ORDER BY pop.n DESC LIMIT ?", (limit,))

        # ---- edges first, so the vertices can be derived from them --------
        #
        # Order matters here, and getting it wrong produces the worst output
        # this tool can produce. The obvious version loads every package as a
        # vertex and then as many edges as fit. On a bounded rebuild that gives
        # a package with no loaded edges a vertex anyway, so the traversal
        # succeeds and returns zero — and the page says "0 packages exposed"
        # about a package it simply has no data for. Measured: with a 60k-edge
        # budget, event-stream reported 0 exposed rather than 26.
        #
        # A false all-clear during a supply-chain incident is worse than no
        # answer at all. So the edge set is chosen first and the vertex set is
        # exactly the packages those edges touch. A package outside it has no
        # vertex, `known()` reports it as not in the graph, and the API says so
        # instead of inventing a reassuring zero.
        edges = list(cur)

        # The pinned set, added on top of the budget rather than inside it.
        #
        # Only meaningful when the rebuild is bounded — an unbounded one already
        # holds every edge, and these queries return rows it has. Deduped
        # against what the budget already bought, so pinning `debug` (which is
        # popular enough to be loaded anyway) costs nothing and is not an error.
        # See PINNED for why popularity ranking cannot be trusted to keep these.
        pins = pinned_packages() if limit is not None else []
        exact: list[str] = []
        if pins:
            # Normalised to tuples on both sides: sqlite3 and psycopg hand back
            # different row objects, and only one of them compares equal to a
            # plain tuple.
            have = {tuple(r) for r in edges}
            extra, exact = pinned_edges(conn, have, pins, log)
            if extra:
                edges.extend(extra)
                log(f"[rehydrate] +{len(extra)} pinned edges so the incident "
                    f"packages the popularity cut drops answer completely")

        names = {n for pair in edges for n in pair}
        log(f"[rehydrate] {len(edges)} edges touching {len(names)} packages")

        sent_v = _paced(
            h, ingest.UPSERT_STUBS,
            ({"id": pkg_id(n), "name": n, "ecosystem": "npm"} for n in sorted(names)),
            log, "vertices")
        log(f"[rehydrate] {sent_v} vertices in {time.perf_counter() - t0:.1f}s")


        sent_e = _paced(
            h, ingest.CREATE_EDGES,
            ({"src": pkg_id(dep), "dst": pkg_id(dependent)} for dep, dependent in edges),
            log, "edges")

        took = time.perf_counter() - t0
        log(f"[rehydrate] {sent_v} vertices, {sent_e} edges in {took:.1f}s")
        write_manifest(bounded=limit is not None, edges=sent_e,
                       vertices=sent_v, exact=exact, log=log)
        return {"vertices": sent_v, "edges": sent_e, "seconds": round(took, 1)}
    finally:
        conn.close()


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--force", action="store_true",
                   help="load even if the graph already holds edges (will duplicate)")
    p.add_argument("--limit", type=int, default=None, help="for a smoke test")
    a = p.parse_args()

    h = Hydra()
    h.wait_ready()
    if not a.force and not graph_is_empty(h):
        print("[rehydrate] graph already holds edges; nothing to do "
              "(use --force to load anyway)")
        return
    run(h, limit=a.limit)


if __name__ == "__main__":
    main()
