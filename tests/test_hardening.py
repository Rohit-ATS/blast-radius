"""Phase 4: the limits, and the audit that keeps them honest.

The Cypher test here is the important one. Its job is not to assert that the
code is safe today — it is to fail the moment somebody interpolates a user
string into a query, which is the change that would make it unsafe. A comment
saying "parameters only" does not survive a refactor; a test does.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Modules that talk to HydraDB.
CYPHER_MODULES = ["blast.py", "chains.py", "graphify.py", "live.py",
                  "watch.py", "server.py", "worker.py", "ingest.py"]

# Interpolations that are provably not user input, each with the reason it
# cannot be parameterised. openCypher does not allow a bound parameter in
# either position: a relationship type is part of the pattern, and a
# variable-length bound is part of the syntax.
ALLOWED_INTERPOLATIONS = {
    ("blast.py", "REACH_COUNT"),     # %d — traversal depth, an int from a bounded range
    ("blast.py", "REACH_NAMES"),     # %d — same
    ("chains.py", "NEIGHBOURS"),     # %s — relationship type from a hardcoded dict
}


# Cypher, specifically — not SQL DDL and not prose that happens to contain the
# word CREATE. A real query has a node pattern or a relationship arrow, which
# `CREATE TABLE ...` and a module docstring do not.
CYPHER_SYNTAX = re.compile(r"MATCH\s*\(|-\[:|MERGE\s*\(")


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Docstrings quote Cypher to explain it — several modules here open by
    showing the edge shape. Documentation is not an executed query, and
    flagging it trains people to ignore this test."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is None:
                continue
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def _cypher_string_literals(path: pathlib.Path):
    """Every string constant in the file that is actually a Cypher query."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docs:
                continue
            if CYPHER_SYNTAX.search(node.value):
                yield node, node.value


def test_no_user_input_is_interpolated_into_cypher():
    """An f-string or a `+` in a query is how injection arrives. The only
    interpolation allowed is `%` formatting of a value the code itself chose."""
    offenders = []

    for name in CYPHER_MODULES:
        path = ROOT / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            # f"...MATCH..." — the string is built at runtime from expressions
            if isinstance(node, ast.JoinedStr):
                rendered = "".join(
                    v.value for v in node.values if isinstance(v, ast.Constant))
                if re.search(r"\b(MATCH|MERGE|CREATE|DELETE|SET)\b", rendered):
                    # An f-string is acceptable only when every interpolated
                    # expression is an integer-typed local, which the depth
                    # loops are. Anything else is flagged for a human.
                    interpolated = [v for v in node.values
                                    if isinstance(v, ast.FormattedValue)]
                    simple_ints = all(
                        isinstance(v.value, ast.Name) and v.value.id in ("d", "k", "depth")
                        for v in interpolated)
                    if not simple_ints:
                        offenders.append(
                            f"{name}:{node.lineno} f-string Cypher with "
                            f"{len(interpolated)} interpolation(s)")

            # "MATCH ..." + something
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                for side in (node.left, node.right):
                    if (isinstance(side, ast.Constant)
                            and isinstance(side.value, str)
                            and re.search(r"\b(MATCH|MERGE|CREATE)\b", side.value)):
                        offenders.append(
                            f"{name}:{node.lineno} Cypher built with string concatenation")

    assert not offenders, (
        "user input may reach a Cypher query:\n  " + "\n  ".join(offenders))


def test_every_cypher_literal_binds_its_values():
    """A query mentioning a package name inline rather than as $param would be
    the giveaway. Every literal must use $-parameters for its values."""
    suspicious = []
    for name in CYPHER_MODULES:
        path = ROOT / name
        if not path.exists():
            continue
        for node, text in _cypher_string_literals(path):
            # A query with an {id: ...} lookup must bind it
            if "{id:" in text and "$" not in text:
                suspicious.append(f"{name}:{node.lineno}: {text[:70]}")
    assert not suspicious, "unbound values in Cypher:\n  " + "\n  ".join(suspicious)


def test_the_allowed_interpolations_are_still_the_only_ones():
    """If this fails, someone added a new `%` into a query. That may be fine —
    but it needs reading, so it fails until the list above is updated."""
    found = set()
    for name in ("blast.py", "chains.py"):
        path = ROOT / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                continue
            if not re.search(r"%[sd]", node.value.value):
                continue
            if not re.search(r"\bMATCH\b", node.value.value):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name):
                    found.add((name, t.id))

    unexpected = found - ALLOWED_INTERPOLATIONS
    assert not unexpected, (
        f"new interpolated Cypher, needs review: {sorted(unexpected)}")


# ------------------------------------------------------------------ limits

def test_the_lockfile_cap_is_ten_megabytes():
    import server
    assert server.MAX_LOCKFILE_BYTES == 10 * 1024 * 1024


def test_package_names_are_validated():
    import server
    ok = ["debug", "@11ty/eleventy-dev-server", "ua-parser-js", "a", "lodash.merge"]
    bad = ["../etc/passwd", "Debug", "has space", "semi;colon", "@/missing-scope",
           "back\\slash", "quote'name", "-leading-dash", ""]

    pattern = re.compile(server.PACKAGE_NAME_PATTERN)
    for name in ok:
        assert pattern.match(name), f"{name!r} is a valid npm name but was rejected"
    for name in bad:
        assert not pattern.match(name), f"{name!r} should not have been accepted"


def test_a_key_gets_a_ceiling_not_an_exemption():
    """The point of the change: a leaked key is more dangerous than an
    anonymous flood, because it is trusted."""
    import apimeta
    q = apimeta.KeyQuota(per_minute=3, per_day=5)

    assert all(q.check("k")[0] for _ in range(3))
    allowed, retry, scope = q.check("k")
    assert allowed is False and scope == "burst"
    assert retry > 0, "a 429 without a retry hint is not actionable"

    # a different key is unaffected — the ceiling is per key, not global
    assert q.check("other")[0] is True


def test_the_daily_cap_bounds_a_sustained_drain():
    """A per-minute limit alone still permits 24 hours of steady extraction."""
    import apimeta
    q = apimeta.KeyQuota(per_minute=1000, per_day=4)
    for _ in range(4):
        assert q.check("k")[0]
    allowed, retry, scope = q.check("k")
    assert allowed is False and scope == "daily"
    assert retry > 3000, "the daily window should reset in hours, not seconds"


def test_cache_reports_per_source_hit_rates():
    """One aggregate number hides the thing worth knowing."""
    import apimeta
    c = apimeta.Cache()
    c.cached("osv:a", 900, lambda: 1)
    c.cached("osv:a", 900, lambda: 1)
    c.cached("registry:b", 3600, lambda: 2)

    stats = c.stats()
    assert stats["by_source"]["osv"]["hits"] == 1
    assert stats["by_source"]["osv"]["hit_rate"] == 0.5
    assert stats["by_source"]["registry"]["hits"] == 0
    assert stats["ttl_seconds"]["osv"] == 900


def test_ttls_match_the_documented_policy():
    import apimeta
    assert apimeta.TTL_OSV == 900        # OSV 15 min
    assert apimeta.TTL_REGISTRY == 3600  # registry 1 h
    assert apimeta.TTL_GRAPH == 60       # graph 60 s


def test_intel_uses_the_shared_cache():
    """The bug this replaced: intel kept a private dict and /api/health read a
    different object, so the reported hit rate was 0 no matter what."""
    import apimeta
    import intel
    before = apimeta.CACHE.hits + apimeta.CACHE.misses
    intel._cached("registry:doc:__probe__", lambda: {"x": 1}, 3600)
    intel._cached("registry:doc:__probe__", lambda: {"x": 1}, 3600)
    assert apimeta.CACHE.hits + apimeta.CACHE.misses > before
