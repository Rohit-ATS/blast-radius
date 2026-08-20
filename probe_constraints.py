"""Empirical map of HydraDB 0.1.0's supported query surface.

This is evidence, not scaffolding: the PASS/FAIL table it prints is what the
"constraints we hit and engineered around" section of the README is built from.
Re-run it against any HydraDB build to see whether a constraint still holds.

Run:  py probe_constraints.py
"""

import json
import sys
import time

import requests

URL = "http://127.0.0.1:8443/v1/graphs/default/query"
HEADERS = {
    "Authorization": "Bearer local-development-token-32-bytes",
    "X-Graph-Namespace": "default",
    "Content-Type": "application/json",
}

# (label, expectation, query, params) — expectation is what we believe today.
TESTS = [
    ("integer vertex id + MERGE + SET", "WORKS",
     "UNWIND $rows AS row MERGE (p {id: row.id}) SET p:Package, p.name = row.name",
     {"rows": [{"id": 900000001, "name": "_probe_a"}, {"id": 900000002, "name": "_probe_b"}]}),

    ("string vertex id", "FAILS",
     "UNWIND $rows AS row MERGE (p {id: row.id}) SET p:Package, p.name = row.name",
     {"rows": [{"id": "_probe_string_id", "name": "_probe_string"}]}),

    ("CREATE edge between two inline ids", "WORKS",
     "UNWIND $rows AS row CREATE (a {id: row.src})-[:REQUIRED_BY]->(b {id: row.dst})",
     {"rows": [{"src": 900000001, "dst": 900000002}]}),

    ("MATCH ... MERGE edge inside UNWIND", "FAILS",
     "UNWIND $rows AS row MATCH (a {id: row.src}) MATCH (b {id: row.dst}) MERGE (a)-[:REQUIRED_BY]->(b)",
     {"rows": [{"src": 900000001, "dst": 900000002}]}),

    ("var-length traversal from fixed source id", "WORKS",
     "MATCH (t {id: $id})-[:REQUIRED_BY*1..5]->(v) RETURN v.name",
     {"id": 900000001}),

    ("count(*) over a label", "WORKS",
     "MATCH (p:Package) RETURN count(*)", None),

    ("length(path) in RETURN", "FAILS",
     "MATCH path = (t {id: $id})-[:REQUIRED_BY*1..3]->(v) RETURN length(path)",
     {"id": 900000001}),

    ("var-length MATCH with no fixed source", "FAILS",
     "MATCH (a:Package)-[:REQUIRED_BY*1..2]->(b:Package) RETURN b.name LIMIT 5", None),

    ("CREATE INDEX", "FAILS",
     "CREATE INDEX ON :Package(id)", None),

    ("bare MATCH (n) with no predicate", "FAILS",
     "MATCH (n) RETURN count(*)", None),

    ("filter on edge property during traversal", "FAILS",
     "MATCH (t {id: $id})-[r:REQUIRED_BY*1..3]->(v) WHERE r.kind = 'prod' RETURN v.name",
     {"id": 900000001}),

    ("DISTINCT in RETURN", "?",
     "MATCH (t {id: $id})-[:REQUIRED_BY*1..3]->(v) RETURN DISTINCT v.name",
     {"id": 900000001}),

    ("count(DISTINCT x)", "?",
     "MATCH (t {id: $id})-[:REQUIRED_BY*1..3]->(v) RETURN count(DISTINCT v.name)",
     {"id": 900000001}),

    ("LIMIT on traversal", "?",
     "MATCH (t {id: $id})-[:REQUIRED_BY*1..5]->(v) RETURN v.name LIMIT 10",
     {"id": 900000001}),

    ("$param as upper bound in *1..$n", "?",
     "MATCH (t {id: $id})-[:REQUIRED_BY*1..$d]->(v) RETURN v.name",
     {"id": 900000001, "d": 3}),

    ("SET a label via SET p:Label", "?",
     "UNWIND $rows AS row MERGE (p {id: row.id}) SET p:Package",
     {"rows": [{"id": 900000003}]}),

    ("property equality predicate on non-id property", "?",
     "MATCH (p:Package {name: $n}) RETURN p.id", {"n": "_probe_a"}),

    ("WHERE on a matched node property", "?",
     "MATCH (p:Package) WHERE p.name = $n RETURN p.id", {"n": "_probe_a"}),

    ("ORDER BY on a returned property", "?",
     "MATCH (t {id: $id})-[:REQUIRED_BY*1..3]->(v) RETURN v.name ORDER BY v.name",
     {"id": 900000001}),

    ("DELETE by id", "?",
     "MATCH (p {id: $id}) DETACH DELETE p", {"id": 900000003}),
]

CLEANUP = [
    ("MATCH (p {id: $id}) DETACH DELETE p", {"id": 900000001}),
    ("MATCH (p {id: $id}) DETACH DELETE p", {"id": 900000002}),
    ("MATCH (p {id: $id}) DETACH DELETE p", {"id": 900000003}),
]


def run(query, params):
    body = {"cell_id": "cell-0", "query": query, "consistency": "causal"}
    if params:
        body["parameters"] = params
    t0 = time.perf_counter()
    try:
        r = requests.post(URL, data=json.dumps(body), headers=HEADERS, timeout=60)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:150]}", ms
        payload = r.json()
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return False, msg[:150], ms
        return True, json.dumps(payload)[:150], ms
    except Exception as e:
        return False, f"{e.__class__.__name__}: {e}"[:150], (time.perf_counter() - t0) * 1000


if __name__ == "__main__":
    print("=" * 100)
    print(f"{'result':<7} {'expected':<9} {'ms':>7}  test")
    print("-" * 100)
    surprises = []
    for label, expect, q, p in TESTS:
        ok, detail, ms = run(q, p)
        got = "WORKS" if ok else "FAILS"
        flag = ""
        if expect in ("WORKS", "FAILS") and got != expect:
            flag = "  <-- SURPRISE"
            surprises.append((label, expect, got, detail))
        print(f"{got:<7} {expect:<9} {ms:>7.1f}  {label}{flag}")
        print(f"{'':>26}{detail}")
    for q, p in CLEANUP:
        run(q, p)
    print("=" * 100)
    if surprises:
        print(f"\n{len(surprises)} SURPRISES — the assumed constraint map is wrong here:")
        for label, expect, got, detail in surprises:
            print(f"  - {label}: expected {expect}, got {got}\n      {detail}")
    else:
        print("\nno surprises: the documented constraint map holds.")
    sys.exit(0)
