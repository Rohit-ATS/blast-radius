"""Watch npm publish in real time, and say who each publish reaches.

npm's replication endpoint no longer supports `feed=continuous` — it answers
400 for every variant of it. What does work is polling `_changes?since=<seq>`
from the current `update_seq`, which is a few seconds behind live rather than
truly streaming. That is the honest description of what this is.

The interesting event is not "a package was published". Around 4,300,000
packages exist and most publishes are somebody's first version with nobody
downstream. The interesting event is when a package that *thousands of things
already depend on* ships a new version — that is the exact moment a
supply-chain attack becomes live, and it is the one the ticker highlights.

Every entry is enriched from the graph (how many packages depend on it) and
from OSV (is this new version already flagged). Nothing is fabricated: a
package we have never crawled says so.
"""

import threading
import time
from collections import deque

import requests

import intel
from hydra import pkg_id

REPLICATE = "https://replicate.npmjs.com"
UA = {"User-Agent": "blast-radius-hackhydra/0.1 (+supply-chain monitor)"}

POLL_SECONDS = 4.0
MAX_PER_POLL = 12          # bound the registry lookups a single poll can cause
RING = 60                  # events kept for the UI


class Feed:
    """A background poller with a ring buffer of enriched publish events."""

    def __init__(self, hydra=None, blast_mod=None, depth: int = 4):
        self.hydra = hydra
        self.blast = blast_mod
        self.depth = depth
        self.events: deque = deque(maxlen=RING)
        self.seq = None
        self.started_at = None
        self.polls = 0
        self.seen = 0
        self.errors = 0
        self.last_error = None
        self.running = False
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update(UA)
        self.npm_total = None

    # ----------------------------------------------------------------

    def start(self):
        if self.running:
            return self
        self.running = True
        self.started_at = time.time()
        threading.Thread(target=self._loop, daemon=True, name="npm-feed").start()
        return self

    def stop(self):
        self.running = False

    def snapshot(self, limit: int = 25):
        with self._lock:
            events = list(self.events)[:limit]
        return {
            "running": self.running,
            "since_seq": self.seq,
            "polls": self.polls,
            "publishes_seen": self.seen,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "errors": self.errors,
            "last_error": self.last_error,
            "npm_total": self.npm_total,
            "events": events,
            "source": "replicate.npmjs.com/_changes (polled; npm rejects "
                      "feed=continuous)",
        }

    # ----------------------------------------------------------------

    def _loop(self):
        while self.running:
            try:
                if self.seq is None:
                    self._anchor()
                self._poll_once()
                self.errors = 0
            except Exception as e:
                self.errors += 1
                self.last_error = f"{e.__class__.__name__}: {e}"[:180]
                # Back off rather than hammer a failing endpoint, but never
                # give up — a demo may run for an hour.
                time.sleep(min(60.0, POLL_SECONDS * (2 ** min(self.errors, 4))))
                continue
            time.sleep(POLL_SECONDS)

    def _anchor(self):
        """Start from now, not from the beginning of npm."""
        r = self._session.get(f"{REPLICATE}/", timeout=25)
        root = r.json()
        self.seq = root.get("update_seq")
        self.npm_total = root.get("doc_count")

    def _poll_once(self):
        r = self._session.get(f"{REPLICATE}/_changes", timeout=30,
                              params={"since": self.seq, "limit": 60})
        if r.status_code != 200:
            raise RuntimeError(f"changes http {r.status_code}")
        data = r.json()
        self.polls += 1
        rows = data.get("results") or []
        if data.get("last_seq"):
            self.seq = data["last_seq"]
        if not rows:
            return

        names = []
        for ev in rows:
            name = ev.get("id")
            if not name or ev.get("deleted") or name.startswith("_"):
                continue
            names.append(name)
        self.seen += len(names)

        for name in names[:MAX_PER_POLL]:
            try:
                event = self._enrich(name)
            except Exception:
                continue
            if event:
                with self._lock:
                    self.events.appendleft(event)

    # ----------------------------------------------------------------

    def _enrich(self, name: str):
        """Turn a bare package name into something worth putting on screen."""
        summary = intel.npm_summary(name)
        if not summary:
            return None
        version = summary["latest"]

        dependents = None
        in_graph = False
        if self.hydra is not None and self.blast is not None:
            try:
                known, _ = self.blast.resolve_package(self.hydra, name)
                in_graph = bool(known)
                if known:
                    rows = self.hydra.query(
                        self.blast.REACH_COUNT % self.depth,
                        {"id": pkg_id(name)})
                    dependents = rows[0]["count(*)"] if rows else 0
            except Exception:
                dependents = None

        advisories = []
        try:
            osv = intel.osv_query(name, version)
            advisories = osv.get("vulns", [])
        except Exception:
            pass
        malware = [a for a in advisories if a.get("kind") == "malware"]

        # Severity for the ticker: an established package shipping a new
        # version is the supply-chain moment; a brand new package is noise.
        if malware:
            level = "malicious"
        elif dependents and dependents >= 500:
            level = "high"
        elif dependents and dependents > 0:
            level = "notable"
        else:
            level = "routine"

        return {
            "name": name,
            "version": version,
            "published": summary.get("published", ""),
            "at": time.time(),
            "in_graph": in_graph,
            "dependents": dependents,
            "level": level,
            "advisories": [{"id": a["id"], "kind": a["kind"],
                            "summary": a["summary"][:120]} for a in advisories[:3]],
            "maintainers": summary.get("maintainers", [])[:3],
            "headline": self._headline(name, version, dependents, in_graph, malware),
        }

    @staticmethod
    def _headline(name, version, dependents, in_graph, malware):
        if malware:
            return (f"{name}@{version} was just published and is already "
                    f"flagged as malicious.")
        if dependents:
            return (f"{name}@{version} just published — {dependents:,} packages "
                    f"in the graph depend on it.")
        if in_graph:
            return f"{name}@{version} just published — nothing depends on it yet."
        return f"{name}@{version} just published — not in the crawled graph."
