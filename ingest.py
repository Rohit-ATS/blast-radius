"""Crawl the npm registry into a two-layer store: HydraDB + a SQLite sidecar.

Layer 1 — HydraDB, topology only
--------------------------------
  (:Package {id: nid(name), name, latest})
  (dependency)-[:REQUIRED_BY]->(dependent)

The edge points from the dependency to the thing that depends on it, which is
backwards from how you would draw it. That is deliberate. HydraDB 0.1.0 only
executes a variable-length MATCH when the *source* id is fixed, and in an
incident the one thing you know is the compromised package. Reversing the edge
at write time makes the compromised package the source, so "who is transitively
exposed" is one traversal from a known id instead of a scan.

Only `dependencies` become edges. peerDependencies are recorded in the sidecar
but are not installed transitively by npm, so counting them as blast radius
would overstate exposure.

Layer 2 — deps.db (SQLite), everything you have to filter on
------------------------------------------------------------
HydraDB 0.1.0 cannot filter on edge properties during traversal, so declared
semver ranges cannot live on the edges and still be useful. They go in SQLite,
which answers "whose range would actually have admitted 4.4.2" with an indexed
scan. Topology in the graph, predicates in the sidecar — each store doing the
shape of work it is good at.

Run:
  py ingest.py --seeds seeds.txt --max-packages 40000 --max-versions 5
Resumable: state is checkpointed to .crawl_state.json every batch.
"""

import argparse
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests

from hydra import Hydra, nid

REGISTRY = "https://registry.npmjs.org"
SLIM = {"Accept": "application/vnd.npm.install-v1+json"}
STATE_PATH = ".crawl_state.json"
DEPS_DB = "deps.db"

_stop = threading.Event()


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

# There is no CREATE INDEX in HydraDB 0.1.0, and none is needed: vertices are
# addressed by integer id, and nid() derives that id from the name locally.

UPSERT_PACKAGES = """
UNWIND $rows AS row
MERGE (p {id: row.id})
SET p:Package, p.name = row.name, p.latest = row.latest
"""

# A dependency we have seen named but not yet crawled. Sets name only, so it
# does not clobber `latest` if the package was already crawled in full.
UPSERT_STUBS = """
UNWIND $rows AS row
MERGE (p {id: row.id})
SET p:Package, p.name = row.name
"""

# Reversed on purpose: src is the dependency, dst is the dependent.
# CREATE (not MERGE) because HydraDB rejects MATCH..MERGE inside UNWIND;
# CREATE with an explicit id binds to the existing vertex rather than
# duplicating it, and the sidecar's primary key stops us writing an edge twice.
CREATE_EDGES = """
UNWIND $rows AS row
CREATE (a {id: row.src})-[:REQUIRED_BY]->(b {id: row.dst})
"""

SIDECAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    nid           INTEGER PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    latest        TEXT,
    versions_seen INTEGER,
    crawled       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deps (
    src   TEXT NOT NULL,          -- the dependent
    dst   TEXT NOT NULL,          -- the dependency
    kind  TEXT NOT NULL,
    range TEXT,
    via   TEXT,                   -- version of src this range was read from
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS deps_dst ON deps(dst);
CREATE TABLE IF NOT EXISTS release_deps (
    name    TEXT NOT NULL,        -- the dependent package
    version TEXT NOT NULL,        -- ...at this version
    dep     TEXT NOT NULL,        -- the dependency
    kind    TEXT NOT NULL,
    range   TEXT,
    PRIMARY KEY (name, version, dep, kind)
);
CREATE INDEX IF NOT EXISTS release_deps_dep ON release_deps(dep);
CREATE TABLE IF NOT EXISTS maintainers (
    maintainer TEXT NOT NULL,
    package    TEXT NOT NULL,
    PRIMARY KEY (maintainer, package)
);
CREATE INDEX IF NOT EXISTS maintainers_pkg ON maintainers(package);
-- nid() collisions would silently merge two packages into one vertex. Over
-- 100k names in a 51-bit space this should never fire; if it ever does, the
-- evidence is here rather than in a wrong answer.
CREATE TABLE IF NOT EXISTS collisions (
    nid INTEGER, name_a TEXT, name_b TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_sidecar(path: str = DEPS_DB) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.executescript(SIDECAR_SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.commit()
    return db


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch_package(session: requests.Session, name: str, slim: bool = False) -> dict | None:
    """Full doc carries maintainers + publish times (~200KB for a big package).
    Slim doc is ~2.6x smaller but drops both. Default to full; the maintainer
    pivot and the time-window query are two of the four headline features."""
    url = f"{REGISTRY}/{requests.utils.quote(name, safe='@')}"
    try:
        r = session.get(url, headers=SLIM if slim else {}, timeout=30)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def parse_package(doc: dict, max_versions: int) -> dict:
    """Reduce a registry document to just the rows we write."""
    name = doc.get("name")
    versions = doc.get("versions", {}) or {}
    times = doc.get("time", {}) or {}
    latest = (doc.get("dist-tags", {}) or {}).get("latest")

    ordered = sorted(versions.keys(), key=_version_sort_key)
    keep = ordered[-max_versions:] if max_versions > 0 else ordered
    if latest and latest in versions and latest not in keep:
        keep.append(latest)

    releases, depends, requires, deps_out = [], [], [], set()
    for v in keep:
        meta = versions[v] or {}
        rid = f"{name}@{v}"
        releases.append({
            "id": rid, "name": name, "version": v,
            "published_at": times.get(v, ""),
            "deprecated": bool(meta.get("deprecated")),
        })
        for kind, field in (("prod", "dependencies"), ("peer", "peerDependencies")):
            for dep, rng in (meta.get(field) or {}).items():
                depends.append({"release": rid, "dep": dep, "range": str(rng)[:120],
                                "kind": kind})
                deps_out.add(dep)
                if v == latest or v == keep[-1]:
                    requires.append({"src": name, "dep": dep, "range": str(rng)[:120],
                                     "kind": kind, "via": v})

    maintainers = [{"name": name, "maintainer": m.get("name")}
                   for m in (doc.get("maintainers") or []) if m.get("name")]

    return {
        "package": {"name": name, "latest": latest or "",
                    "versions_seen": len(versions)},
        "releases": releases,
        "depends": depends,
        "requires": _dedupe(requires),
        "maintainers": maintainers,
        "frontier": deps_out,
    }


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        k = (r["src"], r["dep"], r["kind"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


_NUM = re.compile(r"\d+")


def _version_sort_key(v: str):
    parts = _NUM.findall(v)
    return ([int(p) for p in parts[:4]] + [0, 0, 0, 0])[:4], "-" not in v


# --------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------

def crawl(args) -> None:
    h = Hydra()
    h.wait_ready()
    db = open_sidecar(args.db)

    # name -> id map, so a collision surfaces as evidence instead of a merge.
    known: dict[int, str] = {i: n for i, n in db.execute("SELECT nid, name FROM packages")}
    # Vertices already MERGEd this process; avoids re-sending stubs every wave.
    written: set[int] = set(known)

    # A previous run's finished_at would make this crawl look already-done to
    # the API, so the flag is set explicitly for the lifetime of the process.
    db.execute("DELETE FROM meta WHERE key = 'finished_at'")
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('running', '1')")
    db.commit()

    visited, queue = load_state(args.seeds)
    session = requests.Session()
    session.headers.update({"User-Agent": "blast-radius-hackhydra/0.1"})

    pkg_rows, rel_rows, dep_rows, req_rows, mnt_rows = [], [], [], [], []
    started = time.time()
    done = 0
    edges_written = 0

    def vid(name: str) -> int:
        i = nid(name)
        prev = known.get(i)
        if prev is None:
            known[i] = name
        elif prev != name:
            db.execute("INSERT INTO collisions (nid, name_a, name_b) VALUES (?,?,?)",
                       (i, prev, name))
            print(f"[collision] nid {i}: {prev!r} vs {name!r}", file=sys.stderr)
        return i

    def flush(force: bool = False):
        nonlocal pkg_rows, rel_rows, dep_rows, req_rows, mnt_rows, edges_written
        if not force and len(pkg_rows) < args.batch:
            return
        if not (pkg_rows or dep_rows or req_rows or mnt_rows):
            return

        # ---- sidecar first: its primary keys decide which edges are new ----
        for row in pkg_rows:
            db.execute(
                "INSERT INTO packages (nid, name, latest, versions_seen, crawled) "
                "VALUES (?,?,?,?,1) ON CONFLICT(name) DO UPDATE SET "
                "latest=excluded.latest, versions_seen=excluded.versions_seen, crawled=1",
                (vid(row["name"]), row["name"], row["latest"], row["versions_seen"]))
        db.executemany(
            "INSERT OR IGNORE INTO release_deps (name, version, dep, kind, range) "
            "VALUES (?,?,?,?,?)",
            [(r["release"].rsplit("@", 1)[0], r["release"].rsplit("@", 1)[1],
              r["dep"], r["kind"], r["range"]) for r in dep_rows])
        db.executemany(
            "INSERT OR IGNORE INTO maintainers (maintainer, package) VALUES (?,?)",
            [(m["maintainer"], m["name"]) for m in mnt_rows])

        # A dependency named but not yet crawled is still a real package and a
        # real vertex — it just has no `latest` until its turn comes.
        stub_rows = []
        for r in req_rows:
            for name in (r["src"], r["dep"]):
                i = vid(name)
                if i not in written:
                    written.add(i)
                    stub_rows.append({"id": i, "name": name})
                    db.execute("INSERT OR IGNORE INTO packages (nid, name) VALUES (?,?)",
                               (i, name))

        edge_rows = []
        for r in req_rows:
            cur = db.execute(
                "INSERT OR IGNORE INTO deps (src, dst, kind, range, via) VALUES (?,?,?,?,?)",
                (r["src"], r["dep"], r["kind"], r["range"], r["via"]))
            # Only `dependencies` are installed transitively; peers are not
            # blast radius. Written once, because the PK above filters repeats.
            if cur.rowcount and r["kind"] == "prod":
                edge_rows.append({"src": vid(r["dep"]), "dst": vid(r["src"])})
        db.commit()

        # ---- then the graph: vertices before the edges that reference them --
        if stub_rows:
            h.write_batch(UPSERT_STUBS, stub_rows, chunk=args.chunk)
        if pkg_rows:
            h.write_batch(UPSERT_PACKAGES,
                          [{"id": vid(r["name"]), "name": r["name"], "latest": r["latest"]}
                           for r in pkg_rows], chunk=args.chunk)
        if edge_rows:
            h.write_batch(CREATE_EDGES, edge_rows, chunk=args.chunk)
            edges_written += len(edge_rows)

        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                   ("crawled", str(done)))
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                   ("queued", str(len(queue))))
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                   ("edges", str(edges_written)))
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                   ("updated_at", str(time.time())))
        db.commit()

        pkg_rows, rel_rows, dep_rows, req_rows, mnt_rows = [], [], [], [], []
        save_state(visited, queue)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        while queue and done < args.max_packages and not _stop.is_set():
            wave = [queue.popleft() for _ in range(min(args.concurrency * 4, len(queue)))]
            # A package can sit in the queue more than once (many packages name
            # the same dependency), so dedupe within the wave too — filtering
            # only against `visited` would fetch and count it twice.
            fresh, seen_in_wave = [], set()
            for n in wave:
                if n in visited or n in seen_in_wave:
                    continue
                seen_in_wave.add(n)
                fresh.append(n)
            wave = fresh
            for n in wave:
                visited.add(n)
            docs = pool.map(lambda n: fetch_package(session, n, args.slim), wave)

            for doc in docs:
                if not doc or not doc.get("name"):
                    continue
                parsed = parse_package(doc, args.max_versions)
                pkg_rows.append(parsed["package"])
                rel_rows.extend(parsed["releases"])
                dep_rows.extend(parsed["depends"])
                req_rows.extend(parsed["requires"])
                mnt_rows.extend(parsed["maintainers"])
                for dep in parsed["frontier"]:
                    if dep not in visited:
                        queue.append(dep)
                done += 1

            flush()
            rate = done / max(time.time() - started, 1e-6)
            print(f"[crawl] {done:>6} packages | queue {len(queue):>7} | "
                  f"{rate:5.1f} pkg/s | {edges_written} edges", flush=True)

    flush(force=True)
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
               ("finished_at", str(time.time())))
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('running', '0')")
    db.commit()
    print(f"[crawl] finished: {done} packages, {edges_written} edges "
          f"in {time.time() - started:.0f}s")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state(seeds_path: str) -> tuple[set, deque]:
    """Resume if there is state, and always merge in any seed we have not seen.

    The merge is what lets the frontier be widened mid-crawl: BFS from a small
    seed set drains after a few thousand packages (popular packages mostly
    depend on each other), so expand_seeds.py writes a bigger seeds file and
    re-running the crawler picks the new names up without losing progress.
    """
    seeds = []
    if os.path.exists(seeds_path):
        seeds = [l.strip() for l in open(seeds_path, encoding="utf-8")
                 if l.strip() and not l.startswith("#")]

    if os.path.exists(STATE_PATH):
        s = json.load(open(STATE_PATH))
        visited, queue = set(s["visited"]), deque(s["queue"])
        pending = set(queue)
        added = 0
        for n in seeds:
            if n not in visited and n not in pending:
                queue.append(n)
                pending.add(n)
                added += 1
        print(f"[state] resuming: {len(visited)} visited, {len(queue)} queued "
              f"(+{added} new seeds)")
        return visited, queue

    print(f"[state] fresh crawl from {len(seeds)} seeds")
    return set(), deque(seeds)


def save_state(visited: set, queue: deque) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"visited": sorted(visited), "queue": list(queue)}, f)
    os.replace(tmp, STATE_PATH)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seeds", default="seeds.txt")
    p.add_argument("--db", default=DEPS_DB, help="SQLite sidecar path")
    p.add_argument("--max-packages", type=int, default=25000)
    p.add_argument("--max-versions", type=int, default=5,
                   help="versions kept per package; 0 = all. Guard against edge blowup.")
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--batch", type=int, default=200)
    p.add_argument("--chunk", type=int, default=500)
    p.add_argument("--slim", action="store_true",
                   help="smaller docs, but no maintainers or publish times")
    args = p.parse_args()

    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    crawl(args)


if __name__ == "__main__":
    main()
