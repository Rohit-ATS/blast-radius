"""Go modules — Minimum Version Selection, which is not range resolution.

Go is the odd one out and forcing it into a semver shape would produce a
confidently wrong answer. There are no ranges in a go.mod. A module says

    require github.com/gorilla/mux v1.8.0

and that is a *floor*, not a constraint. The build then takes the maximum floor
requested by anything in the graph — Minimum Version Selection. Nobody declares
"<2.0.0"; nobody gets a surprise upgrade either.

So the exposure question changes shape. In npm or PyPI you ask "would this
declared range have admitted the poisoned version". In Go you ask "did anything
in the graph request a version at or above it", because MVS will then select at
least that. `satisfies(version, requirement)` is therefore `version >= requirement`,
and that is modelled explicitly rather than dressed up as a range check.

Two more Go-specific details that break naive parsing:

  pseudo-versions   v0.0.0-20230101120000-abcdef123456 encodes a commit, and
                    sorts by the embedded timestamp, not by the 0.0.0
  major suffixes    module paths carry /v2, /v3 for major versions past one,
                    so github.com/foo/bar and github.com/foo/bar/v2 are
                    different modules that share a name in every UI
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterator

import requests

from .base import Adapter, ParsedPkg

PROXY = "https://proxy.golang.org"
INDEX = "https://index.golang.org/index"

_VER = re.compile(
    r"^\s*v?(\d+)\.(\d+)\.(\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<meta>[0-9A-Za-z.-]+))?\s*$")

# v0.0.0-20230101120000-abcdef123456
_PSEUDO = re.compile(r"^\d{14}-[0-9a-f]{12}$")


@lru_cache(maxsize=100_000)
def parse_version(v: str):
    """(major, minor, patch, rank, ordinal) or None.

    A pseudo-version outranks a plain prerelease at the same triple because it
    is a real commit on the way to that release, and orders among its peers by
    the timestamp it carries.
    """
    if not v:
        return None
    v = v.strip()
    incompatible = v.endswith("+incompatible")
    if incompatible:
        v = v[: -len("+incompatible")]
    m = _VER.match(v)
    if not m:
        return None
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    pre = m.group("pre")
    if not pre:
        return major, minor, patch, 2, 0
    if _PSEUDO.match(pre):
        return major, minor, patch, 1, int(pre.split("-")[0])
    return major, minor, patch, 0, 0


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


@lru_cache(maxsize=200_000)
def satisfies(version: str, requirement: str) -> bool:
    """Under MVS, a requirement is a floor: anything at or above it is used.

    A comparison operator is accepted if one appears — some tooling writes them
    even though go.mod does not — but a bare version means ">=", which is the
    opposite of npm's bare version meaning "==".
    """
    v = parse_version(version)
    if v is None:
        return False
    requirement = (requirement or "").strip()
    if not requirement or requirement == "*":
        return True

    for op in (">=", "<=", "==", "=", ">", "<"):
        if requirement.startswith(op):
            b = parse_version(requirement[len(op):].strip())
            if b is None:
                return False
            r = _cmp(v, b)
            return {">=": r >= 0, "<=": r <= 0, "==": r == 0, "=": r == 0,
                    ">": r > 0, "<": r < 0}[op]

    b = parse_version(requirement)
    return bool(b) and _cmp(v, b) >= 0


def escape_module(path: str) -> str:
    """The proxy lowercases paths and escapes capitals as !x, so that
    github.com/Sirupsen/logrus and .../sirupsen/logrus stay distinct on
    case-insensitive filesystems."""
    return re.sub(r"([A-Z])", lambda m: "!" + m.group(1).lower(), path or "")


def base_module(path: str) -> str:
    """Strip a /vN major suffix for display grouping. The modules stay
    separate; this only says which family a path belongs to."""
    return re.sub(r"/v[2-9]\d*$", "", path or "")


class GoAdapter(Adapter):
    name = "go"
    osv_ecosystem = "Go"
    label = "Go"
    lockfiles = ("go.mod", "go.sum")

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})
        self._since = None

    def fetch_package(self, name: str) -> dict | None:
        """The proxy has no single document, so one is assembled: the latest
        version, the version list, and the go.mod for that version."""
        mod = escape_module(name)
        try:
            latest = self._session.get(f"{PROXY}/{mod}/@latest", timeout=30)
            if latest.status_code != 200:
                return None
            info = latest.json()
            version = info.get("Version", "")

            doc = {"module": name, "latest": version,
                   "published_at": info.get("Time", "")}

            listing = self._session.get(f"{PROXY}/{mod}/@v/list", timeout=30)
            doc["versions"] = (listing.text.split()
                               if listing.status_code == 200 else [])

            gomod = self._session.get(
                f"{PROXY}/{mod}/@v/{escape_module(version)}.mod", timeout=30)
            doc["gomod"] = gomod.text if gomod.status_code == 200 else ""
            return doc
        except Exception:
            return None

    def parse(self, doc: dict, max_versions: int = 5) -> ParsedPkg | None:
        name = doc.get("module")
        if not name:
            return None
        versions = doc.get("versions") or []
        pkg = ParsedPkg(ecosystem=self.name, name=name,
                        latest=doc.get("latest", ""),
                        versions_seen=len(versions))
        pkg.extra["base_module"] = base_module(name)

        ordered = sorted((v for v in versions if parse_version(v)),
                         key=lambda v: parse_version(v))
        keep = ordered[-max_versions:] if max_versions > 0 else ordered
        for v in keep:
            pkg.releases.append({"version": v, "published_at": "",
                                 "deprecated": False})
        if pkg.latest and not any(r["version"] == pkg.latest for r in pkg.releases):
            pkg.releases.append({"version": pkg.latest,
                                 "published_at": doc.get("published_at", ""),
                                 "deprecated": False})

        for dep, version in self.parse_gomod(doc.get("gomod", "")):
            pkg.deps.append({"version": pkg.latest, "dep": dep,
                             "range": version, "kind": "prod"})
            pkg.frontier.add(dep)

        # The proxy exposes no ownership at all. Rather than guess an owner
        # from the repository host, this stays empty and the maintainer pivot
        # simply has nothing to say about Go — which is true.
        return pkg

    @staticmethod
    def parse_gomod(text: str) -> list[tuple[str, str]]:
        """Both `require x v1` and a `require ( ... )` block, ignoring
        `// indirect` markers and replace/exclude directives."""
        out, in_block = [], False
        for raw in (text or "").splitlines():
            line = raw.split("//")[0].strip()
            if not line:
                continue
            if line.startswith("require ("):
                in_block = True
                continue
            if in_block and line == ")":
                in_block = False
                continue
            if in_block:
                parts = line.split()
                if len(parts) >= 2:
                    out.append((parts[0], parts[1]))
            elif line.startswith("require "):
                parts = line[len("require "):].split()
                if len(parts) >= 2:
                    out.append((parts[0], parts[1]))
        return out

    def satisfies(self, version: str, spec: str) -> bool:
        return satisfies(version, spec)

    def changes_feed(self) -> Iterator[str] | None:
        """index.golang.org streams newline-delimited JSON of new module
        versions since a timestamp."""
        import json as _json
        from datetime import datetime, timedelta, timezone
        try:
            if self._since is None:
                self._since = (datetime.now(timezone.utc)
                               - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = self._session.get(INDEX, timeout=30,
                                  params={"since": self._since, "limit": 200})
            if r.status_code != 200:
                return iter(())
            names, newest = [], self._since
            for line in r.text.splitlines():
                if not line.strip():
                    continue
                try:
                    row = _json.loads(line)
                except Exception:
                    continue
                if row.get("Path"):
                    names.append(row["Path"])
                if row.get("Timestamp"):
                    newest = row["Timestamp"]
            self._since = newest
            return iter(names)
        except Exception:
            return iter(())
