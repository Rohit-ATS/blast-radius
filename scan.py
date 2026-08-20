"""Static analysis of the actual published tarball, and version-to-version diff.

An advisory tells you a package *was* malicious, after somebody found out and
filed it. The September 2025 chalk/debug takeover was live on npm for hours
before that happened. This is the part that can, in principle, see it first:
download the exact bytes npm would install, read them, and diff them against
the version before.

What it looks for is drawn from the published post-mortems of real npm
attacks. Every one of these appears in benign code too, which is why nothing
here is called a finding — they are weighted signals, and the diff is what
makes them meaningful. `eval` in a package that has always had `eval` is
noise; `eval` that appeared in the version published an hour ago, alongside a
new network call, is the thing you are looking for.

  py scan.py debug 4.4.2 --against 4.4.1
"""

import argparse
import base64
import gzip
import io
import json
import math
import re
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from intel import npm_doc

UA = {"User-Agent": "blast-radius-hackhydra/0.1"}
MAX_TARBALL = 24 * 1024 * 1024        # refuse to pull an unbounded download
MAX_FILE = 2 * 1024 * 1024            # a single source file worth reading
MAX_FILES = 1500
CODE_EXT = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx", ".json")

# (id, weight, regex, why it matters). Weight is how much this moves a verdict
# on its own; the diff multiplies it.
RULES = [
    ("install_hook", 5, re.compile(r'"(?:pre|post)?install"\s*:', re.I),
     "runs code at npm install time, before any of your own code"),
    ("child_process", 4, re.compile(r"require\(\s*['\"]child_process['\"]|from\s+['\"]child_process['\"]"),
     "spawns operating-system processes"),
    ("eval", 4, re.compile(r"\beval\s*\(|new\s+Function\s*\("),
     "executes code assembled at runtime, which defeats reading the source"),
    ("base64_blob", 4, re.compile(r"['\"][A-Za-z0-9+/]{240,}={0,2}['\"]"),
     "a large base64 literal — the usual way a payload is smuggled past review"),
    ("buffer_decode", 3, re.compile(r"Buffer\.from\s*\([^)]{0,80}['\"]base64['\"]"),
     "decodes base64 at runtime, typically paired with eval"),
    ("env_exfil", 5, re.compile(r"process\.env\b[^;\n]{0,120}(?:fetch|https?\.request|axios|XMLHttpRequest|curl)", re.I),
     "reads environment variables and sends them somewhere"),
    ("credential_paths", 5, re.compile(
        r"\.ssh/|\.aws/credentials|\.npmrc|\.docker/config|id_rsa|"
        r"(?<!process)(?<!\w)\.env['\"`\s/]"),
     "touches credential files that a package has no reason to read"),
    ("raw_ip_endpoint", 4, re.compile(r"https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"),
     "talks to a bare IP address rather than a hostname"),
    ("wallet_hooks", 5, re.compile(r"\bethereum\b|\bweb3\b|wallet|metamask|0x[a-fA-F0-9]{40}", re.I),
     "wallet or address interception, the payload in the 2025 npm attacks"),
    ("obfuscated_hex", 3, re.compile(r"(?:\\x[0-9a-fA-F]{2}){12,}"),
     "long hex-escaped string, a hallmark of obfuscated payloads"),
    ("dynamic_require", 2, re.compile(r"require\s*\(\s*[^'\")\s][^)]{0,60}\)"),
     "requires a module named at runtime"),
    ("network_client", 1, re.compile(r"require\(\s*['\"](?:https?|net|dgram|tls)['\"]"),
     "opens network connections"),
]

# What a package *can do*, which is not the same as what it intends. A
# compiler legitimately spawns processes and runs an install hook; scoring that
# as "malicious" is how a scanner loses the room. Intent is inferred from the
# diff instead — capability that appeared in *this* release.
CAPABILITY = ((25, "high"), (12, "moderate"), (5, "low"), (0, "minimal"))
DIFF_VERDICT = ((8, "new_capability"), (1, "minor_change"), (0, "no_new_capability"))


def entropy(s: str) -> float:
    """Shannon entropy per character. Minified code sits near 4.5; encrypted or
    packed payloads run higher, and plain source lower."""
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def tarball_url(name: str, version: str) -> str | None:
    doc = npm_doc(name)
    if not doc:
        return None
    meta = (doc.get("versions") or {}).get(version) or {}
    return ((meta.get("dist") or {}).get("tarball")) or None


def unpublished(name: str, version: str) -> bool:
    """Was this version published and then removed?

    npm keeps the publish timestamp in `time` after deleting the release from
    `versions`. That combination is exactly what a removed malicious version
    looks like: `debug@4.4.2` and `ua-parser-js@0.7.29` are both in `time` and
    absent from `versions` today. It is a strong signal in its own right, and
    it is also why the bytes cannot be analysed after the fact — the advisory
    outlives the artifact.
    """
    doc = npm_doc(name)
    if not doc:
        return False
    return (version in (doc.get("time") or {})
            and version not in (doc.get("versions") or {}))


def fetch_files(name: str, version: str, timeout: float = 45.0):
    """Download and unpack the published tarball into {path: text}."""
    url = tarball_url(name, version)
    if not url:
        return None, f"npm has no tarball for {name}@{version}"
    try:
        r = requests.get(url, headers=UA, timeout=timeout, stream=True)
        if r.status_code != 200:
            return None, f"tarball http {r.status_code}"
        raw = io.BytesIO()
        for chunk in r.iter_content(65536):
            raw.write(chunk)
            if raw.tell() > MAX_TARBALL:
                return None, f"tarball larger than {MAX_TARBALL // 1024 // 1024}MB"
        raw.seek(0)
    except Exception as e:
        return None, f"{e.__class__.__name__}: {e}"

    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=gzip.GzipFile(fileobj=raw), mode="r|") as tar:
            for member in tar:
                if not member.isfile() or len(files) >= MAX_FILES:
                    continue
                path = member.name.split("package/", 1)[-1]
                if not path.endswith(CODE_EXT) or member.size > MAX_FILE:
                    continue
                fh = tar.extractfile(member)
                if not fh:
                    continue
                try:
                    files[path] = fh.read().decode("utf-8", errors="replace")
                except Exception:
                    continue
    except Exception as e:
        return None, f"unpack failed: {e.__class__.__name__}"
    return files, None


def scan_files(files: dict[str, str]) -> list[dict]:
    """Apply every rule to every file, keeping one hit per rule per file."""
    hits = []
    for path, text in files.items():
        # package.json is where install hooks live; skip the code rules there
        # to avoid flagging a dependency named "eval-something".
        is_manifest = path.endswith("package.json")
        for rule_id, weight, pattern, why in RULES:
            if is_manifest != (rule_id == "install_hook"):
                continue
            m = pattern.search(text)
            if not m:
                continue
            line = text.count("\n", 0, m.start()) + 1
            snippet = text[max(0, m.start() - 40):m.start() + 90]
            hits.append({
                "rule": rule_id, "weight": weight, "why": why,
                "file": path, "line": line,
                "snippet": re.sub(r"\s+", " ", snippet).strip()[:160],
            })
    for path, text in files.items():
        if path.endswith(".json"):
            continue
        longest = max((len(l) for l in text.split("\n")), default=0)
        if longest > 5000 and entropy(text[:4000]) > 5.0:
            hits.append({
                "rule": "packed_source", "weight": 3,
                "why": "a single line thousands of characters long with high "
                       "entropy — the file is packed rather than written",
                "file": path, "line": 1,
                "snippet": f"longest line {longest} chars, entropy "
                           f"{entropy(text[:4000]):.2f}",
            })
    return hits


def capability_for(score: int) -> str:
    for threshold, label in CAPABILITY:
        if score >= threshold:
            return label
    return "minimal"


def diff_verdict_for(score: int) -> str:
    for threshold, label in DIFF_VERDICT:
        if score >= threshold:
            return label
    return "no_new_capability"


def scan(name: str, version: str, against: str | None = None) -> dict:
    """Scan one version, and optionally diff it against an earlier one.

    The diff is the point. A package that has always spawned processes is
    probably a build tool; a package that started spawning processes in the
    release published this morning is an incident.
    """
    t0 = time.perf_counter()
    if unpublished(name, version):
        return {
            "name": name, "version": version, "ok": False,
            "error": "unpublished",
            "verdict": "unpublished",
            "message": (f"{name}@{version} was published and has since been "
                        f"removed from npm. The bytes are gone, so they cannot "
                        f"be analysed — but a release that npm deleted is "
                        f"itself a strong signal, and any advisory against it "
                        f"still stands."),
            "note": ("A lockfile still pinning this version will now fail to "
                     "install, which is often how a team first notices."),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    files, err = fetch_files(name, version)
    if files is None:
        return {"name": name, "version": version, "ok": False, "error": err,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    hits = scan_files(files)
    score = sum(h["weight"] for h in hits)

    result = {
        "name": name, "version": version, "ok": True,
        "files_scanned": len(files),
        "signals": sorted(hits, key=lambda h: -h["weight"]),
        "score": score,
        "capability": capability_for(score),
        "note": ("Capability, not intent. A build tool legitimately spawns "
                 "processes and runs install hooks. Compare against the "
                 "previous release to see what this version actually added."),
        "source": "npm tarball, analysed locally",
    }

    if against:
        base_files, base_err = fetch_files(name, against)
        if base_files is None:
            result["diff_error"] = base_err
        else:
            base_hits = scan_files(base_files)
            before = {(h["rule"], h["file"]) for h in base_hits}
            introduced = [h for h in hits if (h["rule"], h["file"]) not in before]
            added_files = sorted(set(files) - set(base_files))
            changed = sorted(p for p in set(files) & set(base_files)
                             if files[p] != base_files[p])
            new_score = sum(h["weight"] for h in introduced)
            result["diff"] = {
                "against": against,
                "introduced_signals": sorted(introduced, key=lambda h: -h["weight"]),
                "introduced_score": new_score,
                "files_added": added_files[:40],
                "files_changed": changed[:40],
                "files_removed": sorted(set(base_files) - set(files))[:40],
                "verdict": diff_verdict_for(new_score),
                "reading": (f"{len(introduced)} capability signal(s) appeared in "
                            f"{version} that {against} did not have"
                            if introduced else
                            f"{version} introduces no capability that {against} "
                            f"did not already have"),
            }

    result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("version")
    p.add_argument("--against", help="earlier version to diff against")
    args = p.parse_args()
    r = scan(args.name, args.version, args.against)
    if not r.get("ok"):
        print(f"failed: {r.get('error')}", file=sys.stderr)
        return 1
    print(f"{r['name']}@{r['version']} -> capability: {r['capability']} "
          f"(score {r['score']}, {r['files_scanned']} files, {r['latency_ms']:.0f}ms)")
    print(f"  {r['note']}")
    for h in r["signals"][:12]:
        print(f"  [{h['weight']}] {h['rule']:<18} {h['file']}:{h['line']}")
        print(f"        {h['why']}")
        print(f"        {h['snippet'][:110]}")
    if r.get("diff"):
        d = r["diff"]
        print(f"\nversus {d['against']}: {d['verdict']} — {d['reading']}")
        for h in d["introduced_signals"][:10]:
            print(f"  + [{h['weight']}] {h['rule']} in {h['file']}:{h['line']}")
        if d["files_added"]:
            print(f"  files added: {', '.join(d['files_added'][:8])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
