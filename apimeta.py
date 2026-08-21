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


# How long each kind of answer stays true enough to reuse.
#
# These are not arbitrary. An advisory that appears on osv.dev is news for
# hours, so fifteen minutes of staleness costs nothing and saves an outbound
# call on every audit of a shared dependency. A registry document changes only
# when someone publishes, so an hour is generous and still catches a publish
# well inside the window the live feed already watches. A graph traversal is
# expensive and the graph moves only as fast as the crawler writes, so a minute
# absorbs the burst of identical requests a single page load produces without
# ever showing a number that is meaningfully old.
TTL_OSV = 900.0        # 15 minutes
TTL_REGISTRY = 3600.0  # 1 hour
TTL_GRAPH = 60.0       # 1 minute


class Cache:
    """A small TTL cache that reports whether it hit, because a latency number
    means something different when it came from memory.

    Counters are kept per namespace as well as in total. One aggregate hit rate
    hides the thing worth knowing: OSV and the registry should hit most of the
    time, and if they are not, the TTLs are wrong or the keys are.
    """

    def __init__(self, max_entries: int = 4000):
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self._ns: dict[str, dict[str, int]] = {}

    def _bump(self, key: str, field: str) -> None:
        ns = key.split(":", 1)[0] if ":" in key else "other"
        slot = self._ns.setdefault(ns, {"hits": 0, "misses": 0})
        slot[field] += 1

    def get(self, key: str, ttl: float):
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                self.hits += 1
                self._bump(key, "hits")
                return hit[1], True
            self.misses += 1
            self._bump(key, "misses")
        return None, False

    def put(self, key: str, value):
        with self._lock:
            self._data[key] = (time.time(), value)
            if len(self._data) > self.max_entries:
                for k in sorted(self._data, key=lambda k: self._data[k][0])[:500]:
                    self._data.pop(k, None)
                    self.evictions += 1

    def cached(self, key: str, ttl: float, produce):
        """get-or-produce. The caller does not have to remember to put()."""
        value, hit = self.get(key, ttl)
        if hit:
            return value
        value = produce()
        self.put(key, value)
        return value

    def stats(self):
        total = self.hits + self.misses
        by_ns = {}
        for ns, c in sorted(self._ns.items()):
            t = c["hits"] + c["misses"]
            by_ns[ns] = {**c, "hit_rate": round(c["hits"] / t, 3) if t else None}
        return {"entries": len(self._data), "hits": self.hits,
                "misses": self.misses, "evictions": self.evictions,
                "hit_rate": round(self.hits / total, 3) if total else None,
                "ttl_seconds": {"osv": TTL_OSV, "registry": TTL_REGISTRY,
                                "graph": TTL_GRAPH},
                "by_source": by_ns}


# One cache for the whole process. Previously intel.py kept a private dict with
# a flat TTL and no counters, while /api/health reported a separate instance
# that nothing ever called — so the hit rate read 0 forever no matter how well
# the caching was actually working.
CACHE = Cache()


class KeyQuota:
    """Per-key ceilings: a burst limit and a daily cap.

    An API key used to be exempt from rate limiting entirely, on the reasoning
    that a key identifies a real integrator. That is exactly backwards for the
    failure that matters: a leaked key is *more* dangerous than an anonymous
    flood, because it is trusted. One key in a public repository would have
    drained the OSV quota and pinned the CPU with nothing to stop it and no
    signal that it was happening.

    So a key raises the ceiling rather than removing it. The daily cap is the
    part that actually bounds the bill — a per-minute limit alone still permits
    a sustained 24-hour drain.
    """

    def __init__(self, per_minute: int = 3000, per_day: int = 250_000):
        self.per_minute = per_minute
        self.per_day = per_day
        self._minute: dict[str, deque] = {}
        self._day: dict[str, list] = {}          # [window_start, count]
        self._lock = threading.Lock()

    def check(self, key_id: str):
        """(allowed, retry_after_s, scope). `scope` says which ceiling bit, so
        the 429 can explain itself instead of being a bare refusal."""
        now = time.time()
        with self._lock:
            day = self._day.setdefault(key_id, [now, 0])
            if now - day[0] >= 86400:
                day[0], day[1] = now, 0
            if day[1] >= self.per_day:
                return False, int(86400 - (now - day[0])) + 1, "daily"

            q = self._minute.setdefault(key_id, deque())
            while q and now - q[0] > 60.0:
                q.popleft()
            if len(q) >= self.per_minute:
                return False, int(60 - (now - q[0])) + 1, "burst"

            q.append(now)
            day[1] += 1

            if len(self._minute) > 5000:
                for k in [k for k, v in self._minute.items() if not v][:2000]:
                    self._minute.pop(k, None)
            return True, 0, ""

    def usage(self, key_id: str) -> dict:
        with self._lock:
            day = self._day.get(key_id) or [time.time(), 0]
            minute = len(self._minute.get(key_id) or ())
        return {"minute_used": minute, "minute_limit": self.per_minute,
                "day_used": day[1], "day_limit": self.per_day,
                "day_resets_in_s": max(0, int(86400 - (time.time() - day[0])))}

    def stats(self) -> dict:
        with self._lock:
            return {"keys_seen": len(self._day),
                    "per_minute": self.per_minute, "per_day": self.per_day}


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


class SingleFlight:
    """Coalesce concurrent identical work into one execution.

    A blast-radius traversal over a hub package takes seconds, and the console,
    a demo audience and a load check all tend to ask for the *same* preset at
    the same moment. Without this, twenty-four concurrent requests become
    twenty-four concurrent traversals, HydraDB saturates, and everyone gets a
    503 — including the twenty-three who would have been perfectly happy with
    the answer the first one was already computing.

    The first caller for a key runs the work; the rest block on the same result
    and are told they waited rather than computed, so a latency number still
    means what it says.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._results: dict[str, tuple] = {}
        self.coalesced = 0
        self.executed = 0

    def run(self, key: str, produce, timeout: float = 60.0):
        """(value, was_coalesced). Raises whatever `produce` raises, for the
        leader and every follower alike — a shared failure is still shared."""
        with self._lock:
            waiting = self._inflight.get(key)
            if waiting is None:
                event = threading.Event()
                self._inflight[key] = event
                leader = True
            else:
                event, leader = waiting, False

        if not leader:
            self.coalesced += 1
            if not event.wait(timeout):
                # The leader is taking longer than a follower is willing to
                # wait. Doing the work independently is better than failing.
                return produce(), False
            ok, payload = self._results.get(key, (False, None))
            if ok:
                return payload, True
            if isinstance(payload, BaseException):
                raise payload
            return produce(), False

        self.executed += 1
        try:
            value = produce()
            self._results[key] = (True, value)
            return value, False
        except BaseException as exc:
            self._results[key] = (False, exc)
            raise
        finally:
            event.set()
            with self._lock:
                self._inflight.pop(key, None)
            # The result is only needed while followers are still waking up.
            threading.Timer(5.0, lambda: self._results.pop(key, None)).start()

    def stats(self) -> dict:
        total = self.executed + self.coalesced
        return {"executed": self.executed, "coalesced": self.coalesced,
                "coalesce_rate": (round(self.coalesced / total, 3)
                                  if total else None)}
