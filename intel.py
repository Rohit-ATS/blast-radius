"""Live package intelligence: is this package real, current, and compromised?

The graph answers *who is exposed*. It cannot answer *whether anything is
actually malicious*, and it only knows the 27k packages the crawler reached.
This module closes both gaps against live sources, at request time:

  npm registry   does this package exist, what is its latest version, who
                 maintains it, when was each version published
  OSV.dev        Google's Open Source Vulnerabilities database — the canonical
                 feed for npm advisories, including `MAL-` identifiers for
                 *confirmed malicious packages*, not just CVEs. Free, no key.
  heuristics     the signals that precede an advisory being published: install
                 scripts, a version shipped after long dormancy, a maintainer
                 set that just changed

Nothing here is a heuristic pretending to be a fact. Every finding carries its
source, and an advisory from OSV is labelled differently from a smell we
noticed ourselves.
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

import apimeta

REGISTRY = "https://registry.npmjs.org"
OSV_API = "https://api.osv.dev/v1"
UA = {"User-Agent": "blast-radius-hackhydra/0.1 (+supply-chain incident response)"}
SLIM = {**UA, "Accept": "application/vnd.npm.install-v1+json"}

# Live lookups are on the request path, so they are cached. This used to be a
# private dict here with one flat TTL and no counters, which meant two things:
# an advisory and a registry document expired at the same rate despite changing
# at wildly different ones, and /api/health reported a hit rate of 0 forever
# because it was reading a different cache object that nothing used.
#
# Both now go through the shared instrumented cache, with the namespace in the
# key so the hit rate can be read per source.
TTL = apimeta.TTL_REGISTRY          # kept for callers that pass no ttl

_session = requests.Session()
_session.headers.update(UA)


def _cached(key: str, produce, ttl: float = TTL):
    return apimeta.CACHE.cached(key, ttl, produce)


# --------------------------------------------------------------------------
# npm registry
# --------------------------------------------------------------------------

# A packument holds every version's full metadata. For a long-lived popular
# package that is tens of megabytes of JSON, and json.loads turns it into a
# considerably larger Python object.
#
# Two things made that expensive rather than merely large. The download was
# unbounded, so one giant package could allocate hundreds of megabytes in a
# single call; and the result was cached whole for an hour, so every package
# the live feed enriched stayed resident. With the feed running, the web
# process climbed to 378MiB in bursts, and on an instance shared with the graph
# that is what got graph-node OOM-killed.
#
# So the download is capped, oversized packages fall back to the registry's
# abbreviated format, and callers that only need a summary cache the summary
# rather than the document it came from.
MAX_DOC_BYTES = 6 * 1024 * 1024
# Documents above this are used and discarded rather than cached.
CACHEABLE_DOC_BYTES = 512 * 1024
ABBREVIATED = "application/vnd.npm.install-v1+json"


def _fetch_capped(url: str, timeout: float, accept: str | None = None):
    """(body, was_too_big). Streams so an oversized doc is never materialised."""
    headers = {"Accept": accept} if accept else None
    with _session.get(url, timeout=timeout, stream=True, headers=headers) as r:
        if r.status_code != 200:
            return None, False
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            size += len(chunk)
            if size > MAX_DOC_BYTES:
                return None, True
            chunks.append(chunk)
    return b"".join(chunks), False


def npm_doc(name: str, timeout: float = 12.0) -> dict | None:
    """The full registry document, or None if npm does not have this name."""
    def fetch():
        url = f"{REGISTRY}/{quote(name, safe='@')}"
        try:
            body, too_big = _fetch_capped(url, timeout)
            if too_big:
                body, _ = _fetch_capped(url, timeout, accept=ABBREVIATED)
            if not body:
                return None
            doc = json.loads(body)
            # Marked so the cache can decline to keep it. The cache bounds
            # itself by entry count, which is the wrong unit here: 4,000 entries
            # is a sensible ceiling for advisories and summaries and an unbounded
            # one for documents that are individually megabytes.
            if len(body) > CACHEABLE_DOC_BYTES:
                doc = dict(doc)
                doc["_oversized"] = True
            return doc
        except Exception:
            return None

    doc = _cached(f"registry:doc:{name}", fetch, apimeta.TTL_REGISTRY)
    if isinstance(doc, dict) and doc.get("_oversized"):
        # Kept out of the cache so a run of large packages cannot accumulate.
        # Re-fetching one of these is far cheaper than holding all of them.
        apimeta.CACHE.drop(f"registry:doc:{name}")
    return doc


def npm_summary(name: str) -> dict | None:
    """What the console needs about a package that may not be in the graph.

    Cached on its own key. It is a few hundred bytes, where the document behind
    it can be megabytes, and the live feed calls this for every publish it puts
    on screen — caching the document instead meant the ticker's memory grew with
    the number of distinct packages npm happened to publish that hour.
    """
    return _cached(f"registry:summary:{name}", lambda: _npm_summary(name),
                   apimeta.TTL_REGISTRY)


def _npm_summary(name: str) -> dict | None:
    doc = npm_doc(name)
    if not doc or not doc.get("name"):
        return None
    versions = doc.get("versions") or {}
    times = doc.get("time") or {}
    latest = (doc.get("dist-tags") or {}).get("latest", "")
    meta = versions.get(latest) or {}
    return {
        "name": doc["name"],
        "latest": latest,
        "published": times.get(latest, ""),
        "modified": times.get("modified", ""),
        "versions": len(versions),
        "maintainers": [m.get("name") for m in (doc.get("maintainers") or [])
                        if m.get("name")],
        "deprecated": bool(meta.get("deprecated")),
        "description": (doc.get("description") or "")[:280],
        "repository": ((doc.get("repository") or {}) or {}).get("url", "")
        if isinstance(doc.get("repository"), dict) else "",
        "license": meta.get("license", "") if isinstance(meta.get("license"), str) else "",
        "dependencies": meta.get("dependencies") or {},
    }


def npm_dependencies(name: str, version: str | None = None) -> dict[str, str]:
    """Declared runtime dependencies of one version (default: latest)."""
    doc = npm_doc(name)
    if not doc:
        return {}
    versions = doc.get("versions") or {}
    v = version or (doc.get("dist-tags") or {}).get("latest", "")
    return (versions.get(v) or {}).get("dependencies") or {}


# --------------------------------------------------------------------------
# OSV — the authoritative part
# --------------------------------------------------------------------------

# CWE-506 is "Embedded Malicious Code" — the canonical classification for a
# package that was deliberately poisoned rather than merely defective. Both the
# September 2025 debug takeover and the 2018 event-stream backdoor carry it.
_MALWARE_CWE = "CWE-506"


def _classify(vuln: dict) -> str:
    """`malware` when the advisory says a package was *deliberately* poisoned,
    `vulnerability` when it is an ordinary defect. One means somebody attacked
    you; the other means somebody made a mistake.

    This reads structured fields only, never prose. An earlier version scanned
    the advisory text for words like "malicious" and confidently labelled
    `express` as malware, because ordinary vulnerability write-ups say
    "an attacker can inject malicious content" all the time. Getting this wrong
    in the alarming direction is worse than not classifying at all.
    """
    vid = vuln.get("id", "")
    ds = vuln.get("database_specific") or {}
    if vid.startswith("MAL-"):
        return "malware"
    if "malicious-packages-origins" in ds:
        return "malware"
    if _MALWARE_CWE in (ds.get("cwe_ids") or []):
        return "malware"
    return "vulnerability"


def _severity(vuln: dict) -> str:
    for s in vuln.get("severity") or []:
        if s.get("type") in ("CVSS_V3", "CVSS_V4") and s.get("score"):
            return str(s["score"])
    db = (vuln.get("database_specific") or {}).get("severity")
    return str(db) if db else ""


def osv_query(name: str, version: str | None = None, timeout: float = 15.0,
              ecosystem: str = "npm") -> dict:
    """Advisories affecting this package (optionally this exact version).

    Without a version OSV returns everything ever filed against the package;
    with one it returns only what actually affects that release, which is the
    difference between alarming and true.

    `ecosystem` is OSV's spelling — "npm", "PyPI", "crates.io", "Go", "Maven" —
    and it is part of the identity, not a filter: PyPI's `requests` and npm's
    `requests` are unrelated packages, and asking the wrong one produces a
    confident advisory for software the project does not have installed.
    """
    key = f"osv:{ecosystem}:{name}@{version or '*'}"

    def fetch():
        body: dict = {"package": {"name": name, "ecosystem": ecosystem}}
        if version:
            body["version"] = version
        try:
            r = _session.post(f"{OSV_API}/query", json=body, timeout=timeout)
            if r.status_code != 200:
                return {"ok": False, "error": f"osv http {r.status_code}", "vulns": []}
            raw = r.json().get("vulns") or []
        except Exception as e:
            return {"ok": False, "error": f"{e.__class__.__name__}", "vulns": []}

        vulns = [{
            "id": v.get("id", ""),
            "kind": _classify(v),
            "summary": (v.get("summary") or "").strip()[:200],
            "severity": _severity(v),
            "published": v.get("published", ""),
            "aliases": v.get("aliases") or [],
            "url": f"https://osv.dev/vulnerability/{v.get('id', '')}",
        } for v in raw]
        vulns.sort(key=lambda v: (v["kind"] != "malware", v["id"]))
        return {"ok": True, "vulns": vulns}

    return _cached(key, fetch, apimeta.TTL_OSV)   # key already starts "osv:"


def osv_batch(packages: list[tuple[str, str]], timeout: float = 25.0) -> dict:
    """One request for many (name, version) pairs — used for a whole lockfile.

    The batch endpoint returns ids only, so anything that comes back has to be
    re-queried for detail. That is still far cheaper than one query per package
    for a 2,000-entry lockfile.
    """
    if not packages:
        return {"ok": True, "hits": {}}
    queries = [{"package": {"name": n, "ecosystem": "npm"}, "version": v}
               for n, v in packages]
    try:
        r = _session.post(f"{OSV_API}/querybatch", json={"queries": queries},
                          timeout=timeout)
        if r.status_code != 200:
            return {"ok": False, "error": f"osv http {r.status_code}", "hits": {}}
        results = r.json().get("results") or []
    except Exception as e:
        return {"ok": False, "error": e.__class__.__name__, "hits": {}}

    hits: dict[str, list[str]] = {}
    for (name, version), res in zip(packages, results):
        ids = [v.get("id", "") for v in (res.get("vulns") or [])]
        if ids:
            hits[f"{name}@{version}"] = ids
    return {"ok": True, "hits": hits}


# --------------------------------------------------------------------------
# heuristics — signals that arrive before an advisory does
# --------------------------------------------------------------------------

_SENSITIVE = ("preinstall", "install", "postinstall")


def heuristics(name: str, version: str | None = None) -> list[dict]:
    """Smells, labelled as smells.

    An advisory is a fact. These are not — they are the properties that the
    published post-mortems of real npm attacks keep having in common, and they
    are reported as `signal`, never as `finding`.
    """
    doc = npm_doc(name)
    if not doc:
        return []
    versions = doc.get("versions") or {}
    times = doc.get("time") or {}
    v = version or (doc.get("dist-tags") or {}).get("latest", "")
    meta = versions.get(v) or {}
    out: list[dict] = []

    scripts = meta.get("scripts") or {}
    lifecycle = [s for s in _SENSITIVE if scripts.get(s)]
    if lifecycle:
        out.append({
            "signal": "install_scripts",
            "detail": f"runs {', '.join(lifecycle)} on install",
            "why": "arbitrary code executes at `npm install` time, before any "
                   "of your own code runs — the standard delivery mechanism",
        })

    # A long-dormant package that suddenly ships is the shape of a hijack.
    published = sorted((t for k, t in times.items()
                        if k not in ("created", "modified")), reverse=True)
    if v and times.get(v) and len(published) > 1:
        try:
            this = time.strptime(times[v][:19], "%Y-%m-%dT%H:%M:%S")
            prev = [p for p in published if p < times[v]]
            if prev:
                before = time.strptime(prev[0][:19], "%Y-%m-%dT%H:%M:%S")
                gap_days = (time.mktime(this) - time.mktime(before)) / 86400
                if gap_days > 365:
                    out.append({
                        "signal": "dormant_then_published",
                        "detail": f"{gap_days:.0f} days between the previous "
                                  f"release and {v}",
                        "why": "abandoned packages are attractive takeover "
                               "targets; a sudden release after silence is worth "
                               "a look",
                    })
        except Exception:
            pass

    if meta.get("deprecated"):
        out.append({"signal": "deprecated",
                    "detail": str(meta["deprecated"])[:160],
                    "why": "the maintainer has flagged this release; npm marks "
                           "packages it removes for squatting as 0.0.1-security"})

    if v == "0.0.1-security":
        out.append({"signal": "npm_tombstone",
                    "detail": "0.0.1-security is npm's placeholder for a name "
                              "it removed",
                    "why": "somebody squatted this name and npm took it down"})

    maintainers = [m.get("name") for m in (doc.get("maintainers") or [])]
    if len(maintainers) == 1:
        out.append({"signal": "single_maintainer",
                    "detail": f"published solely by {maintainers[0]}",
                    "why": "one compromised account is enough; the September "
                           "2025 chalk/debug attack began with a single "
                           "maintainer's credentials"})
    return out


# --------------------------------------------------------------------------
# the combined verdict
# --------------------------------------------------------------------------

def audit_tree(resolved: dict[str, str], max_detail: int = 60,
               chunk: int = 900) -> dict:
    """Scan an entire resolved dependency tree against OSV, live.

    This is the part that needs no graph at all: the lockfile *is* the user's
    tree, already resolved to exact versions, so every entry can be checked
    against the advisory database directly. It works for any project on earth,
    including packages our crawl has never heard of.

    The batch endpoint returns ids only, so the worst offenders are re-queried
    for detail — capped, because a large monorepo can have hundreds of hits and
    nobody reads hundreds of advisories.
    """
    t0 = time.perf_counter()
    pairs = [(n, v) for n, v in resolved.items() if n and v]
    hits: dict[str, list[str]] = {}
    errors = []
    for i in range(0, len(pairs), chunk):
        batch = osv_batch(pairs[i:i + chunk])
        if batch.get("ok"):
            hits.update(batch["hits"])
        else:
            errors.append(batch.get("error", "osv batch failed"))

    # Detail only for as many as anyone will actually read, worst first: a
    # package with more advisories is more likely to be the one that matters.
    ranked = sorted(hits, key=lambda k: -len(hits[k]))[:max_detail]
    findings = []
    if ranked:
        with ThreadPoolExecutor(max_workers=8) as pool:
            def detail(key):
                name, _, version = key.rpartition("@")
                return key, osv_query(name, version)
            for key, res in pool.map(detail, ranked):
                name, _, version = key.rpartition("@")
                vulns = res.get("vulns", [])
                findings.append({
                    "name": name,
                    "version": version,
                    "malware": [v for v in vulns if v["kind"] == "malware"],
                    "vulnerabilities": [v for v in vulns if v["kind"] != "malware"],
                    "advisory_ids": hits[key],
                })

    findings.sort(key=lambda f: (-len(f["malware"]), -len(f["vulnerabilities"]),
                                 f["name"]))
    malicious = [f for f in findings if f["malware"]]
    return {
        "scanned": len(pairs),
        "flagged": len(hits),
        "malicious_count": len(malicious),
        "vulnerable_count": len(hits) - len(malicious),
        "detailed": len(findings),
        "truncated": len(hits) > len(findings),
        "findings": findings,
        "errors": errors,
        "source": "osv.dev",
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _parse_semver(v: str):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(g) for g in m.groups()) if m else None


def safe_versions(name: str, bad_version: str, limit: int = 3) -> list[str]:
    """The nearest releases *above* the bad one that carry no advisory.

    Upgrading is the fix, but "upgrade to latest" is bad advice during an
    incident — latest may be several majors away and break the build. These are
    checked against OSV individually, cheapest-upgrade first, so the
    recommendation is the smallest safe step rather than the biggest.
    """
    doc = npm_doc(name)
    if not doc:
        return []
    bad = _parse_semver(bad_version)
    candidates = []
    for v in (doc.get("versions") or {}):
        parsed = _parse_semver(v)
        if not parsed or "-" in v:            # skip prereleases
            continue
        if bad is None or parsed > bad:
            candidates.append((parsed, v))
    candidates.sort()

    out = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        # Check a window of the nearest upgrades rather than every release.
        window = [v for _, v in candidates[:40]]
        for v, res in zip(window, pool.map(lambda x: osv_query(name, x), window)):
            if not res.get("vulns"):
                out.append(v)
                if len(out) >= limit:
                    break
    return out


def remediation(name: str, bad_version: str, dependents: list[str] | None = None,
                resolved: dict[str, str] | None = None) -> dict:
    """Everything needed to actually fix it — as data, and as a prompt.

    Two audiences. A human wants the exact commands and the `overrides` block
    to paste into package.json. An AI coding agent (Cursor, Claude Code, Codex)
    wants the full situation in one self-contained brief so it can edit the
    repo without guessing. Both are produced from the same facts, and neither
    invents a version number that was not checked against OSV.
    """
    t0 = time.perf_counter()
    assessment = assess(name, bad_version)
    fixes = safe_versions(name, bad_version)
    target = fixes[0] if fixes else None
    dependents = dependents or []

    direct = bool(resolved and name in resolved)
    advisories = assessment.get("advisories", [])
    malware = [a for a in advisories if a["kind"] == "malware"]

    commands = []
    if target:
        if direct:
            commands.append(f"npm install {name}@{target}")
        commands.append("npm install   # after adding the overrides below")
    commands.append(f"npm ls {name}    # confirm no {bad_version} remains")

    # npm's `overrides` forces a version everywhere in the tree, including deep
    # transitive copies you do not control. That is the only lever that works
    # when the compromised package is not your direct dependency.
    overrides = {"overrides": {name: target}} if target else {}

    prompt = _fix_prompt(name, bad_version, target, assessment, dependents,
                         direct, malware)
    return {
        "package": name,
        "bad_version": bad_version,
        "verdict": assessment.get("verdict"),
        "is_direct_dependency": direct,
        "safe_versions": fixes,
        "recommended": target,
        "latest": assessment.get("package", {}).get("latest"),
        "advisories": advisories,
        "affected_dependents": dependents[:40],
        "affected_count": len(dependents),
        "commands": commands,
        "package_json_overrides": overrides,
        "ai_prompt": prompt,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _fix_prompt(name, bad_version, target, assessment, dependents, direct,
                malware) -> str:
    """A self-contained brief for a coding agent. No links to follow, no
    context the agent has to go and find — everything it needs is inline."""
    lines = [
        f"You are fixing a live npm supply-chain incident in this repository.",
        "",
        f"COMPROMISED PACKAGE: {name}@{bad_version}",
    ]
    if malware:
        lines.append("STATUS: confirmed malicious — this is not a routine CVE.")
        for a in malware[:3]:
            lines.append(f"  - {a['id']}: {a['summary']}")
            lines.append(f"    {a['url']}")
    elif assessment.get("advisories"):
        lines.append("STATUS: known vulnerable.")
        for a in assessment["advisories"][:3]:
            lines.append(f"  - {a['id']}: {a['summary']}")
    lines += [
        "",
        f"SAFE VERSION: {target if target else 'none found above the bad version — see below'}",
        f"LATEST PUBLISHED: {assessment.get('package', {}).get('latest', 'unknown')}",
        "",
        "HOW IT REACHES THIS PROJECT:",
    ]
    if direct:
        lines.append(f"  {name} is a DIRECT dependency in package.json.")
    elif dependents:
        # Without a lockfile these are ecosystem-wide dependents from the
        # graph, not this repo's. Saying "it is pulled in by X" when X is not
        # in the repo sends an agent hunting for a package that is not there.
        lines.append(f"  {name} was not found in package.json, so it is most "
                     f"likely transitive. Run `npm ls {name}` to see the exact "
                     f"chain in this repo.")
        lines.append(f"  For context, {len(dependents):,} packages in the "
                     f"crawled npm ecosystem depend on it, including: "
                     f"{', '.join(dependents[:6])}.")
    else:
        lines.append(f"  {name} was not found in package.json. Run "
                     f"`npm ls {name}` to find the chain that pulls it in.")
    lines += [
        "",
        "WHAT TO DO:",
    ]
    if target and direct:
        lines.append(f"  1. Update {name} to ^{target} in package.json dependencies.")
    if target:
        lines.append(f"  {'2' if direct else '1'}. Add an npm `overrides` block so every "
                     f"transitive copy is forced to the safe version:")
        lines.append(f'       "overrides": {{ "{name}": "{target}" }}')
    lines += [
        f"  {'3' if target and direct else '2'}. Run `npm install` and then "
        f"`npm ls {name}` and confirm no copy of {bad_version} remains anywhere.",
        f"  {'4' if target and direct else '3'}. If any build breaks, report which "
        f"package required {name}@{bad_version} rather than reverting the pin.",
        "",
        "CONSTRAINTS:",
        "  - Do not downgrade any other package to satisfy the constraint.",
        f"  - Do not remove {name}; pin it. Removing it will break the packages above.",
        "  - Do not edit the lockfile by hand; let npm regenerate it.",
    ]
    if malware:
        lines += [
            "",
            "AFTER THE FIX — this package executed attacker code during install,",
            "so treat the machine and CI as having run untrusted code:",
            "  - rotate any npm, CI and cloud tokens that were readable from this repo",
            "  - check CI logs around the install for unexpected network calls",
        ]
    return "\n".join(lines)


def assess(name: str, version: str | None = None) -> dict:
    """Everything known about one package, from live sources, in one shot.

    When no version is given this resolves the latest one first and asks about
    *that*, rather than asking OSV about the package in general. Those are very
    different questions: a version-less query returns every advisory ever filed
    against the package across its whole history, so `express` comes back
    looking compromised when the current release is clean.
    """
    t0 = time.perf_counter()
    summary = npm_summary(name)
    if summary is not None and not version:
        version = summary["latest"] or None

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_osv = pool.submit(osv_query, name, version)
        f_heur = pool.submit(heuristics, name, version)
        osv, signals = f_osv.result(), f_heur.result()

    if summary is None:
        return {
            "name": name, "exists": False,
            "verdict": "unknown_package",
            "message": f"npm has no package called '{name}'.",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    vulns = osv.get("vulns", [])
    malware = [v for v in vulns if v["kind"] == "malware"]
    if malware:
        verdict = "malicious"
    elif vulns:
        verdict = "vulnerable"
    elif signals:
        verdict = "watch"
    else:
        verdict = "clean"

    checked = version or summary["latest"]
    return {
        "name": summary["name"],
        "exists": True,
        "checked_version": checked,
        "is_latest": checked == summary["latest"],
        "verdict": verdict,
        "package": summary,
        "advisories": vulns,
        "malware_count": len(malware),
        "advisory_count": len(vulns),
        "signals": signals,
        "sources": {
            "registry": "registry.npmjs.org",
            "advisories": "osv.dev" if osv.get("ok") else f"osv unavailable: {osv.get('error')}",
        },
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
