"""Crawl the npm registry into HydraDB as a two-layer dependency graph.

Graph model
-----------
  (:Package  {name, latest, versions_seen})
  (:Release  {id:"name@version", name, version, published_at, deprecated})
  (:Maintainer {name})

  (Release)-[:OF]->(Package)
  (Release)-[:DEPENDS_ON {range, kind}]->(Package)     # precise layer
  (Package)-[:REQUIRES  {range, kind, via}]->(Package) # collapsed hot path
  (Package)-[:MAINTAINED_BY]->(Maintainer)

Why two layers: REQUIRES collapses versions so blast-radius traversal is a
single variable-length hop and stays fast at depth 5+. DEPENDS_ON keeps
version precision so "which release introduced it" and "would this range have
resolved to the bad version" stay answerable. Traversal speed and forensic
precision have different shapes; storing both is the point.

Run:
  python ingest.py --seeds seeds.txt --max-packages 25000 --max-versions 5
Resumable: state is checkpointed to .crawl_state.json every batch.
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests

from hydra import Hydra

REGISTRY = "https://registry.npmjs.org"
SLIM = {"Accept": "application/vnd.npm.install-v1+json"}
STATE_PATH = ".crawl_state.json"

_stop = threading.Event()


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

SCHEMA = [
    "CREATE INDEX ON :Package(name)",
    "CREATE INDEX ON :Release(id)",
    "CREATE INDEX ON :Maintainer(name)",
]

UPSERT_PACKAGES = """
UNWIND $rows AS row
MERGE (p:Package {name: row.name})
SET p.latest = row.latest, p.versions_seen = row.versions_seen
"""

UPSERT_RELEASES = """
UNWIND $rows AS row
MERGE (r:Release {id: row.id})
SET r.name = row.name, r.version = row.version,
    r.published_at = row.published_at, r.deprecated = row.deprecated
MERGE (p:Package {name: row.name})
MERGE (r)-[:OF]->(p)
"""

UPSERT_DEPENDS = """
UNWIND $rows AS row
MERGE (r:Release {id: row.release})
MERGE (p:Package {name: row.dep})
MERGE (r)-[d:DEPENDS_ON {kind: row.kind}]->(p)
SET d.range = row.range
"""

UPSERT_REQUIRES = """
UNWIND $rows AS row
MERGE (a:Package {name: row.src})
MERGE (b:Package {name: row.dep})
MERGE (a)-[q:REQUIRES {kind: row.kind}]->(b)
SET q.range = row.range, q.via = row.via
"""

UPSERT_MAINTAINERS = """
UNWIND $rows AS row
MERGE (p:Package {name: row.name})
MERGE (m:Maintainer {name: row.maintainer})
MERGE (p)-[:MAINTAINED_BY]->(m)
"""


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
    for stmt in SCHEMA:
        try:
            h.query(stmt)
        except Exception as e:
            print(f"[schema] skipped ({e.__class__.__name__}) — continuing", file=sys.stderr)

    visited, queue = load_state(args.seeds)
    session = requests.Session()
    session.headers.update({"User-Agent": "blast-radius-hackhydra/0.1"})

    pkg_rows, rel_rows, dep_rows, req_rows, mnt_rows = [], [], [], [], []
    started = time.time()
    done = 0

    def flush(force: bool = False):
        nonlocal pkg_rows, rel_rows, dep_rows, req_rows, mnt_rows
        if not force and len(pkg_rows) < args.batch:
            return
        if pkg_rows:
            h.write_batch(UPSERT_PACKAGES, pkg_rows, chunk=args.chunk)
        if rel_rows:
            h.write_batch(UPSERT_RELEASES, rel_rows, chunk=args.chunk)
        if dep_rows:
            h.write_batch(UPSERT_DEPENDS, dep_rows, chunk=args.chunk)
        if req_rows:
            h.write_batch(UPSERT_REQUIRES, req_rows, chunk=args.chunk)
        if mnt_rows:
            h.write_batch(UPSERT_MAINTAINERS, mnt_rows, chunk=args.chunk)
        pkg_rows, rel_rows, dep_rows, req_rows, mnt_rows = [], [], [], [], []
        save_state(visited, queue)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        while queue and done < args.max_packages and not _stop.is_set():
            wave = [queue.popleft() for _ in range(min(args.concurrency * 4, len(queue)))]
            wave = [n for n in wave if n not in visited]
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
                  f"{rate:5.1f} pkg/s | edges buffered {len(dep_rows)}", flush=True)

    flush(force=True)
    print(f"[crawl] finished: {done} packages in {time.time() - started:.0f}s")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state(seeds_path: str) -> tuple[set, deque]:
    if os.path.exists(STATE_PATH):
        s = json.load(open(STATE_PATH))
        print(f"[state] resuming: {len(s['visited'])} visited, {len(s['queue'])} queued")
        return set(s["visited"]), deque(s["queue"])
    seeds = [l.strip() for l in open(seeds_path) if l.strip() and not l.startswith("#")]
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
