"""Move every entity into HydraDB, so the graph is the product and not a cache.

Until now only package->package edges lived in the graph; maintainers,
advisories and name-similarity sat in SQLite, which made the whole thing look
like a lookup table with a graph bolted on. Those three are *relationships*,
and relationships belong in the graph:

    (:Maintainer {id: nid("maint:"+name), name})-[:MAINTAINS]->(:Package)
    (:Advisory   {id: nid("adv:"+osv_id), osv_id, severity, is_malware})-[:AFFECTS]->(:Package)
    (:Package)-[:SIMILAR_TO]->(:Package)        # written both ways

Namespaced ids ("maint:qix", "adv:MAL-2025-46974") keep the three id spaces from
colliding inside nid()'s single integer space.

What stays in SQLite, deliberately: declared semver ranges. HydraDB 0.1.0
cannot filter on edge properties during a traversal, so a range stored on an
edge would be unreadable at exactly the moment it matters. That is a real
constraint, not a preference, and it is documented rather than hidden.

  py graphify.py                 # all three
  py graphify.py --only advisories --limit 4000
"""

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from hydra import Hydra, nid
from ingest import DEPS_DB, open_sidecar
import blast
import intel

UPSERT_NODES = """
UNWIND $rows AS row
MERGE (n {id: row.id})
SET n:%s, n.name = row.name
"""

UPSERT_ADVISORIES = """
UNWIND $rows AS row
MERGE (n {id: row.id})
SET n:Advisory, n.name = row.name, n.osv_id = row.osv_id,
    n.severity = row.severity, n.is_malware = row.is_malware,
    n.summary = row.summary
"""

CREATE_EDGE = """
UNWIND $rows AS row
CREATE (a {id: row.src})-[:%s]->(b {id: row.dst})
"""

# Written edges are recorded so re-running is idempotent: HydraDB has no MERGE
# for edges inside UNWIND, so the only way not to duplicate is to remember.
LEDGER = """
CREATE TABLE IF NOT EXISTS graph_edges (
    kind TEXT NOT NULL,
    src  TEXT NOT NULL,
    dst  TEXT NOT NULL,
    PRIMARY KEY (kind, src, dst)
);
CREATE TABLE IF NOT EXISTS advisories (
    osv_id     TEXT NOT NULL,
    package    TEXT NOT NULL,
    severity   TEXT,
    is_malware INTEGER NOT NULL DEFAULT 0,
    summary    TEXT,
    PRIMARY KEY (osv_id, package)
);
"""


def new_edges(db, kind, pairs):
    """Filter to pairs not already written, and record the ones we keep."""
    fresh = []
    for src, dst in pairs:
        cur = db.execute(
            "INSERT OR IGNORE INTO graph_edges (kind, src, dst) VALUES (?,?,?)",
            (kind, src, dst))
        if cur.rowcount:
            fresh.append((src, dst))
    db.commit()
    return fresh


# --------------------------------------------------------------------------

def do_maintainers(h, db, chunk):
    rows = db.execute("SELECT maintainer, package FROM maintainers").fetchall()
    names = sorted({m for m, _ in rows})
    print(f"[maintainers] {len(names):,} people, {len(rows):,} relationships")

    h.write_batch(UPSERT_NODES % "Maintainer",
                  [{"id": nid("maint:" + m), "name": m} for m in names],
                  chunk=chunk)

    fresh = new_edges(db, "MAINTAINS", rows)
    print(f"[maintainers] {len(fresh):,} new edges")
    h.write_batch(CREATE_EDGE % "MAINTAINS",
                  [{"src": nid("maint:" + m), "dst": nid(p)} for m, p in fresh],
                  chunk=chunk)

    # The reverse edge as well. A traversal needs a fixed source id, so without
    # this there is no way to ask "who maintains this package" from the package
    # — you would have to fall back to SQL, which is exactly what this migration
    # exists to stop doing.
    # Derive from every MAINTAINS edge ever written, not just this run's new
    # ones — otherwise a re-run adds no reverse edges because `fresh` is empty.
    all_maintains = db.execute(
        "SELECT src, dst FROM graph_edges WHERE kind = 'MAINTAINS'").fetchall()
    back = new_edges(db, "MAINTAINED_BY", [(p, m) for m, p in all_maintains])
    print(f"[maintainers] {len(back):,} new reverse edges")
    h.write_batch(CREATE_EDGE % "MAINTAINED_BY",
                  [{"src": nid(p), "dst": nid("maint:" + m)} for p, m in back],
                  chunk=chunk)
    return len(names), len(fresh)


def do_advisories(h, db, chunk, limit):
    """Ask OSV about every crawled package at its latest version, in batch."""
    pkgs = db.execute(
        "SELECT name, latest FROM packages WHERE crawled = 1 AND latest != '' "
        "ORDER BY nid LIMIT ?", (limit,)).fetchall()
    print(f"[advisories] querying OSV for {len(pkgs):,} packages")

    flagged: dict[str, list[str]] = {}
    for i in range(0, len(pkgs), 900):
        batch = intel.osv_batch(pkgs[i:i + 900])
        if batch.get("ok"):
            flagged.update(batch["hits"])
        print(f"[advisories]   {min(i + 900, len(pkgs)):,}/{len(pkgs):,} "
              f"-> {len(flagged):,} flagged", flush=True)
    # Backfill the reverse edges from everything ever written before the early
    # return below, so a run that flags nothing new still repairs them.
    prior = db.execute(
        "SELECT src, dst FROM graph_edges WHERE kind = 'AFFECTS'").fetchall()
    repair = new_edges(db, "HAS_ADVISORY", [(p, a) for a, p in prior])
    if repair:
        print(f"[advisories] {len(repair):,} reverse edges backfilled")
        h.write_batch(CREATE_EDGE % "HAS_ADVISORY",
                      [{"src": nid(p), "dst": nid("adv:" + a)} for p, a in repair],
                      chunk=chunk)

    if not flagged:
        print("[advisories] nothing flagged")
        return 0, 0

    # The batch endpoint returns ids only; detail comes from a per-package
    # query, which is also what tells us malware from ordinary vulnerability.
    def detail(key):
        name, _, version = key.rpartition("@")
        return name, intel.osv_query(name, version)

    records: dict[str, dict] = {}
    links: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for done, (name, res) in enumerate(pool.map(detail, list(flagged)), 1):
            for v in res.get("vulns", []):
                records[v["id"]] = {
                    "osv_id": v["id"],
                    "severity": v.get("severity", "") or "",
                    "is_malware": v["kind"] == "malware",
                    "summary": (v.get("summary") or "")[:200],
                }
                links.append((v["id"], name))
                db.execute(
                    "INSERT OR REPLACE INTO advisories "
                    "(osv_id, package, severity, is_malware, summary) VALUES (?,?,?,?,?)",
                    (v["id"], name, v.get("severity", ""),
                     1 if v["kind"] == "malware" else 0,
                     (v.get("summary") or "")[:200]))
            if done % 200 == 0:
                db.commit()
                print(f"[advisories]   detailed {done:,}/{len(flagged):,}", flush=True)
    db.commit()

    malware = sum(1 for r in records.values() if r["is_malware"])
    print(f"[advisories] {len(records):,} advisories ({malware:,} malware), "
          f"{len(links):,} links")

    h.write_batch(UPSERT_ADVISORIES,
                  [{"id": nid("adv:" + r["osv_id"]), "name": r["osv_id"], **r}
                   for r in records.values()], chunk=chunk)
    fresh = new_edges(db, "AFFECTS", links)
    h.write_batch(CREATE_EDGE % "AFFECTS",
                  [{"src": nid("adv:" + a), "dst": nid(p)} for a, p in fresh],
                  chunk=chunk)
    all_affects = db.execute(
        "SELECT src, dst FROM graph_edges WHERE kind = 'AFFECTS'").fetchall()
    back = new_edges(db, "HAS_ADVISORY", [(p, a) for a, p in all_affects])
    h.write_batch(CREATE_EDGE % "HAS_ADVISORY",
                  [{"src": nid(p), "dst": nid("adv:" + a)} for p, a in back],
                  chunk=chunk)
    return len(records), len(fresh)


def do_similar(h, db, chunk, top):
    """Name-similarity edges among packages we already know about.

    Only names that exist are linked, and only for the packages worth
    impersonating — a typosquat of something with no dependents is nobody's
    problem. Both directions are written so a traversal from either end works
    with a fixed source id.
    """
    known = {n for (n,) in db.execute("SELECT name FROM packages")}
    targets = [n for (n,) in db.execute(
        "SELECT dst FROM deps WHERE kind='prod' GROUP BY dst "
        "ORDER BY count(*) DESC LIMIT ?", (top,))]
    print(f"[similar] {len(targets):,} high-value names against "
          f"{len(known):,} known packages")

    pairs = []
    for name in targets:
        for cand in blast.edit1(name):
            if cand in known and cand != name:
                pairs.append((name, cand))
                pairs.append((cand, name))
    fresh = new_edges(db, "SIMILAR_TO", pairs)
    print(f"[similar] {len(fresh):,} new edges "
          f"({len({a for a, _ in fresh}):,} names involved)")
    h.write_batch(CREATE_EDGE % "SIMILAR_TO",
                  [{"src": nid(a), "dst": nid(b)} for a, b in fresh],
                  chunk=chunk)
    return len(fresh)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DEPS_DB)
    p.add_argument("--chunk", type=int, default=500)
    p.add_argument("--limit", type=int, default=30000,
                   help="packages to ask OSV about")
    p.add_argument("--top", type=int, default=4000,
                   help="most-depended-on names to build SIMILAR_TO around")
    p.add_argument("--only", choices=("maintainers", "advisories", "similar"))
    args = p.parse_args()

    h = Hydra(timeout=180.0)
    h.wait_ready()
    db = open_sidecar(args.db)
    db.executescript(LEDGER)
    db.commit()

    started = time.time()
    if args.only in (None, "maintainers"):
        do_maintainers(h, db, args.chunk)
    if args.only in (None, "advisories"):
        do_advisories(h, db, args.chunk, args.limit)
    if args.only in (None, "similar"):
        do_similar(h, db, args.chunk, args.top)

    print(f"\n[graphify] done in {time.time() - started:.0f}s")
    for label, q in (("Package", "MATCH (n:Package) RETURN count(*)"),
                     ("Maintainer", "MATCH (n:Maintainer) RETURN count(*)"),
                     ("Advisory", "MATCH (n:Advisory) RETURN count(*)")):
        try:
            print(f"  {label:<11}", f"{h.query(q)[0]['count(*)']:,}")
        except Exception as e:
            print(f"  {label:<11} count failed: {str(e)[:60]}")
    for label, q in (("REQUIRED_BY", "MATCH ()-[r:REQUIRED_BY]->() RETURN count(*)"),
                     ("MAINTAINS", "MATCH ()-[r:MAINTAINS]->() RETURN count(*)"),
                     ("AFFECTS", "MATCH ()-[r:AFFECTS]->() RETURN count(*)"),
                     ("SIMILAR_TO", "MATCH ()-[r:SIMILAR_TO]->() RETURN count(*)"),
                     ("MAINTAINED_BY", "MATCH ()-[r:MAINTAINED_BY]->() RETURN count(*)"),
                     ("HAS_ADVISORY", "MATCH ()-[r:HAS_ADVISORY]->() RETURN count(*)")):
        try:
            print(f"  {label:<11}", f"{h.query(q)[0]['count(*)']:,}")
        except Exception as e:
            print(f"  {label:<11} count failed: {str(e)[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
