"""Map HydraDB's supported OpenCypher write surface before rewriting ingest.py.

Run:  py probe.py
Prints a PASS/FAIL table. Paste the whole output back into the chat.
"""

import json
import requests

URL = "http://127.0.0.1:8443/v1/graphs/default/query"
HEADERS = {
    "Authorization": "Bearer local-development-token-32-bytes",
    "X-Graph-Namespace": "default",
    "Content-Type": "application/json",
}

TESTS = [
    # ---- vertex upserts -------------------------------------------------
    ("A. MERGE by id + SET (the shape the error suggests)",
     "UNWIND $rows AS row MERGE (p:Package {id: row.id}) SET p.name = row.name, p.latest = row.latest",
     {"rows": [{"id": "debug", "name": "debug", "latest": "4.4.3"},
               {"id": "chalk", "name": "chalk", "latest": "6.0.0"}]}),

    ("B. same, but with RETURN appended",
     "UNWIND $rows AS row MERGE (p:Package {id: row.id}) SET p.name = row.name RETURN p.id AS id",
     {"rows": [{"id": "ms", "name": "ms"}]}),

    ("C. SET from a whole map (row spread)",
     "UNWIND $rows AS row MERGE (p:Package {id: row.id}) SET p += row",
     {"rows": [{"id": "semver", "name": "semver", "latest": "7.6.0"}]}),

    # ---- edge upserts ---------------------------------------------------
    ("D. MATCH both ends by id + MERGE edge, no edge props",
     "UNWIND $rows AS row MATCH (a:Package {id: row.src}) MATCH (b:Package {id: row.dst}) MERGE (a)-[:REQUIRES]->(b)",
     {"rows": [{"src": "debug", "dst": "ms"}]}),

    ("E. MERGE edge with properties in the pattern",
     "UNWIND $rows AS row MATCH (a:Package {id: row.src}) MATCH (b:Package {id: row.dst}) MERGE (a)-[r:REQUIRES {kind: row.kind}]->(b) SET r.range = row.range",
     {"rows": [{"src": "chalk", "dst": "ms", "kind": "prod", "range": "^2.1.3"}]}),

    ("F. MERGE edge then SET props separately",
     "UNWIND $rows AS row MATCH (a:Package {id: row.src}) MATCH (b:Package {id: row.dst}) MERGE (a)-[r:REQUIRES]->(b) SET r.kind = row.kind, r.range = row.range",
     {"rows": [{"src": "semver", "dst": "ms", "kind": "prod", "range": "^2.0.0"}]}),

    ("G. MERGE both vertices AND edge in one statement",
     "UNWIND $rows AS row MERGE (a:Package {id: row.src}) MERGE (b:Package {id: row.dst}) MERGE (a)-[:REQUIRES]->(b)",
     {"rows": [{"src": "express", "dst": "debug"}]}),

    # ---- traversal (the actual product) ---------------------------------
    ("H. variable-length reverse traversal — THE core query",
     "MATCH path = (victim:Package)-[:REQUIRES*1..3]->(target:Package {id: $name}) RETURN victim.id AS victim, length(path) AS depth LIMIT 20",
     {"name": "ms"}),

    ("I. aggregation over path length",
     "MATCH path = (v:Package)-[:REQUIRES*1..3]->(t:Package {id: $name}) RETURN length(path) AS depth, count(v) AS n",
     {"name": "ms"}),

    ("J. UNWIND a list of names in a read query (lockfile query)",
     "UNWIND $names AS mine MATCH (m:Package {id: mine}) RETURN m.id AS found",
     {"names": ["debug", "chalk", "nonexistent-xyz"]}),

    ("K. multi-label node + second index property",
     "UNWIND $rows AS row MERGE (r:Release {id: row.id}) SET r.name = row.name, r.version = row.version",
     {"rows": [{"id": "debug@4.4.3", "name": "debug", "version": "4.4.3"}]}),

    ("L. index creation",
     "CREATE INDEX ON :Package(id)", None),
]


def run(query, params):
    body = {"cell_id": "cell-0", "query": query}
    if params:
        body["parameters"] = params
    try:
        r = requests.post(URL, data=json.dumps(body), headers=HEADERS, timeout=60)
        payload = r.json()
        if isinstance(payload, dict) and "error" in payload:
            return False, payload["error"].get("message", "")[:180]
        return True, json.dumps(payload.get("rows", []))[:180]
    except Exception as e:
        return False, f"{e.__class__.__name__}: {e}"


if __name__ == "__main__":
    print("=" * 78)
    for label, q, p in TESTS:
        ok, detail = run(q, p)
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        print(f"       {detail}")
    print("=" * 78)
