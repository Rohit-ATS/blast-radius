"""Thin HydraDB client over the HTTP JSON query API.

HydraDB also speaks Bolt (neo4j://127.0.0.1:7687) so the official `neo4j`
Python driver works. HTTP is used here because it has no driver dependency,
no routing table negotiation, and gives clearer errors while iterating fast.
"""

import json
import os
import time
from typing import Any, Iterable

import requests

HYDRA_URL = os.environ.get("HYDRA_URL", "http://127.0.0.1:8443")
HYDRA_TOKEN = os.environ.get("HYDRA_TOKEN", "local-development-token-32-bytes")
HYDRA_GRAPH = os.environ.get("HYDRA_GRAPH", "default")
HYDRA_CELL = os.environ.get("HYDRA_CELL", "cell-0")


class HydraError(RuntimeError):
    pass


class Hydra:
    def __init__(self, url: str = HYDRA_URL, token: str = HYDRA_TOKEN,
                 graph: str = HYDRA_GRAPH, cell: str = HYDRA_CELL):
        self.endpoint = f"{url}/v1/graphs/{graph}/query"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "X-Graph-Namespace": graph,
            "Content-Type": "application/json",
        })
        self.cell = cell

    def query(self, cypher: str, params: dict[str, Any] | None = None,
              consistency: str = "causal", retries: int = 3) -> list[dict]:
        body: dict[str, Any] = {"cell_id": self.cell, "query": cypher,
                                "consistency": consistency}
        if params:
            body["parameters"] = params
        last = None
        for attempt in range(retries):
            try:
                r = self.session.post(self.endpoint, data=json.dumps(body), timeout=180)
                if r.status_code >= 400:
                    raise HydraError(f"{r.status_code}: {r.text[:600]}")
                return _rows(r.json())
            except (requests.RequestException, HydraError) as e:
                last = e
                if attempt == retries - 1:
                    break
                time.sleep(1.5 * (attempt + 1))
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
                    self.query("CREATE (p:_Probe {id: 'ready'})")
                    self.query("MATCH (p:_Probe {id: 'ready'}) DELETE p")
                    return
            except Exception:
                pass
            time.sleep(3)
        raise HydraError("HydraDB never became ready — check `docker compose logs hydradb`")


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
    if isinstance(v, dict) and set(v.keys()) == {"type", "value"}:
        return v["value"]
    if isinstance(v, dict):
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


if __name__ == "__main__":
    h = Hydra()
    h.wait_ready()
    print("hydradb ready and round-tripping writes")
