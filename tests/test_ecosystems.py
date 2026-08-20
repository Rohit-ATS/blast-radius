"""Version-range grammars, one ecosystem at a time.

A wrong `satisfies()` does not crash — it returns a confident wrong number, and
every downstream figure inherits it. These are the cases where the ecosystems
genuinely disagree, plus the malformed input that must return False rather than
guess.

Run:  py -m pytest tests/test_ecosystems.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ecosystems                                              # noqa: E402
from ecosystems import crates, golang, npm, pypi               # noqa: E402


# ==========================================================================
# the divergences — the whole reason each adapter has its own implementation
# ==========================================================================

def test_bare_version_means_three_different_things():
    """The single most dangerous difference between these ecosystems.

    npm pins, Cargo carets, Go floors. Reading a Cargo manifest with npm rules
    reports packages as shielded by a pin they never had.
    """
    assert npm.satisfies("1.2.9", "1.2.3") is False       # exact pin
    assert crates.satisfies("1.2.9", "1.2.3") is True     # ^1.2.3
    assert golang.satisfies("1.2.9", "1.2.3") is True     # >=1.2.3 under MVS

    assert crates.satisfies("2.0.0", "1.2.3") is False    # caret still stops at major
    assert golang.satisfies("2.0.0", "1.2.3") is True     # MVS has no ceiling


def test_tilde_is_not_the_same_operator_across_ecosystems():
    """npm's ~ pins the minor. PEP 440's ~= pins one component fewer than you
    wrote, so ~=1.4 spans all of 1.x while ~1.4.0 does not."""
    assert npm.satisfies("1.5.0", "~1.4.0") is False
    assert pypi.satisfies("1.5.0", "~=1.4") is True
    assert pypi.satisfies("1.5.0", "~=1.4.0") is False    # three components: 1.4.x only
    assert pypi.satisfies("2.0.0", "~=1.4") is False


# ==========================================================================
# PyPI — PEP 440
# ==========================================================================

@pytest.mark.parametrize("version,spec,expected", [
    # compatible release: the pinned prefix is the operand minus its last part
    ("1.4.6", "~=1.4.5", True),
    ("1.5.0", "~=1.4.5", False),
    ("1.4.5", "~=1.4.5", True),
    ("1.9.9", "~=1.4", True),
    ("2.0.0", "~=1.4", False),
    ("2", "~=2", False),                 # single component is not a valid ~=
    # wildcards
    ("1.2.9", "==1.2.*", True),
    ("1.3.0", "==1.2.*", False),
    ("1.3.0", "!=1.2.*", True),
    # ordinary comparators, and compounds
    ("2.5.0", ">=2.0,<3.0", True),
    ("3.0.0", ">=2.0,<3.0", False),
    ("1.9.9", ">=2.0,<3.0", False),
    ("2.0.0", "!=2.0.0", False),
    # the parenthesised form that requires_dist actually emits
    ("2.5.0", "(>=2.0,<3.0)", True),
    # release-segment padding: 1.2 and 1.2.0 are the same version
    ("1.2", "==1.2.0", True),
    ("1.2.0", "==1.2", True),
    # pre / post / dev ordering
    ("2.0.0rc1", "<2.0.0", True),
    ("2.0.0", ">2.0.0rc1", True),
    ("1.0.post1", ">1.0", True),
    ("1.0.dev1", "<1.0", True),
    # epoch beats everything to its left
    ("1!2.0", ">3.0", True),
    # arbitrary equality is an opaque string compare
    ("1.0+ubuntu-1", "===1.0+ubuntu-1", True),
    ("1.0", "===1.0+ubuntu-1", False),
    # empty specifier means any version
    ("1.2.3", "", True),
])
def test_pypi_ranges(version, spec, expected):
    assert pypi.satisfies(version, spec) is expected


@pytest.mark.parametrize("spec", [
    "not-a-version", ">=", "~=", "===", ">>>1.0", "@#$%",
])
def test_pypi_malformed_spec_is_false(spec):
    assert pypi.satisfies("1.0.0", spec) is False


@pytest.mark.parametrize("version", ["", "abc", "1.2.3.dev.x.y", None])
def test_pypi_malformed_version_is_false(version):
    assert pypi.satisfies(version or "", ">=1.0") is False


def test_pypi_environment_markers_are_stripped():
    """A marker says *when* a dependency applies, not which versions. Without
    a target environment it cannot be evaluated, so it is removed — which
    over-reports the tree slightly and under-reports nothing."""
    assert pypi.satisfies("2.5.0", '>=2.0 ; extra == "socks"') is True
    name, spec = pypi.split_requirement('requests (>=2.0,<3.0) ; extra == "socks"')
    assert name == "requests" and spec == ">=2.0,<3.0"


def test_pypi_name_normalisation():
    """PEP 503: case folds and runs of -_. collapse to a single dash."""
    a = ecosystems.get("pypi")
    assert a.normalise_name("Flask_SQLAlchemy") == "flask-sqlalchemy"
    assert a.normalise_name("zope..interface") == "zope-interface"
    assert a.normalise_name("Requests") == "requests"


def test_pypi_requirement_splitting_handles_extras():
    assert pypi.split_requirement("celery[redis]>=5.0") == ("celery", ">=5.0")
    assert pypi.split_requirement("charset_normalizer<4,>=2") == (
        "charset_normalizer", "<4,>=2")


# ==========================================================================
# crates.io — Cargo
# ==========================================================================

@pytest.mark.parametrize("version,spec,expected", [
    # caret is the default operator, written or not
    ("1.2.9", "1.2.3", True),
    ("1.2.9", "^1.2.3", True),
    ("2.0.0", "^1.2.3", False),
    ("1.2.2", "^1.2.3", False),
    # caret width follows the leftmost non-zero component
    ("0.2.9", "^0.2.3", True),
    ("0.3.0", "^0.2.3", False),
    ("0.0.4", "^0.0.3", False),          # pre-1.0 patch is effectively pinned
    ("0.0.3", "^0.0.3", True),
    ("1.9.9", "^1", True),
    ("2.0.0", "^1", False),
    ("0.9.9", "^0", True),
    ("1.0.0", "^0", False),
    ("0.0.9", "^0.0", True),
    ("0.1.0", "^0.0", False),
    # tilde pins the minor when one is written, the major when it is not
    ("1.2.9", "~1.2.3", True),
    ("1.3.0", "~1.2.3", False),
    ("1.2.9", "~1.2", True),
    ("1.3.0", "~1.2", False),
    ("1.9.0", "~1", True),
    ("2.0.0", "~1", False),
    # explicit operators
    ("1.2.3", "=1.2.3", True),
    ("1.2.4", "=1.2.3", False),
    ("1.2.9", "=1.2", True),             # pins only what was written
    ("1.5.0", ">=1.0, <2.0", True),
    ("2.0.0", ">=1.0, <2.0", False),
    # wildcards
    ("1.9.9", "1.*", True),
    ("2.0.0", "1.*", False),
    ("1.2.9", "1.2.*", True),
    ("1.3.0", "1.2.*", False),
    ("9.9.9", "*", True),
    ("9.9.9", "", True),
])
def test_crates_ranges(version, spec, expected):
    assert crates.satisfies(version, spec) is expected


@pytest.mark.parametrize("spec", ["not-a-version", "^", "~", ">=abc", "%%%"])
def test_crates_malformed_spec_is_false(spec):
    assert crates.satisfies("1.0.0", spec) is False


def test_crates_prerelease_sorts_below_release():
    assert crates.parse_version("1.0.0-alpha") < crates.parse_version("1.0.0")


# ==========================================================================
# Go — Minimum Version Selection
# ==========================================================================

@pytest.mark.parametrize("version,requirement,expected", [
    # a requirement is a floor, not a pin
    ("v1.8.0", "v1.8.0", True),
    ("v1.9.0", "v1.8.0", True),
    ("v2.0.0", "v1.8.0", True),
    ("v1.7.9", "v1.8.0", False),
    # explicit operators are honoured when tooling writes them
    ("v1.9.0", ">=v1.8.0", True),
    ("v1.9.0", "<v1.8.0", False),
    ("v1.8.0", "==v1.8.0", True),
    ("v1.9.0", "==v1.8.0", False),
    # the v prefix is optional on either side
    ("1.8.0", "v1.8.0", True),
    ("v1.8.0", "1.8.0", True),
    # +incompatible is a tag, not part of the version
    ("v2.0.0+incompatible", "v2.0.0", True),
    # prereleases sort below the release they lead to
    ("v1.8.0-rc1", "v1.8.0", False),
    ("v1.8.0", "v1.8.0-rc1", True),
    # A pseudo-version is a pre-release of the triple it names, so it sorts
    # BELOW that release — v0.0.0-2023… does not satisfy a v0.0.0 floor.
    ("v0.0.0-20230101120000-abcdef123456", "v0.0.0", False),
    ("v0.0.0-20230101120000-abcdef123456", "v0.1.0", False),
    ("v0.0.0", "v0.0.0-20230101120000-abcdef123456", True),
    ("v9.9.9", "*", True),
    ("v9.9.9", "", True),
])
def test_go_mvs(version, requirement, expected):
    assert golang.satisfies(version, requirement) is expected


@pytest.mark.parametrize("version", ["", "abc", "v1", "latest"])
def test_go_malformed_version_is_false(version):
    assert golang.satisfies(version, "v1.0.0") is False


def test_go_pseudo_versions_order_by_timestamp():
    older = golang.parse_version("v0.0.0-20220101120000-aaaaaaaaaaaa")
    newer = golang.parse_version("v0.0.0-20230101120000-bbbbbbbbbbbb")
    assert older < newer
    # A pseudo-version sits above a plain prerelease of the same triple, and
    # below the release itself, because it names a commit on the way to it.
    assert golang.parse_version("v0.0.0-alpha") < newer
    assert newer < golang.parse_version("v0.0.0")


def test_go_module_path_escaping():
    """The proxy escapes capitals so case-insensitive filesystems cannot
    collapse two distinct modules onto one path."""
    assert golang.escape_module("github.com/Sirupsen/logrus") == \
        "github.com/!sirupsen/logrus"
    assert golang.escape_module("github.com/gorilla/mux") == "github.com/gorilla/mux"


def test_go_major_suffix_is_a_different_module():
    assert golang.base_module("github.com/foo/bar/v2") == "github.com/foo/bar"
    assert golang.base_module("github.com/foo/bar") == "github.com/foo/bar"
    a = ecosystems.get("go")
    from hydra import pkg_id
    assert pkg_id("github.com/foo/bar", a.name) != pkg_id("github.com/foo/bar/v2", a.name)


def test_go_parses_both_require_forms():
    gomod = """module example.com/thing

go 1.21

require github.com/gorilla/mux v1.8.0

require (
\tgithub.com/stretchr/testify v1.8.4
\tgolang.org/x/sys v0.15.0 // indirect
)

exclude github.com/bad/pkg v1.0.0
"""
    deps = dict(golang.GoAdapter.parse_gomod(gomod))
    assert deps["github.com/gorilla/mux"] == "v1.8.0"
    assert deps["github.com/stretchr/testify"] == "v1.8.4"
    assert deps["golang.org/x/sys"] == "v0.15.0"
    assert "github.com/bad/pkg" not in deps        # exclude is not a require


# ==========================================================================
# the adapter contract itself
# ==========================================================================

def test_every_adapter_implements_the_contract():
    for a in ecosystems.all_adapters():
        assert a.name and a.osv_ecosystem and a.label
        assert a.lockfiles
        assert callable(a.fetch_package) and callable(a.parse)
        assert a.satisfies("1.0.0", "") is True
        # unparseable input never raises, in any ecosystem
        assert a.satisfies("garbage", "also-garbage") is False


def test_unknown_ecosystem_falls_back_rather_than_raising():
    assert ecosystems.get("rubygems").name == "npm"
    assert ecosystems.known("rubygems") is False
    assert ecosystems.known("pypi") is True


def test_package_ids_never_collide_across_ecosystems():
    """`requests` is a real package on PyPI and on npm; they are not the same
    thing and must not share a vertex."""
    from hydra import pkg_id
    ids = {eco: pkg_id("requests", eco) for eco in ecosystems.names()}
    assert len(set(ids.values())) == len(ids), ids


def test_maintainer_identity_joins_on_email_across_ecosystems():
    """The cross-ecosystem question only works if one human is one node."""
    npm_a = ecosystems.get("npm").normalise_maintainer("Person@Example.COM")
    pypi_a = ecosystems.get("pypi").normalise_maintainer("person@example.com")
    assert npm_a == pypi_a == "person@example.com"

    # A bare handle is not globally unique, so it stays scoped.
    assert ecosystems.get("npm").normalise_maintainer("qix") == "npm/qix"
    assert ecosystems.get("pypi").normalise_maintainer("qix") == "pypi/qix"
