"""npm — the original ecosystem, moved behind the adapter interface.

The semver implementation here is the one that was in blast.py, unchanged and
still verified by the same 40-odd range cases in the suite. It is re-exported
from blast for callers that predate the adapter layer.
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Iterator
from urllib.parse import quote

import requests

from .base import Adapter, ParsedPkg, version_key

REGISTRY = "https://registry.npmjs.org"
REPLICATE = "https://replicate.npmjs.com"


# --------------------------------------------------------------------------
# npm semver — moved verbatim from blast.py
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


# --------------------------------------------------------------------------

class NpmAdapter(Adapter):
    name = "npm"
    osv_ecosystem = "npm"
    label = "npm"
    lockfiles = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                 "npm-shrinkwrap.json")

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})
        self._seq = None

    def fetch_package(self, name: str) -> dict | None:
        """Full doc carries maintainers and publish times. Slim is smaller but
        drops both, and the maintainer pivot is a headline feature."""
        try:
            r = self._session.get(f"{REGISTRY}/{quote(name, safe='@')}", timeout=30)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def parse(self, doc: dict, max_versions: int = 5) -> ParsedPkg | None:
        name = doc.get("name")
        if not name:
            return None
        versions = doc.get("versions") or {}
        times = doc.get("time") or {}
        latest = (doc.get("dist-tags") or {}).get("latest", "")

        ordered = sorted(versions.keys(), key=version_key)
        keep = ordered[-max_versions:] if max_versions > 0 else ordered
        if latest and latest in versions and latest not in keep:
            keep.append(latest)

        pkg = ParsedPkg(ecosystem=self.name, name=name, latest=latest or "",
                        versions_seen=len(versions))
        for v in keep:
            meta = versions[v] or {}
            pkg.releases.append({"version": v,
                                 "published_at": times.get(v, ""),
                                 "deprecated": bool(meta.get("deprecated"))})
            for kind, field in (("prod", "dependencies"),
                                ("peer", "peerDependencies")):
                for dep, rng in (meta.get(field) or {}).items():
                    pkg.deps.append({"version": v, "dep": dep,
                                     "range": str(rng)[:120], "kind": kind})
                    pkg.frontier.add(dep)

        pkg.maintainers = [
            self.normalise_maintainer(m.get("email") or m.get("name") or "")
            for m in (doc.get("maintainers") or [])
        ]
        pkg.maintainers = [m for m in pkg.maintainers if m]
        return pkg

    def satisfies(self, version: str, spec: str) -> bool:
        return satisfies(version, spec)

    def changes_feed(self) -> Iterator[str] | None:
        """npm rejects feed=continuous (400 on every variant), so this polls
        `_changes?since=<seq>` anchored at the registry's current update_seq."""
        try:
            if self._seq is None:
                root = self._session.get(f"{REPLICATE}/", timeout=25).json()
                self._seq = root.get("update_seq")
                return iter(())
            r = self._session.get(f"{REPLICATE}/_changes", timeout=30,
                                  params={"since": self._seq, "limit": 60})
            if r.status_code != 200:
                return iter(())
            data = r.json()
            if data.get("last_seq"):
                self._seq = data["last_seq"]
            names = [ev.get("id") for ev in (data.get("results") or [])
                     if ev.get("id") and not ev.get("deleted")
                     and not str(ev["id"]).startswith("_")]
            return iter(names)
        except Exception:
            return iter(())
