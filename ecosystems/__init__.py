"""Adapter registry. Everything above this layer takes an ecosystem by name."""

from .base import Adapter, ParsedPkg, strip_env_marker, version_key
from .crates import CratesAdapter
from .golang import GoAdapter
from .npm import NpmAdapter
from .pypi import PyPIAdapter

_ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    _ADAPTERS[adapter.name] = adapter
    return adapter


register(NpmAdapter())
register(PyPIAdapter())
register(CratesAdapter())
register(GoAdapter())

DEFAULT = "npm"


def get(ecosystem: str | None = None) -> Adapter:
    """The adapter for an ecosystem, defaulting to npm.

    Unknown names fall back rather than raising: an ecosystem this build does
    not ship yet should degrade to "we cannot answer that", not a 500.
    """
    return _ADAPTERS.get((ecosystem or DEFAULT).lower(), _ADAPTERS[DEFAULT])


def known(ecosystem: str | None) -> bool:
    return (ecosystem or "").lower() in _ADAPTERS


def all_adapters() -> list[Adapter]:
    return list(_ADAPTERS.values())


def names() -> list[str]:
    return sorted(_ADAPTERS)


def osv_ecosystem(ecosystem: str | None) -> str:
    return get(ecosystem).osv_ecosystem


__all__ = ["Adapter", "ParsedPkg", "register", "get", "known", "all_adapters",
           "names", "osv_ecosystem", "strip_env_marker", "version_key", "DEFAULT"]
