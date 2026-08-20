"""crates.io — Cargo version requirements.

The trap here is the default operator, and it is the exact inverse of npm:

    npm     "1.2.3"   means exactly 1.2.3
    Cargo   "1.2.3"   means ^1.2.3  — i.e. >=1.2.3, <2.0.0

Reading a Cargo requirement with npm rules turns "any compatible 1.x" into a
single pinned version. That does not merely narrow the answer, it inverts the
would-resolve analysis: packages that really would have pulled a poisoned
release get reported as shielded by a pin they never had.

Caret width follows the leftmost non-zero component, so ^0.2.3 allows 0.2.x
only and ^0.0.3 allows nothing but 0.0.3 — a pre-1.0 crate is far more tightly
constrained than the same requirement on a 1.x crate.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterator

import requests

from .base import Adapter, ParsedPkg

API = "https://crates.io/api/v1"

_VER = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?\s*$")


@lru_cache(maxsize=100_000)
def parse_version(v: str):
    """(major, minor, patch, is_release). Prereleases sort below releases."""
    m = _VER.match(v or "")
    if not m:
        return None
    major, minor, patch = (int(g) if g else 0 for g in m.groups())
    return major, minor, patch, ("-" not in (v or ""))


def _cmp(a, b) -> int:
    return (a[:3] > b[:3]) - (a[:3] < b[:3]) or (a[3] > b[3]) - (a[3] < b[3])


def _components(operand: str) -> int:
    """How many components were actually written — caret and tilde both widen
    as you write fewer, so this cannot be inferred from the parsed tuple."""
    core = operand.split("-")[0].split("+")[0]
    return len([p for p in core.split(".") if p != ""])


def _caret_upper(base, written: int):
    """Upper bound (exclusive) for ^operand, by the leftmost non-zero rule."""
    major, minor, patch = base[:3]
    if major > 0:
        return (major + 1, 0, 0)
    if written == 1:                       # ^0  -> <1.0.0
        return (1, 0, 0)
    if minor > 0:
        return (0, minor + 1, 0)
    if written == 2:                       # ^0.0 -> <0.1.0
        return (0, 1, 0)
    return (0, 0, patch + 1)               # ^0.0.3 -> <0.0.4


def _tilde_upper(base, written: int):
    """~1.2.3 and ~1.2 both allow 1.2.x; ~1 allows all of 1.x."""
    major, minor, _ = base[:3]
    if written >= 2:
        return (major, minor + 1, 0)
    return (major + 1, 0, 0)


@lru_cache(maxsize=200_000)
def satisfies(version: str, spec: str) -> bool:
    """Does `version` satisfy a Cargo requirement? Comma clauses are ANDed."""
    v = parse_version(version)
    if v is None:
        return False
    spec = (spec or "").strip()
    if not spec or spec == "*":
        return True
    for clause in spec.split(","):
        if not _one(v, clause.strip()):
            return False
    return True


def _one(v, clause: str) -> bool:
    if not clause or clause == "*":
        return True

    if clause.startswith("^"):
        return _ranged(v, clause[1:].strip(), _caret_upper)
    if clause.startswith("~"):
        return _ranged(v, clause[1:].strip(), _tilde_upper)

    for op in (">=", "<=", "==", "=", ">", "<"):
        if clause.startswith(op):
            b = parse_version(clause[len(op):].strip())
            if b is None:
                return False
            r = _cmp(v, b)
            if op in ("=", "=="):
                # `=1.2` pins the components that were written, not the whole
                # triple: it allows every 1.2.x.
                written = _components(clause[len(op):].strip())
                return v[:written] == b[:written]
            return {">=": r >= 0, "<=": r <= 0, ">": r > 0, "<": r < 0}[op]

    if "*" in clause:                       # 1.*, 1.2.*
        prefix = [p for p in clause.split(".") if p != "*" and p != ""]
        try:
            want = tuple(int(p) for p in prefix)
        except ValueError:
            return False
        return v[:len(want)] == want

    # No operator at all — Cargo's default is caret, NOT an exact pin.
    return _ranged(v, clause, _caret_upper)


def _ranged(v, operand: str, upper_fn) -> bool:
    base = parse_version(operand)
    if base is None:
        return False
    if _cmp(v, base) < 0:
        return False
    return v[:3] < upper_fn(base, _components(operand))


# --------------------------------------------------------------------------

class CratesAdapter(Adapter):
    name = "crates"
    osv_ecosystem = "crates.io"
    label = "crates.io"
    lockfiles = ("Cargo.lock", "Cargo.toml")

    def __init__(self):
        self._session = requests.Session()
        # crates.io's crawler policy asks for a real User-Agent with a contact
        # route, and rate-limits anyone who ignores it.
        self._session.headers.update({"User-Agent": self.user_agent})

    def fetch_package(self, name: str) -> dict | None:
        try:
            r = self._session.get(f"{API}/crates/{name}", timeout=30)
            if r.status_code != 200:
                return None
            doc = r.json()
            latest = (doc.get("crate") or {}).get("max_stable_version") \
                or (doc.get("crate") or {}).get("max_version")
            if latest:
                d = self._session.get(
                    f"{API}/crates/{name}/{latest}/dependencies", timeout=30)
                if d.status_code == 200:
                    doc["_dependencies"] = d.json().get("dependencies") or []
                    doc["_dep_version"] = latest
            return doc
        except Exception:
            return None

    def parse(self, doc: dict, max_versions: int = 5) -> ParsedPkg | None:
        crate = doc.get("crate") or {}
        name = crate.get("id") or crate.get("name")
        if not name:
            return None
        latest = crate.get("max_stable_version") or crate.get("max_version") or ""
        versions = doc.get("versions") or []

        pkg = ParsedPkg(ecosystem=self.name, name=name, latest=latest,
                        versions_seen=len(versions))
        for v in versions[:max_versions] if max_versions > 0 else versions:
            pkg.releases.append({
                "version": v.get("num", ""),
                "published_at": v.get("created_at", ""),
                "deprecated": bool(v.get("yanked")),
            })

        dep_version = doc.get("_dep_version") or latest
        for d in doc.get("_dependencies") or []:
            dep = d.get("crate_id")
            if not dep:
                continue
            # dev-dependencies are not installed by consumers, so they are
            # recorded but do not become graph edges — the same rule npm's
            # peerDependencies get.
            kind = {"normal": "prod", "dev": "dev",
                    "build": "build"}.get(d.get("kind"), "prod")
            if d.get("optional"):
                kind = "optional"
            pkg.deps.append({"version": dep_version, "dep": dep,
                             "range": str(d.get("req") or "")[:120], "kind": kind})
            if kind == "prod":
                pkg.frontier.add(dep)

        # crates.io exposes owners on a separate endpoint; the crate document
        # carries none, so maintainers are filled in by the owners call when the
        # crawler asks for them rather than invented here.
        return pkg

    def owners(self, name: str) -> list[str]:
        try:
            r = self._session.get(f"{API}/crates/{name}/owners", timeout=25)
            if r.status_code != 200:
                return []
            out = []
            for o in r.json().get("users") or []:
                ident = self.normalise_maintainer(o.get("login") or "")
                if ident:
                    out.append(ident)
            return out
        except Exception:
            return []

    def satisfies(self, version: str, spec: str) -> bool:
        return satisfies(version, spec)

    def changes_feed(self) -> Iterator[str] | None:
        """crates.io has no changes feed; its summary endpoint lists what was
        published and updated most recently, which is close enough to poll."""
        try:
            r = self._session.get(f"{API}/summary", timeout=25)
            if r.status_code != 200:
                return iter(())
            d = r.json()
            names = []
            for key in ("just_updated", "new_crates"):
                for c in d.get(key) or []:
                    n = c.get("id") or c.get("name")
                    if n:
                        names.append(n)
            return iter(names)
        except Exception:
            return iter(())
