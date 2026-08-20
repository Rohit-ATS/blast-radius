"""Second probe: what exactly does count(*) count on a variable-length match,
and can edges be counted at all? The depth-histogram design depends on this.

Builds a known 6-node shape, asks, then deletes it.

  a -> b -> c -> d        (chain, so depth 1..3 from a)
  a -> e                  (a second child at depth 1)
  b -> e                  (a diamond: e reachable at depth 1 and 2)

Truth from a: distinct victims at depth 1 = {b,e}=2, 1..2 = {b,e,c}=3,
1..3 = {b,e,c,d}=4.  Path counts differ from that because of the diamond.
"""

import json
import time

import requests

URL = "http://127.0.0.1:8443/v1/graphs/default/query"
HEADERS = {
    "Authorization": "Bearer local-development-token-32-bytes",
    "X-Graph-Namespace": "default",
    "Content-Type": "application/json",
}

IDS = {n: 910000000 + i for i, n in enumerate("abcde")}
EDGES = [("a", "b"), ("b", "c"), ("c", "d"), ("a", "e"), ("b", "e")]


def q(query, params=None):
    body = {"cell_id": "cell-0", "query": query, "consistency": "causal"}
    if params:
        body["parameters"] = params
    t0 = time.perf_counter()
    r = requests.post(URL, data=json.dumps(body), headers=HEADERS, timeout=60)
    ms = (time.perf_counter() - t0) * 1000
    if r.status_code >= 400:
        return None, r.text[:200], ms
    p = r.json()
    return p.get("rows", []), p.get("columns", []), ms


def val(rows):
    out = []
    for row in rows or []:
        out.append([c["value"] if isinstance(c, dict) and "value" in c else c for c in row])
    return out


print("--- build ---")
q("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:Package, p.name = row.name",
  {"rows": [{"id": i, "name": f"_c_{n}"} for n, i in IDS.items()]})
q("UNWIND $rows AS row CREATE (a {id: row.src})-[:REQUIRED_BY]->(b {id: row.dst})",
  {"rows": [{"src": IDS[s], "dst": IDS[d]} for s, d in EDGES]})

print("\n--- count(*) vs DISTINCT names, from a ---")
for d in (1, 2, 3, 4):
    rows, cols, ms = q(f"MATCH (t {{id: $id}})-[:REQUIRED_BY*1..{d}]->(v) RETURN count(*)",
                       {"id": IDS["a"]})
    c = val(rows)
    rows2, _, ms2 = q(f"MATCH (t {{id: $id}})-[:REQUIRED_BY*1..{d}]->(v) RETURN DISTINCT v.name",
                      {"id": IDS["a"]})
    names = sorted(r[0] for r in val(rows2))
    print(f"  depth 1..{d}: count(*)={c}  ({ms:.0f}ms)   DISTINCT names={names} n={len(names)} ({ms2:.0f}ms)")

print("\n--- non-distinct names, to see duplicates ---")
rows, _, ms = q("MATCH (t {id: $id})-[:REQUIRED_BY*1..3]->(v) RETURN v.name", {"id": IDS["a"]})
print("  ", sorted(r[0] for r in val(rows)))

print("\n--- edge counting attempts ---")
for label, query in [
    ("MATCH ()-[r:REQUIRED_BY]->() RETURN count(*)",
     "MATCH ()-[r:REQUIRED_BY]->() RETURN count(*)"),
    ("MATCH (a:Package)-[:REQUIRED_BY]->(b) RETURN count(*)",
     "MATCH (a:Package)-[:REQUIRED_BY]->(b) RETURN count(*)"),
    ("MATCH (a:Package)-[:REQUIRED_BY]->(b:Package) RETURN count(*)",
     "MATCH (a:Package)-[:REQUIRED_BY]->(b:Package) RETURN count(*)"),
    ("single-hop from fixed id RETURN count(*)",
     "MATCH (t {id: $id})-[:REQUIRED_BY*1..1]->(v) RETURN count(*)"),
]:
    rows, cols, ms = q(query, {"id": IDS["a"]})
    print(f"  {label}\n      -> {val(rows) if rows is not None else cols} ({ms:.0f}ms)")

print("\n--- does re-CREATE duplicate an edge? ---")
q("UNWIND $rows AS row CREATE (a {id: row.src})-[:REQUIRED_BY]->(b {id: row.dst})",
  {"rows": [{"src": IDS["a"], "dst": IDS["b"]}]})
rows, _, ms = q("MATCH (t {id: $id})-[:REQUIRED_BY*1..1]->(v) RETURN v.name", {"id": IDS["a"]})
print("  after duplicate CREATE a->b, depth-1 names:", sorted(r[0] for r in val(rows)))

print("\n--- MERGE-ing an existing id: does it clobber props? ---")
q("UNWIND $rows AS row MERGE (p {id: row.id}) SET p:Package, p.latest = row.latest",
  {"rows": [{"id": IDS["a"], "latest": "9.9.9"}]})
rows, _, _ = q("MATCH (p:Package {name: $n}) RETURN p.name, p.latest", {"n": "_c_a"})
print("  name+latest after partial SET:", val(rows))

print("\n--- cleanup ---")
for i in IDS.values():
    q("MATCH (p {id: $id}) DETACH DELETE p", {"id": i})
print("done")
