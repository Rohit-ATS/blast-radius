"""Traversals that chain across edge types — the queries a lookup table cannot answer.

Every question here starts at one kind of node and ends at another, crossing a
different relationship on the way:

    Maintainer -MAINTAINS-> Package -REQUIRED_BY*-> the whole blast radius
    Advisory   -AFFECTS->   Package -REQUIRED_BY*-> everyone the CVE reaches
    Package    -SIMILAR_TO-> impostor -REQUIRED_BY*-> who already installed it

That shape is the entire argument for a graph. "Which packages does qix
maintain" is a join. "How many packages transitively depend on anything qix
maintains" is a traversal across two relationship types, and it changes answer
the moment any edge anywhere in the graph moves.

HydraDB 0.1.0 constraint that shapes all of this: a variable-length MATCH needs
a *fixed source id*, so each stage of a chain is its own query anchored at a
known vertex, and the stages are fired concurrently. It cannot return a path at
all — no length(path), no path binding — so where a concrete chain is required
it is rebuilt from the sidecar edge table and then re-verified hop by hop
against the graph, rather than asserted.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from hydra import Hydra, RESULT_LIMIT, nid
from blast import REACH_COUNT, REACH_NAMES, _depth

MAINTAINS = "MATCH (m {id: $id})-[:MAINTAINS]->(p) RETURN p.name"
AFFECTS = "MATCH (a {id: $id})-[:AFFECTS]->(p) RETURN p.name"
SIMILAR = "MATCH (p {id: $id})-[:SIMILAR_TO]->(q) RETURN q.name"
ADVISORY_NODE = ("MATCH (a:Advisory {id: $id}) "
                 "RETURN a.osv_id, a.severity, a.is_malware, a.summary")
ONE_HOP = "MATCH (t {id: $id})-[:REQUIRED_BY*1..1]->(v) RETURN DISTINCT v.name"


def _reach(h, name, depth):
    rows = h.query(REACH_NAMES % depth, {"id": nid(name), "limit": RESULT_LIMIT})
    return {r["v.name"] for r in rows if r.get("v.name")}


def _count(h, name, depth):
    rows = h.query(REACH_COUNT % depth, {"id": nid(name)})
    return rows[0]["count(*)"] if rows else 0


# --------------------------------------------------------------------------
# 1. attack surface of a maintainer
# --------------------------------------------------------------------------

def attack_surface(h: Hydra, maintainer: str, depth: int = 4,
                   max_packages: int = 40):
    """Everything one account can reach. Two hops, two edge types.

    This is the number that matters after a maintainer is phished: not "how
    many packages did they publish", but how much of npm sits downstream of
    those packages. The September 2025 attack began with exactly one account.
    """
    d = _depth(depth)
    t0 = time.perf_counter()
    rows = h.query(MAINTAINS, {"id": nid("maint:" + maintainer)})
    packages = sorted({r["p.name"] for r in rows if r.get("p.name")})
    if not packages:
        return {"maintainer": maintainer, "controls": [], "package_count": 0,
                "total_exposed": 0, "depth": d,
                "message": f"no packages recorded for maintainer '{maintainer}'",
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    # The union matters, not the sum: two packages by the same author usually
    # share much of their downstream, and adding the counts double-counts it.
    scope = packages[:max_packages]
    with ThreadPoolExecutor(max_workers=min(10, len(scope))) as pool:
        reaches = list(pool.map(lambda p: (p, _reach(h, p, d)), scope))

    union: set[str] = set()
    for _, r in reaches:
        union |= r
    union -= set(packages)

    controls = sorted(({"package": p, "exposed": len(r)} for p, r in reaches),
                      key=lambda x: -x["exposed"])
    return {
        "maintainer": maintainer,
        "package_count": len(packages),
        "controls": controls,
        "analysed_packages": len(scope),
        "truncated": len(packages) > len(scope),
        "total_exposed": len(union),
        "sum_of_parts": sum(c["exposed"] for c in controls),
        "depth": d,
        "headline": (f"{maintainer} controls {len(packages):,} package"
                     f"{'' if len(packages) == 1 else 's'}. "
                     f"{len(union):,} packages depend on them."),
        "queries": 1 + len(scope),
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# --------------------------------------------------------------------------
# 2. why am I exposed — the actual chain
# --------------------------------------------------------------------------

def why_exposed(h: Hydra, db, source: str, target: str, depth: int = 6):
    """Not "you are exposed" — the specific chain, hop by hop.

    The depth at which `target` first becomes reachable is computed in the
    graph and is authoritative. The concrete chain cannot be: HydraDB 0.1.0
    returns no path binding, so it is rebuilt from the sidecar edge table and
    then every hop is re-confirmed with a one-hop graph query. If the graph
    disagrees with the reconstruction, the response says so rather than
    printing a chain nobody verified.
    """
    d = _depth(depth)
    t0 = time.perf_counter()

    found_at = None
    seen: set[str] = set()
    for k in range(1, d + 1):
        reach = _reach(h, source, k)
        if target in reach:
            found_at = k
            seen = reach
            break
        seen = reach
    if found_at is None:
        return {"from": source, "to": target, "connected": False,
                "searched_depth": d, "reachable_at_depth": None,
                "message": (f"{target} is not reachable from {source} within "
                            f"{d} hops ({len(seen):,} packages searched)"),
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    # Rebuild backwards through the sidecar: who depends on whom.
    chain = _reconstruct(db, source, target, found_at)
    verified = _verify_chain(h, chain) if chain else False

    return {
        "from": source, "to": target, "connected": True,
        "reachable_at_depth": found_at,
        "path": chain,
        "hops": len(chain) - 1 if chain else None,
        "graph_verified": verified,
        "explanation": _explain(chain) if chain else
                       (f"{target} is reachable from {source} in {found_at} hops, "
                        f"but the concrete chain could not be rebuilt from the "
                        f"sidecar edge table."),
        "queries": found_at + (len(chain) - 1 if chain and verified else 0),
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _reconstruct(db, source, target, want_len):
    """BFS over the sidecar edges from source to target, bounded by want_len."""
    frontier = [source]
    parent: dict[str, str] = {}
    seen = {source}
    for _ in range(want_len):
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        rows = db.execute(
            f"SELECT dst, src FROM deps WHERE dst IN ({marks}) AND kind = 'prod'",
            frontier).fetchall()
        nxt = []
        for dep, dependent in rows:
            if dependent in seen:
                continue
            seen.add(dependent)
            parent[dependent] = dep
            if dependent == target:
                chain = [target]
                while chain[-1] in parent:
                    chain.append(parent[chain[-1]])
                return list(reversed(chain))
            nxt.append(dependent)
        frontier = nxt
    return None


def _verify_chain(h: Hydra, chain):
    """Confirm every hop really exists in the graph, not just the sidecar."""
    if not chain or len(chain) < 2:
        return False
    def hop(i):
        rows = h.query(ONE_HOP, {"id": nid(chain[i])})
        return chain[i + 1] in {r["v.name"] for r in rows if r.get("v.name")}
    with ThreadPoolExecutor(max_workers=min(8, len(chain) - 1)) as pool:
        return all(pool.map(hop, range(len(chain) - 1)))


def _explain(chain):
    if not chain or len(chain) < 2:
        return ""
    steps = [f"{chain[i + 1]} depends on {chain[i]}" for i in range(len(chain) - 1)]
    return " → ".join(chain) + ".  " + "; ".join(steps) + "."


# --------------------------------------------------------------------------
# 3. blast radius of an advisory
# --------------------------------------------------------------------------

def blast_advisory(h: Hydra, osv_id: str, depth: int = 4):
    """Start at the CVE, not the package. Advisory -AFFECTS-> Package -REQUIRED_BY*->

    An advisory usually names several packages, and asking "how far does
    GHSA-xxxx actually reach" is a different question from asking about any one
    of them.
    """
    d = _depth(depth)
    t0 = time.perf_counter()
    aid = nid("adv:" + osv_id)
    meta_rows = h.query(ADVISORY_NODE, {"id": aid})
    rows = h.query(AFFECTS, {"id": aid})
    packages = sorted({r["p.name"] for r in rows if r.get("p.name")})
    if not packages:
        return {"osv_id": osv_id, "affects": [], "total_exposed": 0, "depth": d,
                "message": f"{osv_id} is not in the graph",
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    with ThreadPoolExecutor(max_workers=min(10, len(packages))) as pool:
        reaches = list(pool.map(lambda p: (p, _reach(h, p, d)), packages[:40]))
    union: set[str] = set()
    for _, r in reaches:
        union |= r
    union -= set(packages)

    meta = meta_rows[0] if meta_rows else {}
    return {
        "osv_id": osv_id,
        "severity": meta.get("a.severity") or "",
        "is_malware": bool(meta.get("a.is_malware")),
        "summary": meta.get("a.summary") or "",
        "affects": [{"package": p, "exposed": len(r)} for p, r in
                    sorted(reaches, key=lambda x: -len(x[1]))],
        "package_count": len(packages),
        "total_exposed": len(union),
        "depth": d,
        "headline": (f"{osv_id} directly affects {len(packages):,} package"
                     f"{'' if len(packages) == 1 else 's'}; "
                     f"{len(union):,} more depend on them."),
        "queries": 2 + len(reaches),
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# --------------------------------------------------------------------------
# 4. typosquat risk — a squat with dependents is an incident
# --------------------------------------------------------------------------

def typosquat_risk(h: Hydra, name: str, depth: int = 3):
    """SIMILAR_TO neighbours, then how many packages already pull each one.

    A near-miss name nobody uses is trivia. A near-miss name with real
    dependents means somebody has already installed the wrong thing.
    """
    d = _depth(depth)
    t0 = time.perf_counter()
    rows = h.query(SIMILAR, {"id": nid(name)})
    neighbours = sorted({r["q.name"] for r in rows if r.get("q.name")})
    if not neighbours:
        return {"name": name, "neighbours": [], "at_risk": 0, "depth": d,
                "message": (f"no similarly-named package in the graph for "
                            f"'{name}'"),
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    with ThreadPoolExecutor(max_workers=min(10, len(neighbours))) as pool:
        counts = list(pool.map(lambda n: (n, _count(h, n, d)), neighbours[:40]))
    ranked = sorted(({"name": n, "dependents": c} for n, c in counts),
                    key=lambda x: -x["dependents"])
    active = [r for r in ranked if r["dependents"] > 0]
    return {
        "name": name,
        "neighbours": ranked,
        "neighbour_count": len(neighbours),
        "at_risk": len(active),
        "worst": ranked[0] if ranked else None,
        "depth": d,
        "headline": (f"{len(neighbours):,} package"
                     f"{'' if len(neighbours) == 1 else 's'} in the graph sit one "
                     f"edit from {name}"
                     + (f"; {ranked[0]['name']} already has "
                        f"{ranked[0]['dependents']:,} dependents."
                        if active else "; none of them have dependents.")),
        "queries": 1 + len(counts),
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
