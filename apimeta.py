"""Cross-cutting API concerns: envelope metadata, caching, rate limiting, logs.

Every JSON response leaves with the same metadata attached — whether it
succeeded, which store answered it, how much of npm the graph actually covers,
whether it came from cache, and a request id that ties it to a log line.

One deliberate compromise: the metadata is added at the top level of each
payload rather than nesting the existing body under a `data` key. Nesting is
tidier, but it would rewrite every call site in the console and every
assertion in the test suite at the point where the thing has to be demoed, and
a tidier response shape is not worth that risk. The guarantee that matters —
every response carries the same metadata, and no endpoint ever returns a bare
500 — holds either way.
"""

import json
import logging
import threading
import time
import uuid
from collections import deque

LOG = logging.getLogger("blastradius")

STARTED_AT = time.time()

# (ttl seconds, label) per source. OSV moves when an advisory is filed;
# the registry moves when someone publishes; the graph only moves when the
# crawler runs.
TTL = {"osv": 900.0, "registry": 3600.0, "hydradb": 60.0}


class Cache:
    """A small TTL cache that reports whether it hit, because a latency number
    means something different when it came from memory."""

    def __init__(self, max_entries: int = 4000):
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0

    def get(self, key: str, ttl: float):
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                self.hits += 1
                return hit[1], True
        self.misses += 1
        return None, False

    def put(self, key: str, value):
        with self._lock:
            self._data[key] = (time.time(), value)
            if len(self._data) > self.max_entries:
                for k in sorted(self._data, key=lambda k: self._data[k][0])[:500]:
                    self._data.pop(k, None)

    def stats(self):
        total = self.hits + self.misses
        return {"entries": len(self._data), "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else None}


class RateLimiter:
    """Fixed-window-per-IP, in memory. Enough to stop one client saturating the
    OSV and registry calls this server makes on their behalf; it is not a
    defence against anything determined, and is not presented as one."""

    def __init__(self, limit: int = 120, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, ip: str):
        now = time.time()
        with self._lock:
            q = self._hits.setdefault(ip, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False, int(self.window - (now - q[0])) + 1
            q.append(now)
            if len(self._hits) > 5000:
                for k in [k for k, v in self._hits.items() if not v][:2000]:
                    self._hits.pop(k, None)
            return True, 0


def uptime_seconds() -> float:
    return round(time.time() - STARTED_AT, 1)


def setup_logging(level=logging.INFO):
    """One JSON object per line, so logs are greppable and machine-readable."""
    handler = logging.StreamHandler()

    class JsonLine(logging.Formatter):
        def format(self, record):
            payload = {
                "ts": round(record.created, 3),
                "level": record.levelname.lower(),
                "msg": record.getMessage(),
            }
            for k, v in getattr(record, "extra_fields", {}).items():
                payload[k] = v
            return json.dumps(payload, default=str)

    handler.setFormatter(JsonLine())
    LOG.handlers = [handler]
    LOG.setLevel(level)
    LOG.propagate = False
    return LOG


def log(msg: str, **fields):
    LOG.info(msg, extra={"extra_fields": fields})


def request_id() -> str:
    return uuid.uuid4().hex[:16]
