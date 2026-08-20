"""PyPI — PEP 440 versions, PEP 508 dependency specifiers.

The trap here is `~=`. It looks like npm's `~` and is not the same operator:

    npm      ~1.2.3   >=1.2.3 <1.3.0
    PEP 440  ~=1.2.3  >=1.2.3, ==1.2.*        (same range, different derivation)
    PEP 440  ~=1.2    >=1.2,   ==1.*          (npm has no equivalent at all)

`~=` drops the last component of the operand and pins everything above it, so
the width of the range depends on how many components you wrote. Treating it as
npm's tilde silently narrows `~=1.2` from "any 1.x" to "1.2.x only", which turns
a real exposure into a clean bill of health.

PEP 440 versions are also richer than semver: an epoch (`1!2.0`), arbitrary-length
release tuples (`1.2.3.4`), and pre/post/dev segments that order
dev < pre < release < post.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterator

import requests

from .base import Adapter, ParsedPkg, strip_env_marker

REGISTRY = "https://pypi.org/pypi"
RSS_UPDATES = "https://pypi.org/rss/updates.xml"

# epoch! release [pre] [post] [dev] [+local]
_VER = re.compile(
    r"^\s*v?"
    r"(?:(?P<epoch>\d+)!)?"
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?P<pre>[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?"
    r"(?P<post>[-_.]?(?:post|rev|r)[-_.]?\d*|-\d+)?"
    r"(?P<dev>[-_.]?dev[-_.]?\d*)?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?"
    r"\s*$", re.IGNORECASE)

_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1,
             "c": 2, "rc": 2, "pre": 2, "preview": 2}


def _num(chunk: str, default: int = 0) -> int:
    m = re.search(r"\d+", chunk or "")
    return int(m.group()) if m else default


@lru_cache(maxsize=100_000)
def parse_version(v: str):
    """PEP 440 version -> a tuple that sorts correctly, or None.

    Ordering within a release: dev < pre < (nothing) < post. Encoded as a rank
    so plain tuple comparison gets it right.
    """
    if not v:
        return None
    m = _VER.match(v)
    if not m:
        return None
    epoch = int(m.group("epoch") or 0)
    release = tuple(int(p) for p in m.group("release").split("."))

    pre, post, dev = m.group("pre"), m.group("post"), m.group("dev")
    if dev:
        stage, ordinal = 0, _num(dev)
    elif pre:
        letters = re.sub(r"[^a-zA-Z]", "", pre).lower()
        stage, ordinal = 1 + _PRE_RANK.get(letters, 2) / 10.0, _num(pre)
    elif post:
        stage, ordinal = 3, _num(post)
    else:
        stage, ordinal = 2, 0
    return epoch, release, stage, ordinal


def _pad(a: tuple, b: tuple):
    """Compare release tuples of different lengths: 1.2 == 1.2.0."""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def _cmp(va, vb) -> int:
    if va[0] != vb[0]:
        return (va[0] > vb[0]) - (va[0] < vb[0])
    ra, rb = _pad(va[1], vb[1])
    if ra != rb:
        return (ra > rb) - (ra < rb)
    return ((va[2], va[3]) > (vb[2], vb[3])) - ((va[2], va[3]) < (vb[2], vb[3]))


def _wildcard_match(version: str, operand: str) -> bool:
    """`==1.2.*` — compare only the components the operand pins."""
    want = operand[:-1].rstrip(".").split(".")
    v = parse_version(version)
    if v is None:
        return False
    have = [str(p) for p in v[1]]
    if len(want) > len(have):
        return False
    return have[:len(want)] == want


def _compatible(version: str, operand: str) -> bool:
    """`~=` — compatible release.

    `~=2.2` is `>=2.2, ==2.*`; `~=1.4.5` is `>=1.4.5, ==1.4.*`. The prefix that
    stays pinned is the operand minus its final component, so the range widens
    as you write fewer components. A single component (`~=2`) is meaningless
    under PEP 440 and is rejected rather than guessed at.
    """
    parts = operand.split(".")
    if len(parts) < 2:
        return False
    v, base = parse_version(version), parse_version(operand)
    if v is None or base is None:
        return False
    if _cmp(v, base) < 0:
        return False
    prefix = parts[:-1]
    have = [str(p) for p in v[1]]
    return len(have) >= len(prefix) and have[:len(prefix)] == prefix


@lru_cache(maxsize=200_000)
def satisfies(version: str, spec: str) -> bool:
    """Does `version` satisfy a PEP 440 specifier set?

    Comma-separated clauses are ANDed. Environment markers are stripped by the
    caller, since they describe *when* a dependency applies rather than which
    versions of it.
    """
    if parse_version(version) is None:
        return False
    spec = strip_env_marker(spec).strip()
    # `requests (>=2.0,<3.0)` — the parenthesised form from requires_dist
    if spec.startswith("(") and spec.endswith(")"):
        spec = spec[1:-1].strip()
    if not spec or spec == "*":
        return True

    for clause in spec.split(","):
        if not _one(version, clause.strip()):
            return False
    return True


def _one(version: str, clause: str) -> bool:
    if not clause:
        return True
    # `===` is arbitrary equality: an opaque string compare, no version
    # semantics at all. It exists for versions that are not PEP 440 at all.
    if clause.startswith("==="):
        return version.strip() == clause[3:].strip()

    for op in ("~=", "==", "!=", ">=", "<=", ">", "<"):
        if clause.startswith(op):
            operand = clause[len(op):].strip()
            if not operand:
                return False
            if op == "~=":
                return _compatible(version, operand)
            if operand.endswith(".*"):
                hit = _wildcard_match(version, operand)
                if op == "==":
                    return hit
                if op == "!=":
                    return not hit
                return False        # a wildcard with an ordering operator is invalid
            v, b = parse_version(version), parse_version(operand)
            if v is None or b is None:
                return False
            r = _cmp(v, b)
            return {"==": r == 0, "!=": r != 0, ">=": r >= 0,
                    "<=": r <= 0, ">": r > 0, "<": r < 0}[op]

    # A bare version with no operator. PEP 440 does not define this; pip would
    # reject it. Treated as exact rather than silently accepting everything.
    b = parse_version(clause)
    v = parse_version(version)
    return bool(b) and bool(v) and _cmp(v, b) == 0


# --------------------------------------------------------------------------

_NAME = re.compile(r"^\s*([A-Za-z0-9._-]+)")
_EXTRAS = re.compile(r"\[[^\]]*\]")


def split_requirement(req: str) -> tuple[str, str]:
    """`charset_normalizer<4,>=2` -> ("charset_normalizer", "<4,>=2")."""
    req = strip_env_marker(req)
    req = _EXTRAS.sub("", req)
    m = _NAME.match(req)
    if not m:
        return "", ""
    name = m.group(1)
    rest = req[m.end():].strip()
    if rest.startswith("(") and rest.endswith(")"):
        rest = rest[1:-1].strip()
    return name, rest


class PyPIAdapter(Adapter):
    name = "pypi"
    osv_ecosystem = "PyPI"
    label = "PyPI"
    lockfiles = ("requirements.txt", "poetry.lock", "Pipfile.lock", "uv.lock",
                 "pyproject.toml")

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})

    def normalise_name(self, name: str) -> str:
        """PEP 503: names fold case and collapse runs of -_. to a single -.
        `Flask_SQLAlchemy` and `flask-sqlalchemy` are the same project."""
        return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()

    def fetch_package(self, name: str) -> dict | None:
        try:
            r = self._session.get(f"{REGISTRY}/{name}/json", timeout=30)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def parse(self, doc: dict, max_versions: int = 5) -> ParsedPkg | None:
        info = doc.get("info") or {}
        name = info.get("name")
        if not name:
            return None
        latest = info.get("version", "")
        releases = doc.get("releases") or {}

        pkg = ParsedPkg(ecosystem=self.name, name=self.normalise_name(name),
                        latest=latest, versions_seen=len(releases))
        pkg.extra["display_name"] = name

        ordered = sorted((v for v in releases if parse_version(v)),
                         key=lambda v: parse_version(v))
        keep = ordered[-max_versions:] if max_versions > 0 else ordered
        if latest and latest not in keep:
            keep.append(latest)
        for v in keep:
            files = releases.get(v) or []
            pkg.releases.append({
                "version": v,
                "published_at": (files[0].get("upload_time_iso_8601", "")
                                 if files else ""),
                "deprecated": bool(files and files[0].get("yanked")),
            })

        # PyPI only exposes requires_dist for the *current* version, so every
        # dependency row is attributed to `latest` rather than invented for
        # versions whose metadata the API does not return.
        for req in (info.get("requires_dist") or []):
            dep, spec = split_requirement(req)
            if not dep:
                continue
            dep = self.normalise_name(dep)
            kind = "extra" if ";" in req and "extra" in req else "prod"
            pkg.deps.append({"version": latest, "dep": dep,
                             "range": spec[:120], "kind": kind})
            if kind == "prod":
                pkg.frontier.add(dep)

        author = (info.get("author_email") or info.get("author")
                  or info.get("maintainer_email") or info.get("maintainer") or "")
        for identity in re.split(r"[,;]", author):
            # `Name <a@b.com>` — the address is the part worth joining on
            m = re.search(r"<([^>]+)>", identity)
            ident = self.normalise_maintainer(m.group(1) if m else identity)
            if ident:
                pkg.maintainers.append(ident)
        pkg.maintainers = sorted(set(pkg.maintainers))
        return pkg

    def satisfies(self, version: str, spec: str) -> bool:
        return satisfies(version, spec)

    def changes_feed(self) -> Iterator[str] | None:
        """PyPI publishes an RSS feed of recent updates. Parsed with the stdlib
        rather than a dependency — the shape is `<title>name version</title>`."""
        try:
            r = self._session.get(RSS_UPDATES, timeout=25)
            if r.status_code != 200:
                return iter(())
            titles = re.findall(r"<title>([^<]+)</title>", r.text)
            names = []
            for t in titles[1:]:             # first title is the channel itself
                part = t.strip().split()
                if part:
                    names.append(self.normalise_name(part[0]))
            return iter(names)
        except Exception:
            return iter(())
