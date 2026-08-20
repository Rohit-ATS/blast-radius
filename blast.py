"""Incident-response queries over the HydraDB npm graph.

Five questions, one per feature in the demo:

  1. blast_radius()        Which packages are transitively exposed, and how deep?
  2. would_resolve()       Whose version *ranges* would have pulled the bad
                           version — not just who lists the package.
  3. lockfile_exposure()   Drop in a package-lock.json: are you hit, and via
                           which exact path?
  4. maintainer_pivot()    What else does the compromised maintainer control?
                           (i.e. what gets attacked next)
  5. typosquat_ring()      Which near-miss names sit next to the target?

Everything returns (rows, latency_ms) so the UI can show real numbers.
"""

import json
import re
from functools import lru_cache

from hydra import Hydra

# --------------------------------------------------------------------------
# 1. blast radius — collapsed REQUIRES layer, one variable-length hop
# --------------------------------------------------------------------------

BLAST_RADIUS = """
MATCH path = (victim:Package)-[:REQUIRES*1..%d]->(target:Package {name: $name})
RETURN victim.name AS victim,
       length(path) AS depth
ORDER BY depth ASC, victim ASC
LIMIT $limit
"""

BLAST_COUNT_BY_DEPTH = """
MATCH path = (victim:Package)-[:REQUIRES*1..%d]->(target:Package {name: $name})
RETURN length(path) AS depth, count(DISTINCT victim.name) AS packages
ORDER BY depth ASC
"""

# The same shape via HydraDB's native path procedure. Snapshot-scoped,
# GraphBLAS-backed, no client-side fan-out. Use for the deep/wide case.
BLAST_RADIUS_NATIVE = """
CALL algo.SSpaths({
  sourceLabel: 'Package',
  sourceProperty: 'name',
  sourceValues: [$name],
  relTypes: ['REQUIRES'],
  relDirection: 'incoming',
  maxLen: $depth,
  pathCount: $path_count,
  resultLimit: $limit
})
YIELD path
RETURN path
"""


def blast_radius(h: Hydra, name: str, depth: int = 5, limit: int = 5000):
    return h.timed(BLAST_RADIUS % depth, {"name": name, "limit": limit})


def blast_summary(h: Hydra, name: str, depth: int = 5):
    return h.timed(BLAST_COUNT_BY_DEPTH % depth, {"name": name})


# --------------------------------------------------------------------------
# 2. would-resolve — the query a vector index cannot express at all
# --------------------------------------------------------------------------

DIRECT_DEPENDERS = """
MATCH (r:Release)-[d:DEPENDS_ON]->(p:Package {name: $name})
RETURN r.id AS release, r.name AS package, r.version AS version,
       r.published_at AS published_at, d.range AS range, d.kind AS kind
LIMIT $limit
"""


def would_resolve(h: Hydra, name: str, bad_version: str, limit: int = 20000):
    """Of everyone who depends on the package, whose declared semver range
    actually admits the malicious version? Listing a dependency is not the
    same as pulling it. This is the difference between a scary number and a
    true one."""
    rows, ms = h.timed(DIRECT_DEPENDERS, {"name": name, "limit": limit})
    hits, safe = [], 0
    for r in rows:
        if satisfies(bad_version, r.get("range", "")):
            hits.append(r)
        else:
            safe += 1
    return {"exposed": hits, "shielded_by_pin": safe, "checked": len(rows)}, ms


# --------------------------------------------------------------------------
# 3. lockfile exposure
# --------------------------------------------------------------------------

LOCKFILE_PATHS = """
UNWIND $names AS mine
MATCH path = (m:Package {name: mine})-[:REQUIRES*0..%d]->(t:Package {name: $name})
RETURN mine AS entry, length(path) AS depth
ORDER BY depth ASC
LIMIT $limit
"""


def parse_lockfile(text: str) -> dict[str, str]:
    """package-lock.json v2/v3 -> {name: resolved_version}."""
    data = json.loads(text)
    out: dict[str, str] = {}
    for path, meta in (data.get("packages") or {}).items():
        if not path or not isinstance(meta, dict):
            continue
        name = meta.get("name") or path.split("node_modules/")[-1]
        if name and meta.get("version"):
            out[name] = meta["version"]
    for name, meta in (data.get("dependencies") or {}).items():
        if isinstance(meta, dict) and meta.get("version"):
            out.setdefault(name, meta["version"])
    return out


def lockfile_exposure(h: Hydra, lock_text: str, name: str, bad_version: str | None = None,
                      depth: int = 5, limit: int = 2000):
    resolved = parse_lockfile(lock_text)
    direct = None
    if name in resolved:
        v = resolved[name]
        direct = {"version": v, "malicious": bad_version is not None and v == bad_version}
    rows, ms = h.timed(LOCKFILE_PATHS % depth,
                       {"names": list(resolved.keys()), "name": name, "limit": limit})
    return {"resolved_count": len(resolved), "direct": direct, "paths": rows}, ms


# --------------------------------------------------------------------------
# 4. maintainer pivot — where the attacker goes next
# --------------------------------------------------------------------------

MAINTAINER_PIVOT = """
MATCH (compromised:Package {name: $name})-[:MAINTAINED_BY]->(m:Maintainer)
MATCH (sibling:Package)-[:MAINTAINED_BY]->(m)
WHERE sibling.name <> $name
OPTIONAL MATCH (dependent:Package)-[:REQUIRES]->(sibling)
RETURN m.name AS maintainer, sibling.name AS also_controls,
       count(DISTINCT dependent) AS direct_dependents
ORDER BY direct_dependents DESC
LIMIT $limit
"""


def maintainer_pivot(h: Hydra, name: str, limit: int = 200):
    return h.timed(MAINTAINER_PIVOT, {"name": name, "limit": limit})


# --------------------------------------------------------------------------
# 5. typosquat ring
# --------------------------------------------------------------------------

NEIGHBOR_NAMES = """
UNWIND $candidates AS cand
MATCH (p:Package {name: cand})
OPTIONAL MATCH (d:Package)-[:REQUIRES]->(p)
RETURN p.name AS name, p.latest AS latest, count(d) AS dependents
"""


def edit1(name: str) -> set[str]:
    """Deletions, transpositions, and homoglyph swaps — the three that show up
    in real npm typosquat campaigns."""
    out = set()
    for i in range(len(name)):
        out.add(name[:i] + name[i + 1:])
        if i + 1 < len(name):
            out.add(name[:i] + name[i + 1] + name[i] + name[i + 2:])
    for a, b in (("l", "1"), ("i", "1"), ("o", "0"), ("-", ""), ("rn", "m")):
        if a in name:
            out.add(name.replace(a, b, 1))
    out.discard(name)
    return {n for n in out if len(n) > 2}


def typosquat_ring(h: Hydra, name: str):
    return h.timed(NEIGHBOR_NAMES, {"candidates": sorted(edit1(name))})


# --------------------------------------------------------------------------
# semver: enough of npm's range grammar to be honest
# --------------------------------------------------------------------------

_V = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def _parse(v: str):
    m = _V.match(v.strip())
    return tuple(int(g) for g in m.groups()) if m else None


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


@lru_cache(maxsize=200_000)
def satisfies(version: str, rng: str) -> bool:
    """True if `version` satisfies npm range `rng`.

    Handles ^ ~ >= > <= < = * x || and hyphen ranges, which covers the
    overwhelming majority of real npm manifests. Anything unrecognised
    (git URLs, `file:`, `npm:` aliases, workspace protocols) returns False
    rather than guessing — under-reporting exposure is the safer error.
    """
    v = _parse(version)
    if v is None:
        return False
    rng = (rng or "").strip()
    if rng in ("", "*", "x", "latest", "next"):
        return True
    if "||" in rng:
        return any(satisfies(version, part) for part in rng.split("||"))
    if " - " in rng:
        lo, hi = rng.split(" - ", 1)
        return satisfies(version, f">={lo.strip()}") and satisfies(version, f"<={hi.strip()}")
    for comparator in rng.split():
        if not _one(v, comparator.strip()):
            return False
    return True


def _one(v, c: str) -> bool:
    if not c or c in ("*", "x"):
        return True
    if c.startswith("^"):
        b = _parse(c[1:])
        if not b:
            return False
        if b[0] > 0:
            return _cmp(v, b) >= 0 and v[0] == b[0]
        if b[1] > 0:
            return _cmp(v, b) >= 0 and v[:2] == b[:2]
        return v == b
    if c.startswith("~"):
        b = _parse(c[1:])
        return bool(b) and _cmp(v, b) >= 0 and v[:2] == b[:2]
    for op in (">=", "<=", ">", "<", "="):
        if c.startswith(op):
            b = _parse(c[len(op):])
            if not b:
                return False
            r = _cmp(v, b)
            return {">=": r >= 0, "<=": r <= 0, ">": r > 0,
                    "<": r < 0, "=": r == 0}[op]
    b = _parse(c)
    return bool(b) and v == b
