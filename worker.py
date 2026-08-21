"""The background worker: keep the graph current, and keep the watch running.

Render puts web services to sleep on inactivity and never sleeps background
workers. That is the whole reason this process exists separately from the API:
a supply-chain alert matters most when nobody is looking at the site, so the
crawler and the monitor sweeps cannot live inside a process that stops when the
last visitor closes their tab.

Three loops, each on its own thread, each independently restartable:

  seed      breadth-first crawl of the npm registry, resumable from
            .crawl_state.json, so a redeploy continues rather than restarts

  changes   tail of replicate.npmjs.com/_changes. npm answers 400 to every
            variant of `feed=continuous` — see feed.py — so this polls from a
            persisted `since` sequence, which is a few seconds behind live
            rather than truly streaming. That is the honest description.
            Deltas, not a firehose: this is what keeps the graph current
            instead of a stale snapshot.

  monitors  the 24/7 watch, re-measuring every package an account cares about

A fourth input feeds the first: the **priority queue**. Packages that real
people actually looked up are crawled before whatever breadth-first happened to
reach next. At 0.85% coverage, demand-driven beats alphabetical every time.

Run:  python worker.py
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import threading
import time

import config          # noqa: F401 — loads .env before anything reads it

import accounts
import apimeta
import blast
import notify
from hydra import Hydra
from ingest import DEPS_DB

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DEPS_DB", os.path.join(HERE, DEPS_DB))

# Where the changes tail keeps its place. On Render this lives on the mounted
# disk, so a redeploy resumes from the last sequence instead of replaying or,
# worse, skipping to now and losing everything published in between.
STATE_DIR = os.environ.get("WORKER_STATE_DIR", HERE)
CHANGES_STATE = os.path.join(STATE_DIR, ".changes_state.json")

SEED_ENABLED = config.flag("WORKER_CRAWL", True)
CHANGES_ENABLED = config.flag("WORKER_CHANGES", True)
MONITORS_ENABLED = config.flag("WORKER_MONITORS", True)

CHANGES_POLL_S = config.number("CHANGES_POLL_SECONDS", 6)
SEED_BATCH = int(config.number("WORKER_SEED_BATCH", 300))
SEED_PAUSE_S = config.number("WORKER_SEED_PAUSE", 2)

# Politeness. A public tool crawling a public registry identifies itself and
# backs off when told to; anything else gets the whole project blocked.
UA = ("blast-radius/1.0 (+https://github.com/Rohit-ATS/blast-radius; "
      "npm supply-chain monitor)")

_stop = threading.Event()
hydra = Hydra(timeout=180.0, budget=None)      # patient: not on a request path


def log(event: str, **fields) -> None:
    apimeta.log(event, component="worker", **fields)


# --------------------------------------------------------------------------
# shared state, readable by the API through the sidecar
# --------------------------------------------------------------------------

PROGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_progress (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at REAL
);

-- Packages someone actually asked about. The crawler drains this before it
-- goes back to breadth-first, because a name a human typed is worth more
-- coverage than the next name in an alphabetical queue.
CREATE TABLE IF NOT EXISTS crawl_priority (
  name       TEXT PRIMARY KEY,
  asked_at   REAL NOT NULL,
  hits       INTEGER NOT NULL DEFAULT 1,
  crawled_at REAL
);
CREATE INDEX IF NOT EXISTS priority_pending
  ON crawl_priority(crawled_at, hits DESC);
"""


def sidecar(read_only: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not read_only:
        conn.executescript(PROGRESS_SCHEMA)
        conn.commit()
    return conn


def set_progress(key: str, value) -> None:
    try:
        with sidecar() as conn:
            conn.execute(
                "INSERT INTO worker_progress (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), time.time()))
            conn.commit()
    except Exception as exc:
        log("progress_write_failed", key=key, error=str(exc)[:120])


def get_progress() -> dict:
    """Read by /api/health so the UI can show whether the crawl is alive."""
    try:
        with sidecar(read_only=True) as conn:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM worker_progress").fetchall()
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            out[r["key"]] = {"value": json.loads(r["value"]), "at": r["updated_at"]}
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------
# priority queue
# --------------------------------------------------------------------------

def request_crawl(name: str) -> None:
    """Called by the API when someone asks about a package we do not have."""
    name = (name or "").strip()
    if not name:
        return
    try:
        with sidecar() as conn:
            conn.execute(
                "INSERT INTO crawl_priority (name, asked_at, hits) VALUES (?,?,1) "
                "ON CONFLICT(name) DO UPDATE SET hits = hits + 1, asked_at = ?",
                (name, time.time(), time.time()))
            conn.commit()
    except Exception as exc:
        log("priority_write_failed", package=name, error=str(exc)[:120])


def take_priority(limit: int = 40) -> list[str]:
    try:
        with sidecar() as conn:
            rows = conn.execute(
                "SELECT name FROM crawl_priority WHERE crawled_at IS NULL "
                "ORDER BY hits DESC, asked_at ASC LIMIT ?", (limit,)).fetchall()
        return [r["name"] for r in rows]
    except Exception:
        return []


def mark_crawled(names: list[str]) -> None:
    if not names:
        return
    try:
        with sidecar() as conn:
            conn.executemany(
                "UPDATE crawl_priority SET crawled_at = ? WHERE name = ?",
                [(time.time(), n) for n in names])
            conn.commit()
    except Exception:
        pass


# --------------------------------------------------------------------------
# loop 1 — the seed crawl
# --------------------------------------------------------------------------

def seed_loop() -> None:
    """Breadth-first crawl, with the priority queue jumping the line.

    ingest.crawl() owns the actual fetching, dedup and checkpointing; this only
    decides what it should be pointed at next and keeps it running across
    restarts. Rewriting it here would mean two crawlers with two different
    ideas about politeness.
    """
    import argparse
    from ingest import crawl

    seeds = os.environ.get("WORKER_SEEDS", os.path.join(HERE, "seeds_expanded.txt"))
    if not os.path.exists(seeds):
        seeds = os.path.join(HERE, "seeds.txt")

    while not _stop.is_set():
        wanted = take_priority()
        try:
            if wanted:
                # A name someone typed. Write it to a scratch seed file so the
                # existing crawler treats it as a starting point.
                scratch = os.path.join(STATE_DIR, ".priority_seeds.txt")
                with open(scratch, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(wanted))
                log("crawl_priority_batch", count=len(wanted))
                args = argparse.Namespace(
                    seeds=scratch, db=DB_PATH, ecosystem="npm",
                    max_packages=len(wanted) * 8, max_versions=5,
                    concurrency=8, batch=100, chunk=200, slim=False)
                crawl(args)
                mark_crawled(wanted)
                set_progress("priority_last", {"names": wanted[:10],
                                               "count": len(wanted)})
            else:
                args = argparse.Namespace(
                    seeds=seeds, db=DB_PATH, ecosystem="npm",
                    max_packages=SEED_BATCH, max_versions=5,
                    concurrency=8, batch=100, chunk=200, slim=False)
                crawl(args)
                set_progress("seed_last_batch", {"size": SEED_BATCH})
        except Exception as exc:
            log("crawl_batch_failed", error=f"{exc.__class__.__name__}: {exc}"[:180])
            _stop.wait(30)
            continue

        set_progress("seed_last_run", time.time())
        _stop.wait(SEED_PAUSE_S)


# --------------------------------------------------------------------------
# loop 2 — the changes tail
# --------------------------------------------------------------------------

def _load_since() -> int | None:
    try:
        with open(CHANGES_STATE, encoding="utf-8") as fh:
            return json.load(fh).get("since")
    except Exception:
        return None


def _save_since(seq: int) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CHANGES_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"since": seq, "at": time.time()}, fh)
        os.replace(tmp, CHANGES_STATE)          # atomic: never a half-written cursor
    except Exception as exc:
        log("changes_state_write_failed", error=str(exc)[:120])


def changes_loop() -> None:
    """Tail npm's change feed and fold each publish into the graph.

    This is the difference between a graph that is current and one that is a
    snapshot from whenever the last full crawl happened to stop. It is cheap
    because it is deltas: a few dozen publishes a minute, not four million
    packages.
    """
    import requests
    import feed as feedmod

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    since = _load_since()
    if since is None:
        try:
            top = session.get(f"{feedmod.REPLICATE}/", timeout=20).json()
            since = top.get("update_seq")
            log("changes_anchored", since=since)
        except Exception as exc:
            log("changes_anchor_failed", error=str(exc)[:140])
            _stop.wait(30)
            return changes_loop() if not _stop.is_set() else None

    backoff = CHANGES_POLL_S
    seen = 0

    while not _stop.is_set():
        try:
            res = session.get(f"{feedmod.REPLICATE}/_changes",
                              params={"since": since, "limit": 100}, timeout=30)
            if res.status_code == 429:
                # Told to slow down. Doing so is the difference between being a
                # polite client and being blocked.
                retry = int(res.headers.get("Retry-After") or backoff * 2)
                log("changes_rate_limited", retry_after=retry)
                _stop.wait(min(retry, 300))
                backoff = min(backoff * 2, 120)
                continue
            res.raise_for_status()
            backoff = CHANGES_POLL_S

            body = res.json()
            results = body.get("results") or []
            for row in results:
                name = row.get("id")
                if not name or name.startswith("_design/"):
                    continue
                _ingest_one(name)
                seen += 1

            new_since = body.get("last_seq") or since
            if new_since != since:
                since = new_since
                _save_since(since)

            set_progress("changes", {"since": since, "seen": seen,
                                     "last_delta_at": time.time()})
        except Exception as exc:
            log("changes_poll_failed", error=f"{exc.__class__.__name__}: {exc}"[:160])
            _stop.wait(backoff)
            backoff = min(backoff * 2, 120)
            continue

        _stop.wait(CHANGES_POLL_S)


def _ingest_one(name: str) -> None:
    """One published package: upsert it, and flag it if OSV already knows."""
    import argparse
    from ingest import crawl
    try:
        scratch = os.path.join(STATE_DIR, ".changes_seed.txt")
        with open(scratch, "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
        crawl(argparse.Namespace(
            seeds=scratch, db=DB_PATH, ecosystem="npm",
            max_packages=1, max_versions=3,
            concurrency=2, batch=1, chunk=1, slim=False))
    except Exception as exc:
        log("changes_ingest_failed", package=name, error=str(exc)[:120])
        return

    # Is this publish already known-bad? Cheap to ask, and it is the whole
    # point of watching the feed rather than crawling on a timer.
    try:
        import intel
        verdict = intel.check(name) if hasattr(intel, "check") else None
        if verdict and verdict.get("malicious"):
            log("malicious_publish", package=name)
            set_progress("last_malicious", {"package": name, "at": time.time()})
    except Exception:
        pass


# --------------------------------------------------------------------------
# loop 3 — the watch
# --------------------------------------------------------------------------

def measure(package: str) -> dict:
    """Exactly what the API measures, so an alert can never disagree with the
    endpoint a user would check it against."""
    result, ms = blast.blast_radius(hydra, package, 5, 5000)
    return {**result, "latency_ms": round(ms, 1)}


def monitor_loop() -> None:
    accounts.start_worker(measure, log=log)
    while not _stop.is_set():
        set_progress("monitors", accounts.stats())
        _stop.wait(60)


# --------------------------------------------------------------------------

def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())

    notify.start(log=log)
    log("worker_start", crawl=SEED_ENABLED, changes=CHANGES_ENABLED,
        monitors=MONITORS_ENABLED, db=DB_PATH, state_dir=STATE_DIR)

    threads = []
    if MONITORS_ENABLED:
        threads.append(threading.Thread(target=monitor_loop, name="monitors", daemon=True))
    if CHANGES_ENABLED:
        threads.append(threading.Thread(target=changes_loop, name="changes", daemon=True))
    if SEED_ENABLED:
        threads.append(threading.Thread(target=seed_loop, name="seed", daemon=True))

    for t in threads:
        t.start()

    # A worker with nothing enabled should exit loudly, not idle forever
    # pretending to work.
    if not threads:
        log("worker_idle", reason="every loop disabled by configuration")
        return 1

    while not _stop.is_set():
        _stop.wait(5)
        for t in threads:
            if not t.is_alive():
                log("worker_thread_died", thread=t.name)
                return 1

    log("worker_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
