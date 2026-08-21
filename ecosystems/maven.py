"""Maven Central — POM parsing and interval ranges.

Two things make Maven harder than the other four, and both are handled here
rather than papered over.

**A bare version is a soft requirement.** In npm `1.2.3` pins, in Cargo it
carets, in Go it floors — and in Maven it is merely a *recommendation*. Maven's
nearest-wins resolution can and does override it with a different version
declared closer to the root of the tree. Only bracket notation is a hard
requirement:

    1.0         soft   — prefer 1.0, but resolution may pick something else
    [1.0]       hard   — exactly 1.0, resolution fails rather than substitute
    [1.0,2.0)   hard   — >=1.0, <2.0
    (,1.0]      hard   — <=1.0
    [1.5,)      hard   — >=1.5

So `satisfies()` treats a soft requirement as "this version is compatible with
that recommendation" (>=), and marks it, because reporting a soft requirement
as a pin would claim a guarantee Maven never made.

**Versions are usually not in the POM you fetched.** Real POMs write
`${jackson.version}` and inherit both the property and the dependency version
from a parent. Properties are substituted, parents are followed, and anything
still unresolved is recorded as `managed` with no version rather than guessed —
an invented version would produce a confident wrong advisory match.
"""

from __future__ import annotations

import re
from functools import cmp_to_key, lru_cache
from typing import Iterator

import requests

from .base import Adapter, ParsedPkg

SEARCH = "https://search.maven.org/solrsearch/select"
REPO = "https://repo1.maven.org/maven2"
MAX_PARENTS = 3          # deep inheritance chains exist; bound the walk

# Maven's qualifier ordering. Anything unrecognised sorts *after* a release,
# which is how Maven itself treats unknown qualifiers.
_QUALIFIER_RANK = {
    "alpha": 0, "a": 0, "beta": 1, "b": 1, "milestone": 2, "m": 2,
    "rc": 3, "cr": 3, "snapshot": 4, "": 5, "ga": 5, "final": 5,
    "release": 5, "sp": 6,
}


@lru_cache(maxsize=100_000)
def parse_version(v: str):
    """A comparable key for a Maven version string.

    Maven splits on '.' and '-' and compares token by token, numbers before
    strings. This implements enough of that to order real versions correctly
    without pretending to be the full specification.
    """
    if not v or "${" in v:
        return None
    raw = [t for t in re.split(r"[.\-_]", v.strip().lower()) if t]
    key, i = [], 0
    while i < len(raw):
        t = raw[i]
        if t.isdigit():
            key.append((1, int(t), "", 0))
            i += 1
            continue
        m = re.match(r"^([a-z]+)(\d*)$", t)
        if not m:
            # Not a version token at all. Returning None rather than filing it
            # under "unknown qualifier" is what stops a malformed range like
            # "[1.0" comparing as a real version and answering True.
            return None
        word, digits = m.group(1), m.group(2)
        rank = _QUALIFIER_RANK.get(word, 7)
        ordinal = int(digits) if digits else 0
        if not digits and i + 1 < len(raw) and raw[i + 1].isdigit():
            # `1.0-beta-2` and `1.0-beta2` are the same version in Maven, so a
            # bare number straight after a qualifier is that qualifier's
            # ordinal rather than a new component.
            ordinal = int(raw[i + 1])
            i += 1
        i += 1
        if rank == 5 and ordinal == 0:
            # "ga", "final" and "release" ARE the null qualifier, so 2.0.0.Final
            # and 2.0.0 are one version. Contributing nothing is what makes them
            # compare equal instead of merely adjacent.
            continue
        # An unknown qualifier keeps its text so it sorts lexically the way
        # Maven does — otherwise guava's 33.4.8-jre and 33.4.8-android compare
        # exactly equal and "latest" is decided by Solr's row order.
        key.append((0, rank, "" if rank < 7 else word, ordinal))
    if not any(k[0] == 1 for k in key):
        # Qualifiers alone are not a version. Maven's grammar is permissive
        # enough to "parse" the word garbage, and then garbage >= also-garbage
        # answers True — a comparison between two things that were never
        # versions.
        return None
    return tuple(key) if key else None


# What a missing trailing token is worth depends on what it is compared
# against: numeric zero next to a number (so 1.2 == 1.2.0), and the null
# qualifier next to a qualifier (so 1.0 > 1.0-rc1 but 1.0 < 1.0-sp1, because a
# release beats a candidate and a service pack beats the release).
_PAD_NUM = (1, 0, "", 0)
_PAD_QUAL = (0, 5, "", 0)


def _cmp(a, b) -> int:
    n = max(len(a), len(b))
    for i in range(n):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x is None:
            x = _PAD_NUM if y[0] == 1 else _PAD_QUAL
        elif y is None:
            y = _PAD_NUM if x[0] == 1 else _PAD_QUAL
        if x != y:
            return (x > y) - (x < y)
    return 0


def sort_key(v: str):
    """Order versions by the SAME comparison `satisfies` uses.

    Plain tuple comparison is not good enough here: `2.19.0` parses to a
    shorter tuple than `2.19.0-rc2`, so Python sorts the release *below* its
    own release candidate and "latest" comes back as an RC. Routing the sort
    through _cmp keeps one definition of ordering instead of two that disagree.
    """
    parsed = parse_version(v)
    return cmp_to_key(_cmp)(parsed if parsed is not None else ())


def is_release(v: str) -> bool:
    """Is this a stable release rather than a prerelease?

    Filtering on the literal string "snapshot" is not enough: Central serves
    2.19.0-rc2 and 3.0.0-alpha1 too, and calling one of those "latest" reports
    a version nobody's build actually resolves to. The qualifier ranks already
    encode which tokens mean prerelease, so they are reused here instead of a
    second list of magic words drifting out of step with the first.

    Unrecognised qualifiers stay releases on purpose: Guava ships 31.1-jre and
    31.1-android, and neither is a preview of anything.
    """
    key = parse_version(v)
    if not key:
        return False
    return all(kind == 1 or rank >= 5 for kind, rank, _, _o in key)


def is_hard_requirement(spec: str) -> bool:
    """Only bracket notation is a hard requirement in Maven."""
    spec = (spec or "").strip()
    return bool(spec) and spec[0] in "[("


@lru_cache(maxsize=200_000)
def satisfies(version: str, spec: str) -> bool:
    """Does `version` satisfy a Maven version requirement?

    A soft requirement (a bare version) is treated as a floor rather than a
    pin, because Maven may resolve to something else entirely and reporting it
    as pinned would claim a guarantee that does not exist.
    """
    v = parse_version(version)
    if v is None:
        return False
    spec = (spec or "").strip()
    if not spec or spec == "*" or "${" in spec:
        # An unresolved property is not a range. Refusing to guess here is what
        # stops an invented version producing a confident advisory match.
        return not spec or spec == "*"

    if not is_hard_requirement(spec):
        base = parse_version(spec)
        return bool(base) and _cmp(v, base) >= 0

    # One or more comma-separated intervals, ORed together. Splitting has to
    # respect the brackets, since a single interval contains a comma itself.
    for interval in _split_intervals(spec):
        if _in_interval(v, interval):
            return True
    return False


def _split_intervals(spec: str) -> list[str]:
    out, depth, current = [], 0, ""
    for ch in spec:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(current.strip())
            current = ""
            continue
        current += ch
    if current.strip():
        out.append(current.strip())
    return [i for i in out if i]


def _in_interval(v, interval: str) -> bool:
    interval = interval.strip()
    if not interval or interval[0] not in "[(" or interval[-1] not in "])":
        base = parse_version(interval)
        return bool(base) and _cmp(v, base) >= 0

    lower_inclusive = interval[0] == "["
    upper_inclusive = interval[-1] == "]"
    body = interval[1:-1]

    if "," not in body:                    # [1.0] — a single pinned version
        base = parse_version(body.strip())
        return bool(base) and _cmp(v, base) == 0

    lo, _, hi = body.partition(",")
    lo, hi = lo.strip(), hi.strip()
    if not lo and not hi:
        return False              # "[,]" bounds nothing; it is not "any version"

    if lo:
        base = parse_version(lo)
        if base is None:
            return False
        r = _cmp(v, base)
        if r < 0 or (r == 0 and not lower_inclusive):
            return False
    if hi:
        base = parse_version(hi)
        if base is None:
            return False
        r = _cmp(v, base)
        if r > 0 or (r == 0 and not upper_inclusive):
            return False
    return True


# --------------------------------------------------------------------------

_TAG = {}


def _text(block: str, tag: str) -> str:
    pattern = _TAG.get(tag)
    if pattern is None:
        pattern = _TAG[tag] = re.compile(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", re.S)
    m = pattern.search(block or "")
    return m.group(1).strip() if m else ""


def _section(pom: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", pom or "", re.S)
    return m.group(1) if m else ""


def parse_properties(pom: str) -> dict[str, str]:
    props = {}
    for m in re.finditer(r"<([A-Za-z0-9._-]+)>\s*([^<>]*?)\s*</\1>",
                         _section(pom, "properties")):
        props[m.group(1)] = m.group(2).strip()
    return props


def substitute(value: str, props: dict[str, str], depth: int = 0) -> str:
    """Resolve ${...} against the property map, following one level of
    indirection. Anything unresolved is left as-is so callers can see that it
    was never resolved rather than receiving an empty string."""
    if not value or "${" not in value or depth > 4:
        return value
    def repl(m):
        return props.get(m.group(1), m.group(0))
    out = re.sub(r"\$\{([^}]+)\}", repl, value)
    return substitute(out, props, depth + 1) if out != value else out


def parse_dependencies(pom: str, props: dict[str, str],
                       managed: dict[str, str] | None = None) -> list[dict]:
    """Every <dependency> outside <dependencyManagement>, version resolved
    where possible and marked `managed` where not."""
    managed = managed or {}
    body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "",
                  pom or "", flags=re.S)
    out = []
    for block in re.findall(r"<dependency>(.*?)</dependency>", body, re.S):
        group = substitute(_text(block, "groupId"), props)
        artifact = substitute(_text(block, "artifactId"), props)
        if not group or not artifact:
            continue
        coord = f"{group}:{artifact}"
        version = substitute(_text(block, "version"), props)
        if (not version or "${" in version) and coord in managed:
            version = managed[coord]
        scope = _text(block, "scope") or "compile"
        optional = _text(block, "optional") == "true"
        out.append({
            "dep": coord,
            "range": version if version and "${" not in version else "",
            "kind": "optional" if optional else _scope_kind(scope),
            "unresolved": bool(not version or "${" in version),
        })
    return out


def _scope_kind(scope: str) -> str:
    """Only compile and runtime reach a consumer's classpath transitively."""
    return {"compile": "prod", "runtime": "prod", "provided": "provided",
            "test": "dev", "system": "provided", "import": "import"}.get(
                scope, "prod")


def parse_managed(pom: str, props: dict[str, str]) -> dict[str, str]:
    section = _section(pom, "dependencyManagement")
    out = {}
    for block in re.findall(r"<dependency>(.*?)</dependency>", section, re.S):
        group = substitute(_text(block, "groupId"), props)
        artifact = substitute(_text(block, "artifactId"), props)
        version = substitute(_text(block, "version"), props)
        if group and artifact and version:
            out[f"{group}:{artifact}"] = version
    return out


class MavenAdapter(Adapter):
    name = "maven"
    osv_ecosystem = "Maven"
    label = "Maven"
    lockfiles = ("pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile")

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})
        self._last_error = ""

    def normalise_name(self, name: str) -> str:
        """Maven coordinates are groupId:artifactId and are case-sensitive."""
        return (name or "").strip()

    def _pom_url(self, group: str, artifact: str, version: str) -> str:
        return (f"{REPO}/{group.replace('.', '/')}/{artifact}/{version}/"
                f"{artifact}-{version}.pom")

    def _metadata(self, group: str, artifact: str) -> dict | None:
        """Version list from maven-metadata.xml on the repository CDN.

        This is deliberately the primary source rather than the Solr search
        endpoint at search.maven.org, which times out under load often enough
        to be unusable for live monitoring, and caps results at the rows you
        ask for. The metadata file is CDN-served and carries every version
        that was ever published.
        """
        url = (f"{REPO}/{group.replace('.', '/')}/{artifact}/maven-metadata.xml")
        try:
            r = self._session.get(url, timeout=20)
            if r.status_code != 200:
                return None
        except Exception as exc:
            self._last_error = f"metadata {type(exc).__name__}: {exc}"
            return None
        versions = re.findall(r"<version>\s*([^<]+?)\s*</version>", r.text)
        if not versions:
            return None
        return {"versions": versions,
                "declared_release": _text(r.text, "release"),
                "last_updated": _text(r.text, "lastUpdated")}

    def _search(self, group: str, artifact: str) -> dict | None:
        """Fallback for the rare artifact with no metadata file."""
        try:
            r = self._session.get(SEARCH, timeout=20, params={
                "q": f'g:"{group}" AND a:"{artifact}"',
                "core": "gav", "rows": 60, "wt": "json"})
            if r.status_code != 200:
                return None
            docs = (r.json().get("response") or {}).get("docs") or []
        except Exception as exc:
            self._last_error = f"search {type(exc).__name__}: {exc}"
            return None
        versions = [d.get("v") for d in docs if d.get("v")]
        if not versions:
            return None
        return {"versions": versions, "declared_release": "",
                "last_updated": str(docs[0].get("timestamp") or "")}

    def fetch_package(self, name: str) -> dict | None:
        """`name` is a groupId:artifactId coordinate."""
        if ":" not in name:
            return None
        group, artifact = name.split(":", 1)
        meta = self._metadata(group, artifact) or self._search(group, artifact)
        if not meta:
            return None
        try:
            versions = meta["versions"]
            release = sorted((v for v in versions if is_release(v)), key=sort_key)
            if release:
                latest = release[-1]
            else:
                # Nothing stable has shipped yet. Fall back to the newest
                # prerelease rather than to whatever order the source returned.
                parseable = sorted((v for v in versions if parse_version(v)),
                                   key=sort_key)
                latest = parseable[-1] if parseable else versions[-1]

            # <release> is what the publisher declared, and publishers do get
            # this wrong. It is only trusted when it agrees that it is stable.
            declared = meta.get("declared_release") or ""
            if declared and is_release(declared) and declared in versions:
                if _cmp(parse_version(declared), parse_version(latest)) > 0:
                    latest = declared

            doc = {"group": group, "artifact": artifact, "coordinate": name,
                   "latest": latest, "versions": versions,
                   "timestamp": meta.get("last_updated")}
            doc["pom"], doc["props"], doc["managed"] = self._resolve_pom(
                group, artifact, latest)
            return doc
        except Exception as exc:
            # Returning None silently here cost real debugging time once
            # already: a bug inside the parse looked identical to Central being
            # down. The reason is kept so the caller can tell them apart.
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None

    def _resolve_pom(self, group: str, artifact: str, version: str):
        """Fetch a POM and walk its parents for properties and managed versions.

        Real POMs put the version in a property and the property in a parent, so
        a single fetch resolves almost nothing. The walk is bounded — a cycle or
        a very deep chain degrades to unresolved rather than hanging.
        """
        try:
            r = self._session.get(self._pom_url(group, artifact, version), timeout=30)
            pom = r.text if r.status_code == 200 else ""
        except Exception:
            pom = ""
        if not pom:
            return "", {}, {}

        props = parse_properties(pom)
        managed = parse_managed(pom, props)

        current, hops = pom, 0
        while hops < MAX_PARENTS:
            parent = _section(current, "parent")
            if not parent:
                break
            pg = _text(parent, "groupId")
            pa = _text(parent, "artifactId")
            pv = _text(parent, "version")
            if not (pg and pa and pv):
                break
            try:
                pr = self._session.get(self._pom_url(pg, pa, pv), timeout=30)
                if pr.status_code != 200:
                    break
                parent_pom = pr.text
            except Exception:
                break
            # A child's own values win over anything it inherits.
            inherited = parse_properties(parent_pom)
            inherited.update(props)
            props = inherited
            inherited_managed = parse_managed(parent_pom, props)
            inherited_managed.update(managed)
            managed = inherited_managed
            current = parent_pom
            hops += 1

        return pom, props, managed

    def parse(self, doc: dict, max_versions: int = 5) -> ParsedPkg | None:
        coord = doc.get("coordinate")
        if not coord:
            return None
        versions = doc.get("versions") or []
        pkg = ParsedPkg(ecosystem=self.name, name=coord,
                        latest=doc.get("latest", ""),
                        versions_seen=len(versions))
        pkg.extra["group"] = doc.get("group")
        pkg.extra["artifact"] = doc.get("artifact")

        ordered = [v for v in versions if parse_version(v)]
        ordered.sort(key=sort_key)
        for v in (ordered[-max_versions:] if max_versions > 0 else ordered):
            pkg.releases.append({"version": v, "published_at": "",
                                 "deprecated": False})

        unresolved = 0
        for d in parse_dependencies(doc.get("pom", ""), doc.get("props") or {},
                                    doc.get("managed") or {}):
            if d["unresolved"]:
                unresolved += 1
            pkg.deps.append({"version": pkg.latest, "dep": d["dep"],
                             "range": d["range"][:120], "kind": d["kind"]})
            if d["kind"] == "prod":
                pkg.frontier.add(d["dep"])
        pkg.extra["unresolved_versions"] = unresolved

        # Maven Central exposes no maintainer identity; a POM's <developers>
        # block is free text that is frequently absent or stale. Rather than
        # invent an owner, Maven contributes no MAINTAINS edges.
        return pkg

    def satisfies(self, version: str, spec: str) -> bool:
        return satisfies(version, spec)

    def changes_feed(self) -> Iterator[str] | None:
        """Maven Central's search API can be sorted by publish timestamp, which
        is the closest thing it offers to a changes feed."""
        try:
            r = self._session.get(SEARCH, timeout=30, params={
                "q": "*:*", "rows": 60, "wt": "json", "core": "gav",
                "sort": "timestamp desc"})
            if r.status_code != 200:
                return iter(())
            docs = (r.json().get("response") or {}).get("docs") or []
            return iter([f"{d['g']}:{d['a']}" for d in docs
                         if d.get("g") and d.get("a")])
        except Exception:
            return iter(())
