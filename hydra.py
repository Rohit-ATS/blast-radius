"""Thin HydraDB client over the HTTP JSON query API.

HydraDB also speaks Bolt (neo4j://127.0.0.1:7687) so the official `neo4j`
Python driver works. HTTP is used here because it has no driver dependency,
no routing table negotiation, and gives clearer errors while iterating fast.
"""

import hashlib
import json
import os
import random
import threading
import time
from typing import Any, Iterable

import requests

HYDRA_URL = os.environ.get("HYDRA_URL", "http://127.0.0.1:8443")
HYDRA_GRAPH = os.environ.get("HYDRA_GRAPH", "default")
HYDRA_CELL = os.environ.get("HYDRA_CELL", "cell-0")

# The well-known token from the local compose file. It is a placeholder, not a
# secret, and it is in the public repo because a reader needs it to run this
# locally in one command.
DEV_TOKEN = "local-development-token-32-bytes"

# Which is exactly why it must never silently become the production token. A
# default that works everywhere is a default nobody notices, right up until the
# graph is on the internet with a password that is printed in the README. So the
# fallback is allowed only when HydraDB is on this machine; anywhere else, an
# unset HYDRA_TOKEN is a startup failure rather than a quiet downgrade.
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
_is_local = any(h in HYDRA_URL for h in _LOCAL_HOSTS)

HYDRA_TOKEN = os.environ.get("HYDRA_TOKEN") or ""
if not HYDRA_TOKEN:
    if _is_local:
        HYDRA_TOKEN = DEV_TOKEN
    else:
        raise RuntimeError(
            f"HYDRA_TOKEN is not set and HYDRA_URL ({HYDRA_URL}) is not local. "
            "Refusing to fall back to the development token, which is public. "
            "Generate one with `openssl rand -hex 32` and set it as a secret.")
elif HYDRA_TOKEN == DEV_TOKEN and not _is_local:
    raise RuntimeError(
        "HYDRA_TOKEN is the public development token but HydraDB is remote. "
        "Generate a real one with `openssl rand -hex 32`.")


class HydraError(RuntimeError):
    pass


# Reserved id for the readiness round-trip. Deleted immediately after use.
_PROBE_ID = 999999999999999

# Admission control rejects any single query asking for more than this
# ("query_result_limit rejected by admission control: actual 200000 exceeds
# limit 100000"), so an oversized limit is a 429, not a silent truncation.
RESULT_LIMIT = 100_000


class Hydra:
    def __init__(self, url: str = HYDRA_URL, token: str = HYDRA_TOKEN,
                 graph: str = HYDRA_GRAPH, cell: str = HYDRA_CELL,
                 timeout: float = 180.0, budget: float | None = None):
        self.endpoint = f"{url}/v1/graphs/{graph}/query"
        self.cell = cell
        self.timeout = timeout
        self.budget = budget
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-Graph-Namespace": graph,
            "Content-Type": "application/json",
        }
        # One session per thread. The depth histogram fires its queries
        # concurrently and a requests.Session is not safe to share across
        # threads.
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self._headers)
            self._local.session = s
        return s

    def query(self, cypher: str, params: dict[str, Any] | None = None,
              consistency: str = "causal", retries: int = 5,
              budget: float | None = None) -> list[dict]:
        """Run a query, following cursors, retrying what is worth retrying.

        `budget` is a wall-clock ceiling for the whole call including retries.
        Without one, a cold HydraDB — which fails its own 30-second query
        timeout on every attempt — turns five patient retries into a request
        that takes over two minutes to admit defeat, by which point the browser
        has given up and the user has learned nothing. The crawler wants
        patience; a request handler wants an answer or an honest error.
        """
        body: dict[str, Any] = {"cell_id": self.cell, "query": cypher,
                                "consistency": consistency}
        if params:
            body["parameters"] = params
        budget = self.budget if budget is None else budget
        deadline = (time.time() + budget) if budget else None

        # HydraDB pages results at 1024 rows and returns a `next_cursor`. A
        # client that ignores it silently truncates every large answer — which
        # is precisely the sort of quietly-wrong number this tool exists to
        # prevent. Continuing a page needs BOTH the cursor and the originating
        # query_id; the cursor alone is rejected with "result cursor does not
        # belong to this query request".
        out: list[dict] = []
        cursor = None
        query_id = None
        while True:
            page = dict(body)
            if cursor is not None:
                page["cursor"] = cursor
                page["query_id"] = query_id
            payload = self._post(page, cypher, retries, deadline)
            out.extend(_rows(payload))
            if not isinstance(payload, dict):
                return out
            query_id = payload.get("query_id") or query_id
            cursor = payload.get("next_cursor")
            if cursor is None:
                return out

    def _post(self, body: dict, cypher: str, retries: int, deadline=None):
        """One request, with a retry policy that distinguishes 'try again' from
        'this will never work'.

        Both halves matter. Retrying a rejected query burns seconds on every
        genuine syntax error for nothing; *not* retrying a connection failure
        means a HydraDB restart shows up as user-visible errors, because the
        server accepts connections and serves /readyz before it can actually
        execute against a restored store.
        """
        last = None
        for attempt in range(retries):
            timeout = self.timeout
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                # Never wait past the budget: a request handler that blows its
                # deadline is indistinguishable from a hang.
                timeout = min(timeout, remaining)
            try:
                r = self.session.post(self.endpoint, data=json.dumps(body),
                                      timeout=timeout)
                if r.status_code >= 400:
                    raise HydraError(f"{r.status_code}: {r.text[:600]}")
                return r.json()
            except (requests.RequestException, HydraError) as e:
                last = e
                if isinstance(e, (requests.ConnectionError, requests.Timeout)):
                    # The pooled keep-alive socket is pointing at a process that
                    # no longer exists. Drop the whole session so the retry
                    # dials a fresh connection instead of reusing a dead one.
                    self._local.session = None
                elif not _retryable(e):
                    raise HydraError(f"{e}\n{cypher[:300]}") from None
                if attempt == retries - 1:
                    break
                # Exponential backoff with jitter: a cold HydraDB needs several
                # seconds, and unjittered retries from the histogram's parallel
                # queries would all land on the same instant.
                nap = min(6.0, 0.5 * 2 ** attempt) * (0.75 + random.random() / 2)
                if deadline is not None and time.time() + nap >= deadline:
                    break
                time.sleep(nap)
        raise HydraError(f"query failed after {retries} attempts: {last}\n{cypher[:300]}")

    def timed(self, cypher: str, params: dict[str, Any] | None = None,
              consistency: str = "causal") -> tuple[list[dict], float]:
        """Returns (rows, wall_ms). Use this everywhere the UI shows latency."""
        t0 = time.perf_counter()
        rows = self.query(cypher, params, consistency)
        return rows, (time.perf_counter() - t0) * 1000.0

    def write_batch(self, cypher: str, rows: Iterable[dict], chunk: int = 500,
                    key: str = "rows") -> int:
        """Batched UNWIND write. `cypher` must start with UNWIND $rows AS row."""
        buf: list[dict] = []
        total = 0
        for row in rows:
            buf.append(row)
            if len(buf) >= chunk:
                self.query(cypher, {key: buf})
                total += len(buf)
                buf = []
        if buf:
            self.query(cypher, {key: buf})
            total += len(buf)
        return total

    def wait_ready(self, admin: str = "http://127.0.0.1:9090", timeout: int = 180) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if requests.get(f"{admin}/readyz", timeout=5).status_code < 400:
                    # A listening port is not proof. Round-trip a write.
                    # Two constraints shape this probe: the id must be an
                    # integer (string ids are rejected at parse time), and MERGE
                    # only executes inside UNWIND ("MERGE with following clauses
                    # is not executable"). A bare MERGE here would make a healthy
                    # server look permanently unready.
                    self.query("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:_Probe",
                               {"rows": [{"id": _PROBE_ID}]})
                    self.query("MATCH (p {id: $id}) DETACH DELETE p", {"id": _PROBE_ID})
                    return
            except Exception:
                pass
            time.sleep(3)
        raise HydraError("HydraDB never became ready — check `docker compose logs hydradb`")


def _retryable(err: Exception) -> bool:
    """Is this failure worth another attempt?

    Transport failures and 5xx are transient. A 4xx is the engine telling us
    the query is wrong, and it will be just as wrong next time — the one
    exception being 429, which is retryable when it is backpressure but *not*
    when it is the deterministic result-size ceiling.
    """
    if isinstance(err, (requests.ConnectionError, requests.Timeout)):
        return True
    text = str(err)
    status = text.split(":", 1)[0].strip()
    if status == "429":
        return "query_result_limit" not in text
    if status == "408":
        # HydraDB's own 30-second query timeout. It is a 4xx, but it is very
        # much transient: a cold store fails this repeatedly and then answers
        # the identical query in a second once the working set is cached.
        return True
    if status.isdigit():
        return int(status) >= 500
    return True


def _rows(payload: Any) -> list[dict]:
    """Normalise HydraDB's typed JSON result shape into plain dicts.

    HydraDB returns typed values such as {"type":"vertex_id","value":2}. This
    unwraps one level so callers get ordinary Python values. If the response
    shape differs from what is assumed here, print the raw payload once and
    adjust — this is the single most likely place to need a five-minute fix.
    """
    if isinstance(payload, dict):
        cols = payload.get("columns") or payload.get("fields")
        data = payload.get("rows") if "rows" in payload else payload.get("data", [])
        out = []
        for row in data or []:
            if isinstance(row, dict):
                out.append({k: _unwrap(v) for k, v in row.items()})
            elif isinstance(row, list) and cols:
                out.append({c: _unwrap(v) for c, v in zip(cols, row)})
            else:
                out.append({"value": _unwrap(row)})
        return out
    return []


def _unwrap(v: Any) -> Any:
    # A null comes back as {"type": "null"} with no "value" key at all, which
    # is easy to miss: without this branch it survives as a truthy dict and an
    # absent property reads as present.
    if isinstance(v, dict) and v.get("type") == "null" and "value" not in v:
        return None
    if isinstance(v, dict) and set(v.keys()) == {"type", "value"}:
        return v["value"]
    if isinstance(v, dict):
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


# --------------------------------------------------------------------------
# vertex ids
# --------------------------------------------------------------------------
#
# HydraDB 0.1.0 addresses vertices by non-negative integer id only — a string
# id is rejected at parse time ("UNWIND row 0 field id must be a non-negative
# integer"). There is also no CREATE INDEX, so there is no server-side way to
# look a package up by name before writing it.
#
# Both problems have the same answer: derive the id from the name. nid() is a
# pure function, so any process — crawler, API, benchmark — computes the same
# id for "left-pad" without a round-trip, and writes become idempotent upserts
# addressed by id.
#
# 51 bits keeps every id inside JavaScript's safe integer range (2^53-1) so the
# browser can hold one without precision loss. Collision odds over 100k
# packages are ~2e-6; ingest.py records the name->id map in deps.db and flags a
# collision if two names ever land on one id.

_NID_BITS = 51
_NID_MASK = (1 << _NID_BITS) - 1


def nid(name: str) -> int:
    """Stable non-negative integer vertex id for a package name."""
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & _NID_MASK


# Ids are namespaced by entity kind, and packages additionally by ecosystem.
# `requests` exists on PyPI and on RubyGems and they are not the same package;
# without the prefix they would collide onto one vertex and quietly merge two
# dependency graphs into a wrong answer.

def pkg_id(name: str, ecosystem: str = "npm") -> int:
    """Vertex id for a package in a specific ecosystem."""
    return nid(f"{ecosystem}:{name}")


def maint_id(identity: str) -> int:
    """Vertex id for a maintainer.

    Deliberately *not* namespaced by ecosystem: one human publishing to npm and
    to PyPI is one node, which is what makes the cross-ecosystem question
    answerable. The adapter normalises the identity to an email where one
    exists, so the join happens on something globally unique.
    """
    return nid(f"maint:{identity}")


def adv_id(osv_id: str) -> int:
    """Vertex id for an advisory. OSV ids are already globally unique."""
    return nid(f"adv:{osv_id}")


if __name__ == "__main__":
    h = Hydra()
    h.wait_ready()
    print("hydradb ready and round-tripping writes")
    for sample in ("left-pad", "debug", "@types/node"):
        print(f"  nid({sample!r}) = {nid(sample)}")
