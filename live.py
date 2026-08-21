"""Continuous ingestion — the graph keeps itself current.

The crawler in `ingest.py` is a batch job: it walks a frontier, fills the
graph, and stops. That was fine for building a snapshot, and wrong for the
thing this tool claims to do. A blast radius computed against a graph written
thirteen hours ago is a blast radius for a dependency tree that has since
changed — and the whole premise here is answering "who is exposed *right now*".

So this module runs the five registries' change feeds continuously and writes
what it sees straight into HydraDB. One worker thread per ecosystem, one writer
thread, and a status block that reports what is actually happening rather than
what is supposed to be:

    per ecosystem   last poll, last publish seen, packages written, errors,
                    consecutive errors, whether it is backed off and until when

Three deliberate choices worth stating.

**A publish to a package already in the graph always gets re-fetched.** That is
the security event — a package thousands of things depend on shipping a new
version is the exact moment a supply-chain attack goes live. Packages we have
never seen are ingested too, but rate-limited per poll, because most publishes
are somebody's first version with nobody downstream and letting them in
unbounded would spend the whole budget on noise.

**Failure is per-ecosystem and visible.** A registry that starts refusing
connections backs off exponentially and says so in `status()`; it does not take
the other four down, and it does not quietly stop while the UI keeps showing a
green light. The UI reads the same numbers this module records.

**Nothing is fabricated when a feed is empty.** A poll that returns nothing
records a poll and no events. An ecosystem that has never yielded a publish
reports `last_event_at: None` rather than a plausible-looking timestamp.
"""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
from collections import OrderedDict, deque

import ecosystems
from hydra import Hydra, pkg_id
from ingest import CREATE_EDGES, UPSERT_PACKAGES, UPSERT_STUBS, open_sidecar

# How often each registry is polled. These are not arbitrary: npm's _changes
# moves constantly and is cheap, Maven Central's metadata is expensive and
# barely moves, and crates.io publishes a crawler policy that asks for
# restraint. Polling faster than the data changes only burns someone's budget.
POLL_SECONDS = {
    "npm": 5.0,
    "pypi": 15.0,
    "go": 20.0,
    "crates": 30.0,
    "maven": 60.0,
}

MAX_KNOWN_PER_POLL = 8      # already in the graph — the events that matter
MAX_NEW_PER_POLL = 3        # never seen before — growth, rate-limited

# The graph has a performance cliff and continuous ingestion drives straight at
# it. Measured on this store: ~102k edges answers depth 5 in under a second,
# and at ~246k edges depth 4 and 5 stop returning at all. Live ingestion adds
# roughly 21,000 edges an hour, which reaches that cliff in a single afternoon.
#
# So growth is budgeted. Above the ceiling the crawler keeps refreshing packages
# it already knows — which is the security-relevant work, and adds almost no
# vertices — and stops adding packages nobody depends on yet. Coverage of the
# whole registry was never the goal; answering "who depends on X" quickly is,
# and an unbounded crawl trades that away for rows nobody queries.
EDGE_BUDGET = int(os.environ.get("BLAST_EDGE_BUDGET", "150000"))
RECENT_RING = 4000          # names seen lately, so a slow feed is not re-fetched
COOLDOWN_SECONDS = 900      # do not re-fetch the same package inside this window
BACKOFF_CAP = 300.0
EVENT_RING = 240            # enriched events kept for the UI ticker


class _Recent:
    """A bounded set with insertion order, used as a name cooldown.

    A registry feed returns the last N publishes every time it is polled, so
    without this every poll re-fetches everything it returned last poll.
    """

    def __init__(self, cap: int = RECENT_RING):
        self._d: OrderedDict[str, float] = OrderedDict()
        self._cap = cap

    def seen_recently(self, key: str, window: float = COOLDOWN_SECONDS) -> bool:
        at = self._d.get(key)
        return at is not None and (time.time() - at) < window

    def mark(self, key: str) -> None:
        self._d[key] = time.time()
        self._d.move_to_end(key)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)


class EcosystemState:
    """Everything `status()` reports for one registry, and nothing it doesn't."""

    def __init__(self, name: str):
        self.name = name
        self.polls = 0
        self.events_seen = 0          # publishes the feed reported
        self.packages_written = 0     # ...that we actually fetched and stored
        self.errors = 0
        self.consecutive_errors = 0
        self.last_poll_at: float | None = None
        self.last_event_at: float | None = None
        self.last_write_at: float | None = None
        self.last_error: str = ""
        self.backoff_until: float = 0.0
        self.stopped = False

    @property
    def state(self) -> str:
        if self.stopped:
            return "stopped"
        if time.time() < self.backoff_until:
            return "backoff"
        if self.consecutive_errors:
            return "degraded"
        return "live" if self.polls else "starting"

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "ecosystem": self.name,
            "state": self.state,
            "polls": self.polls,
            "events_seen": self.events_seen,
            "packages_written": self.packages_written,
            "errors": self.errors,
            "consecutive_errors": self.consecutive_errors,
            "last_poll_at": self.last_poll_at,
            "last_event_at": self.last_event_at,
            "last_write_at": self.last_write_at,
            "seconds_since_poll": (round(now - self.last_poll_at, 1)
                                   if self.last_poll_at else None),
            "seconds_since_event": (round(now - self.last_event_at, 1)
                                    if self.last_event_at else None),
            "backoff_seconds": (round(self.backoff_until - now, 1)
                                if now < self.backoff_until else 0),
            "last_error": self.last_error[:200],
            "poll_interval": POLL_SECONDS.get(self.name, 30.0),
        }


class LiveIngest:
    """Runs the change feeds and writes what they report into HydraDB."""

    def __init__(self, hydra: Hydra | None = None, db_path: str = "deps.db",
                 only: tuple[str, ...] | None = None):
        self.hydra = hydra or Hydra()
        self.db_path = db_path
        self.adapters = [a for a in ecosystems.all_adapters()
                         if not only or a.name in only]
        self.state = {a.name: EcosystemState(a.name) for a in self.adapters}
        self.events: deque = deque(maxlen=EVENT_RING)

        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._recent = _Recent()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._subscribers: list = []

        self.started_at: float | None = None
        self.graph_writes = 0          # vertices+edges actually sent to HydraDB
        self.edges_written = 0
        self.last_graph_write_at: float | None = None
        self.write_errors = 0
        self.last_write_error = ""
        self.writable = None           # None until the first write proves it

        # name -> nid for everything the sidecar already knows, so a publish to
        # a package we have crawled is recognised without a query per event.
        self._known: set[str] = set()
        self.edges_total = 0        # prod edges in the sidecar == edges in the graph

    # ------------------------------------------------------------------

    def subscribe(self, fn) -> None:
        """Call `fn(event)` for every package actually written.

        This is how alert routing hears about publishes without this module
        needing to know that alerting exists.
        """
        with self._lock:
            self._subscribers.append(fn)

    def start(self) -> None:
        if self._threads:
            return
        self.started_at = time.time()
        self._load_known()

        writer = threading.Thread(target=self._writer_loop,
                                  name="live-writer", daemon=True)
        writer.start()
        self._threads.append(writer)

        for adapter in self.adapters:
            t = threading.Thread(target=self._poll_loop, args=(adapter,),
                                 name=f"live-{adapter.name}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _load_known(self) -> None:
        try:
            db = sqlite3.connect(self.db_path, check_same_thread=False)
            self._known = {n for (n,) in db.execute(
                "SELECT name FROM packages WHERE crawled = 1")}
            self.edges_total = db.execute(
                "SELECT COUNT(*) FROM deps WHERE kind = 'prod'").fetchone()[0]
            db.close()
        except Exception:
            self._known = set()

    # ------------------------------------------------------------------
    # one thread per registry
    # ------------------------------------------------------------------

    def _poll_loop(self, adapter) -> None:
        st = self.state[adapter.name]
        interval = POLL_SECONDS.get(adapter.name, 30.0)
        backoff = interval

        while not self._stop.is_set():
            if time.time() < st.backoff_until:
                self._stop.wait(1.0)
                continue
            try:
                self._poll_once(adapter, st)
                st.consecutive_errors = 0
                st.last_error = ""
                backoff = interval
            except Exception as exc:
                st.errors += 1
                st.consecutive_errors += 1
                st.last_error = f"{type(exc).__name__}: {exc}"
                # Exponential, capped, and recorded. A registry having a bad
                # afternoon must not take the other four with it, and must not
                # look healthy while it does nothing.
                backoff = min(backoff * 2, BACKOFF_CAP)
                st.backoff_until = time.time() + backoff
            self._stop.wait(interval)

        st.stopped = True

    def _poll_once(self, adapter, st: EcosystemState) -> None:
        feed = adapter.changes_feed()
        st.polls += 1
        st.last_poll_at = time.time()
        if feed is None:
            return

        names = []
        for name in feed:
            if name and name not in names:
                names.append(name)
        if not names:
            return

        st.events_seen += len(names)
        st.last_event_at = time.time()

        known_budget = MAX_KNOWN_PER_POLL
        # Refreshing what we already know never stops; discovering new packages
        # does, once the graph is as large as it can be and still be fast.
        new_budget = 0 if self.growth_paused else MAX_NEW_PER_POLL
        for name in names:
            qual = qualified(adapter.name, name)
            if self._recent.seen_recently(qual):
                continue
            is_known = qual in self._known
            if is_known:
                if known_budget <= 0:
                    continue
                known_budget -= 1
            else:
                if new_budget <= 0:
                    continue
                new_budget -= 1
            self._recent.mark(qual)
            try:
                self._queue.put((adapter, name, is_known), timeout=0.5)
            except queue.Full:
                # The writer is behind. Dropping here is correct: the feed will
                # report this package again, and blocking the poller would make
                # every ecosystem stall behind the slowest one.
                return

    # ------------------------------------------------------------------
    # a single writer thread — sqlite and the graph both prefer one
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        db = open_sidecar(self.db_path)
        while not self._stop.is_set():
            try:
                adapter, name, is_known = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._ingest_one(db, adapter, name, is_known)
            except Exception as exc:
                self.write_errors += 1
                self.last_write_error = f"{type(exc).__name__}: {exc}"
                if "read-only" in str(exc).lower() or "500" in str(exc):
                    # HydraDB 0.1.0 leaves a restarted store permanently
                    # read-only. Saying so beats a green light over a graph
                    # that has silently stopped accepting writes.
                    self.writable = False

    def _ingest_one(self, db, adapter, name: str, is_known: bool) -> None:
        st = self.state[adapter.name]
        doc = adapter.fetch_package(name)
        if not doc:
            return
        pkg = adapter.parse(doc)
        if not pkg or not pkg.name:
            return

        eco = adapter.name
        qual = qualified(eco, pkg.name)
        vid = pkg_id(pkg.name, eco)

        # ---- sidecar first: its primary keys decide which edges are new ----
        db.execute(
            "INSERT INTO packages (nid, name, latest, versions_seen, crawled) "
            "VALUES (?,?,?,?,1) ON CONFLICT(name) DO UPDATE SET "
            "latest=excluded.latest, versions_seen=excluded.versions_seen, "
            "crawled=1",
            (vid, qual, pkg.latest, pkg.versions_seen))

        db.executemany(
            "INSERT OR IGNORE INTO release_deps (name, version, dep, kind, range) "
            "VALUES (?,?,?,?,?)",
            [(qual, d.get("version") or pkg.latest,
              qualified(eco, d["dep"]), d.get("kind", "prod"),
              str(d.get("range") or "")[:120]) for d in pkg.deps])

        db.executemany(
            "INSERT OR IGNORE INTO maintainers (maintainer, package) VALUES (?,?)",
            [(m, qual) for m in pkg.maintainers])

        stub_rows, edge_rows = [], []
        for d in pkg.deps:
            dep_qual = qualified(eco, d["dep"])
            cur = db.execute(
                "INSERT OR IGNORE INTO deps (src, dst, kind, range, via) "
                "VALUES (?,?,?,?,?)",
                (qual, dep_qual, d.get("kind", "prod"),
                 str(d.get("range") or "")[:120], pkg.latest))
            if d.get("kind") != "prod":
                continue
            dep_vid = pkg_id(d["dep"], eco)
            if dep_qual not in self._known:
                db.execute("INSERT OR IGNORE INTO packages (nid, name) VALUES (?,?)",
                           (dep_vid, dep_qual))
                stub_rows.append({"id": dep_vid, "name": d["dep"],
                                  "ecosystem": eco})
            # Only a genuinely new row becomes an edge; the primary key above is
            # what stops HydraDB accumulating duplicates, since CREATE cannot
            # MERGE inside UNWIND.
            if cur.rowcount:
                edge_rows.append({"src": dep_vid, "dst": vid})
        db.commit()

        # ---- then the graph: vertices before the edges that reference them --
        written = 0
        if stub_rows:
            self.hydra.write_batch(UPSERT_STUBS, stub_rows)
            written += len(stub_rows)
        self.hydra.write_batch(UPSERT_PACKAGES, [{
            "id": vid, "name": pkg.name, "latest": pkg.latest,
            "ecosystem": eco}])
        written += 1
        if edge_rows:
            self.hydra.write_batch(CREATE_EDGES, edge_rows)
            written += len(edge_rows)
            self.edges_written += len(edge_rows)
            self.edges_total += len(edge_rows)

        self.writable = True
        self.graph_writes += written
        self.last_graph_write_at = time.time()
        st.packages_written += 1
        st.last_write_at = time.time()

        self._known.add(qual)
        for d in pkg.deps:
            if d.get("kind") == "prod":
                self._known.add(qualified(eco, d["dep"]))

        event = {
            "at": time.time(),
            "ecosystem": eco,
            "name": pkg.name,
            "qualified": qual,
            "version": pkg.latest,
            "id": vid,
            "deps": len([d for d in pkg.deps if d.get("kind") == "prod"]),
            "was_known": is_known,
            "maintainers": list(pkg.maintainers)[:5],
        }
        self.events.appendleft(event)
        self._notify(event)

    def _notify(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception:
                # A broken subscriber must not stop ingestion. It is the
                # subscriber's job to report its own failure.
                pass

    # ------------------------------------------------------------------

    def status(self) -> dict:
        now = time.time()
        per = [self.state[a.name].snapshot() for a in self.adapters]
        live = [p for p in per if p["state"] in ("live", "starting")]
        return {
            "running": bool(self._threads) and not self._stop.is_set(),
            "started_at": self.started_at,
            "uptime_seconds": (round(now - self.started_at, 1)
                               if self.started_at else 0),
            "ecosystems": per,
            "ecosystems_live": len(live),
            "ecosystems_total": len(per),
            "graph_writes": self.graph_writes,
            "edges_written": self.edges_written,
            "last_graph_write_at": self.last_graph_write_at,
            "seconds_since_graph_write": (
                round(now - self.last_graph_write_at, 1)
                if self.last_graph_write_at else None),
            "queue_depth": self._queue.qsize(),
            "edges_total": self.edges_total,
            "edge_budget": EDGE_BUDGET,
            "growth_paused": self.growth_paused,
            "budget_used": (round(self.edges_total / EDGE_BUDGET, 3)
                            if EDGE_BUDGET else None),
            "write_errors": self.write_errors,
            "last_write_error": self.last_write_error[:200],
            "writable": self.writable,
            "events_buffered": len(self.events),
        }

    @property
    def growth_paused(self) -> bool:
        """Past the budget the crawler refreshes but no longer discovers."""
        return EDGE_BUDGET > 0 and self.edges_total >= EDGE_BUDGET

    def recent(self, limit: int = 40) -> list[dict]:
        return list(self.events)[:limit]


def qualified(ecosystem: str, name: str) -> str:
    """The sidecar's key for a package.

    `packages.name` is UNIQUE and predates multi-ecosystem support, so a bare
    name would make PyPI's `requests` and npm's `requests` the same row while
    the graph correctly keeps them apart. npm keeps bare names so the existing
    27k rows stay addressable; everything else is qualified.
    """
    return name if ecosystem == "npm" else f"{ecosystem}:{name}"
