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
