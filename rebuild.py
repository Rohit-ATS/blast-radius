"""Rebuild the HydraDB graph from the deps.db sidecar — no network, no re-crawl.

Why this exists
---------------
HydraDB 0.1.0 on the local-filesystem object store cannot update an existing
SlateDB manifest:

    object store error: Operation `put_opts` with mode `PutMode::Update`
    not yet implemented by LocalFileSystem(file:///data/store)

The first boot creates the manifest and everything works. Every boot *after*
that leaves the store permanently read-only: reads keep answering perfectly,
and every write fails with a 500 "internal query execution error" until the
store directory is recreated.

That would normally mean a ~20-minute re-crawl of the npm registry to get back
to a writable graph. It does not have to: the sidecar already holds every
package and every dependency edge that was ever written to the graph, so the
topology can be replayed locally in about a minute.

  py rebuild.py            # wipe the store, restart, replay from deps.db
  py rebuild.py --verify   # just check whether the graph is writable

This also makes the graph reproducible for anyone cloning the repo with a
deps.db in hand, and it is how `verify.py` gets a clean, writable system back
after `chaos.py` has restarted the container.
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import time

from hydra import Hydra, HydraError, nid
from ingest import CREATE_EDGES, UPSERT_PACKAGES, UPSERT_STUBS, DEPS_DB

ROOT = os.path.dirname(os.path.abspath(__file__))
PROBE_ID = 999999999999998


def writable(h: Hydra) -> tuple[bool, str]:
    """Round-trip a real write. A read-only store answers reads perfectly, so
    only a write tells you the truth about this failure mode."""
    try:
        h.query("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:_Probe",
                {"rows": [{"id": PROBE_ID}]}, retries=1)
        h.query("MATCH (p {id: $id}) DETACH DELETE p", {"id": PROBE_ID}, retries=1)
        return True, "writes round-trip"
    except HydraError as e:
        detail = str(e)
        if "PutMode::Update" in detail or "internal query execution error" in detail:
            return False, ("store is read-only — the manifest cannot be updated "
                           "on the local filesystem backend after a restart")
        return False, detail[:200]


def recreate_store() -> None:
    print("[reset] stopping hydradb")
    subprocess.run(["docker", "compose", "stop", "hydradb"], cwd=ROOT,
                   capture_output=True, timeout=300)
    store = os.path.join(ROOT, ".hydradb", "store")
    cache = os.path.join(ROOT, ".hydradb", "cache")
    for path in (store, cache):
        if os.path.exists(path):
            print(f"[reset] removing {os.path.relpath(path, ROOT)}")
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", path],
                           capture_output=True, timeout=300)
        os.makedirs(path, exist_ok=True)
    print("[reset] starting hydradb")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT,
                   capture_output=True, timeout=300)


def replay(h: Hydra, db: sqlite3.Connection, chunk: int) -> tuple[int, int]:
    """Write every vertex, then every edge, exactly as ingest.py would have."""
    packages = db.execute(
        "SELECT name, latest FROM packages ORDER BY nid").fetchall()
    print(f"[replay] {len(packages):,} vertices")
    full = [{"id": nid(n), "name": n, "latest": v or ""}
            for n, v in packages if v]
    stubs = [{"id": nid(n), "name": n} for n, v in packages if not v]
    h.write_batch(UPSERT_STUBS, stubs, chunk=chunk)
    h.write_batch(UPSERT_PACKAGES, full, chunk=chunk)

    edges = db.execute(
        "SELECT src, dst FROM deps WHERE kind = 'prod'").fetchall()
    print(f"[replay] {len(edges):,} edges")
    # Reversed, exactly as ingest.py writes them: dependency -> dependent.
    rows = ({"src": nid(dst), "dst": nid(src)} for src, dst in edges)
    h.write_batch(CREATE_EDGES, rows, chunk=chunk)
    return len(packages), len(edges)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DEPS_DB)
    p.add_argument("--chunk", type=int, default=500)
    p.add_argument("--verify", action="store_true",
                   help="only report whether the graph is writable")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation before wiping the store")
    args = p.parse_args()

    h = Hydra(timeout=180.0)
    ok, why = writable(h)
    print(f"[check] graph writable: {ok} — {why}")
    if args.verify:
        return 0 if ok else 1
    if ok:
        print("[check] nothing to rebuild; the store already accepts writes.")
        print("        pass --yes to rebuild anyway.")
        if not args.yes:
            return 0

    if not os.path.exists(args.db):
        print(f"[fatal] {args.db} not found — nothing to replay from. "
              f"Run ingest.py instead.", file=sys.stderr)
        return 1
    db = sqlite3.connect(args.db, timeout=30)
    n_pkg = db.execute("SELECT count(*) FROM packages").fetchone()[0]
    n_edge = db.execute(
        "SELECT count(*) FROM deps WHERE kind='prod'").fetchone()[0]
    print(f"[plan] replay {n_pkg:,} packages and {n_edge:,} edges from {args.db}")

    started = time.time()
    recreate_store()
    h.wait_ready()
    ok, why = writable(h)
    if not ok:
        print(f"[fatal] store still not writable after recreate: {why}",
              file=sys.stderr)
        return 1

    pkgs, edges = replay(h, db, args.chunk)

    # Warm before counting: a whole-graph edge scan on a freshly written store
    # exceeds HydraDB's own 30-second query timeout. Walking the depths first
    # pages the working set in.
    for depth in range(1, 6):
        try:
            h.query(f"MATCH (t {{id: $id}})-[:REQUIRED_BY*1..{depth}]->(v) "
                    f"RETURN count(*)", {"id": nid("debug")}, retries=2)
        except HydraError:
            pass
    got_p = h.query("MATCH (p:Package) RETURN count(*)", retries=6)[0]["count(*)"]
    got_e = h.query("MATCH ()-[r:REQUIRED_BY]->() RETURN count(*)",
                    retries=6)[0]["count(*)"]
    print(f"[verify] graph now holds {got_p:,} vertices and {got_e:,} edges "
          f"(sidecar: {pkgs:,} / {edges:,})")
    print(f"[done] {time.time() - started:.0f}s")
    return 0 if (got_p == pkgs and got_e == edges) else 1


if __name__ == "__main__":
    sys.exit(main())
