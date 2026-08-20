"""The contract every package ecosystem implements.

Adding an ecosystem should be additive, not a fork of the crawler. Everything
that differs between npm, PyPI, crates.io and Go lives behind this interface,
and everything above it — the graph writer, the traversals, the audit, the
console — is written once and never mentions an ecosystem by name.

The hard part is `satisfies`. Every ecosystem has its own version-range grammar
and they disagree in ways that silently invert an answer:

    npm        "1.2.3"   means exactly 1.2.3
    Cargo      "1.2.3"   means ^1.2.3 — caret is the default when no operator
                         is given, the opposite of npm
    PEP 440    "~=1.2.3" is compatible-release: >=1.2.3, ==1.2.*
               npm's "~1.2.3" is >=1.2.3 <1.3.0 — similar, not identical, and
               "~=1.2" means >=1.2, ==1.* which has no npm equivalent at all
    Go         does not resolve ranges at all — Minimum Version Selection picks
               the highest version any dependency asks for

So each adapter implements its own, with its own tests. A wrong `satisfies()`
does not fail loudly; it produces a confident wrong number, which is worse than
returning nothing.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class ParsedPkg:
    """One package, normalised. Whatever the registry called these, by the time
    they reach the graph writer they look the same."""

    ecosystem: str
    name: str
    latest: str = ""
    versions_seen: int = 0
    # {version, published_at, deprecated}
    releases: list[dict] = field(default_factory=list)
    # {version, dep, range, kind} — one row per declared dependency
    deps: list[dict] = field(default_factory=list)
    # normalised identities: an email where one exists, else a username
    maintainers: list[str] = field(default_factory=list)
    # dependency names worth crawling next
    frontier: set[str] = field(default_factory=set)
    # anything the adapter wants to keep that has no general meaning
    extra: dict = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return f"{self.ecosystem}:{self.name}"


class Adapter(ABC):
    """One registry. Stateless except for an HTTP session."""

    #: short, lowercase, used in ids and on the wire — "npm", "pypi", "crates"
    name: str = ""
    #: the string OSV.dev expects — "npm", "PyPI", "crates.io", "Go", "Maven"
    osv_ecosystem: str = ""
    #: how a lockfile from this ecosystem is usually called
    lockfiles: tuple[str, ...] = ()
    #: shown in the UI badge
    label: str = ""
    #: registries publish crawler policies; identify yourself and mean it
    user_agent: str = ("blast-radius/1.0 (supply-chain incident response; "
                       "+https://github.com/Rohit-ATS/blast-radius)")

    @abstractmethod
    def fetch_package(self, name: str) -> dict | None:
        """The raw registry document, or None if the registry has no such name."""

    @abstractmethod
    def parse(self, doc: dict, max_versions: int = 5) -> ParsedPkg | None:
        """Reduce a registry document to the rows the graph writer needs."""

    @abstractmethod
    def satisfies(self, version: str, spec: str) -> bool:
        """Does `version` fall inside `spec`, by *this* ecosystem's rules?

        Unparseable input must return False rather than guess. Under-reporting
        exposure is recoverable; over-reporting it destroys trust in every other
        number the tool prints.
        """

    def changes_feed(self) -> Iterator[str] | None:
        """Names published recently, newest first, or None if unsupported.

        Implementations must be non-blocking enough to poll: return what is
        available now and let the caller decide when to ask again.
        """
        return None

    # ------------------------------------------------------------------
    # shared helpers — override only where an ecosystem genuinely differs
    # ------------------------------------------------------------------

    def normalise_name(self, name: str) -> str:
        """Canonical form for id derivation. PyPI folds case and punctuation;
        npm and Cargo do not."""
        return name.strip()

    def normalise_maintainer(self, identity: str) -> str:
        """One human, one node — even across ecosystems.

        This is what makes the cross-ecosystem question answerable at all: the
        same person publishes under an npm username and a PyPI username, but
        usually the same email. Emails win when present; otherwise the username
        is kept and scoped, because two unrelated people can share a handle.
        """
        identity = (identity or "").strip().lower()
        if not identity:
            return ""
        if "@" in identity and "." in identity.split("@")[-1]:
            return identity                      # an email is globally unique
        return f"{self.name}/{identity}"         # a bare handle is not

    def __repr__(self) -> str:
        return f"<Adapter {self.name}>"


# --------------------------------------------------------------------------
# small shared parsing utilities
# --------------------------------------------------------------------------

_NUM = re.compile(r"\d+")


def version_key(v: str):
    """Sort versions numerically, releases ahead of prereleases.

    Deliberately loose: this only orders a list for "keep the newest N", and
    every ecosystem here has its own precedence rules that matter more in
    `satisfies` than they do in a sort.
    """
    parts = _NUM.findall(v or "")
    return ([int(p) for p in parts[:4]] + [0, 0, 0, 0])[:4], "-" not in (v or "")


def strip_env_marker(spec: str) -> str:
    """`requests (>=2.0) ; extra == "socks"` -> `requests (>=2.0)`.

    PEP 508 environment markers describe *when* a dependency applies, not which
    versions. Evaluating them properly needs a target environment we do not
    have, so they are removed and the dependency is treated as present — which
    over-reports the tree slightly and under-reports nothing.
    """
    return (spec or "").split(";", 1)[0].strip()
