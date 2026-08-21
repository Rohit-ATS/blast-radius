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
from ecosystems import crates, golang, maven, npm, pypi        # noqa: E402


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
# Maven — soft requirements and interval notation
# ==========================================================================

def test_a_bare_maven_version_is_a_recommendation_not_a_pin():
    """The fourth answer to "what does a bare version mean", and the only one
    that is not a constraint at all.

    npm pins, Cargo carets, Go floors — and Maven merely *suggests*. Maven's
    nearest-wins resolution can override a bare version with one declared
    closer to the root, so reporting `1.0` as pinned claims a guarantee Maven
    never made. Only bracket notation is binding.
    """
    assert maven.is_hard_requirement("1.0") is False
    assert maven.is_hard_requirement("[1.0]") is True
    assert maven.is_hard_requirement("[1.0,2.0)") is True
    assert maven.is_hard_requirement("(,1.0]") is True

    # the soft form admits a newer version, because resolution may well pick one
    assert maven.satisfies("2.0", "1.0") is True
    # the hard form does not
    assert maven.satisfies("2.0", "[1.0]") is False


@pytest.mark.parametrize("version,spec,expected", [
    # soft requirement — treated as a floor, never as a pin
    ("1.0", "1.0", True),
    ("2.0", "1.0", True),
    ("0.9", "1.0", False),
    # hard pin
    ("1.0", "[1.0]", True),
    ("1.0.0", "[1.0]", True),           # 1.0 == 1.0.0, trailing zeros pad
    ("1.1", "[1.0]", False),
    # half-open intervals
    ("1.5", "[1.0,2.0)", True),
    ("1.0", "[1.0,2.0)", True),         # inclusive lower
    ("2.0", "[1.0,2.0)", False),        # exclusive upper
    ("0.9", "[1.0,2.0)", False),
    ("2.0", "[1.0,2.0]", True),         # inclusive upper
    ("1.0", "(1.0,2.0)", False),        # exclusive lower
    # unbounded on one side
    ("0.9", "(,1.0]", True),
    ("1.0", "(,1.0]", True),
    ("1.1", "(,1.0]", False),
    ("9.0", "[1.5,)", True),
    ("1.4", "[1.5,)", False),
    # a union of intervals, ORed
    ("3.5", "[1.0,2.0],[3.0,4.0)", True),
    ("1.5", "[1.0,2.0],[3.0,4.0)", True),
    ("2.5", "[1.0,2.0],[3.0,4.0)", False),
    ("4.0", "[1.0,2.0],[3.0,4.0)", False),
    # qualifiers
    ("1.0-rc1", "[1.0,2.0)", False),   # a candidate precedes its release
    ("1.0", "[1.0-rc1,)", True),
    ("1.0-SNAPSHOT", "[1.0,)", False),  # a snapshot precedes its release
    # wildcards and blanks admit anything rather than raising
    ("1.0", "*", True),
    ("1.0", "", True),
])
def test_maven_ranges(version, spec, expected):
    assert maven.satisfies(version, spec) is expected


@pytest.mark.parametrize("spec", ["[", "]", "[,", "[1.0", "1.0]", "[a,b]", "[,]"])
def test_maven_malformed_spec_is_false(spec):
    assert maven.satisfies("1.0", spec) is False


@pytest.mark.parametrize("version", ["", "   ", None, "not-a-version"])
def test_maven_malformed_version_is_false(version):
    assert maven.satisfies(version, "[1.0,2.0)") is False


def test_maven_refuses_to_guess_an_unresolved_property():
    """`${jackson.version}` is not a range. Guessing here would produce a
    confident advisory match against a version nobody declared."""
    assert maven.satisfies("2.19.0", "${jackson.version}") is False
    assert maven.satisfies("2.19.0", "[${min},2.0)") is False


@pytest.mark.parametrize("higher,lower", [
    ("2.19.0", "2.19.0-rc2"),           # release beats its candidate
    ("2.19.0-rc10", "2.19.0-rc2"),      # ordinals compare as numbers, not text
    ("2.19.0-rc1", "2.19.0-milestone1"),   # alpha < beta < milestone < rc
    ("2.19.0-milestone1", "2.19.0-beta1"),
    ("2.19.0-beta1", "2.19.0-alpha1"),
    ("1.0", "1.0-SNAPSHOT"),            # a snapshot precedes its release
    ("1.0-sp1", "1.0"),                 # a service pack follows it
    ("33.4.8-jre", "33.4.8-android"),   # unknown qualifiers order lexically
    ("2.0", "1.9.9"),
])
def test_maven_version_ordering(higher, lower):
    assert maven._cmp(maven.parse_version(higher), maven.parse_version(lower)) > 0
    assert maven._cmp(maven.parse_version(lower), maven.parse_version(higher)) < 0


@pytest.mark.parametrize("a,b", [
    ("1.2", "1.2.0"),                   # missing components pad with zero
    ("1.2", "1.2.0.0"),
    ("2.0.0.Final", "2.0.0"),           # final/ga/release ARE the null qualifier
    ("1.0-GA", "1.0"),
    ("1.0-release", "1.0"),
    ("1.0-alpha1", "1.0.alpha.1"),      # '.', '-' and '_' all separate tokens
    ("1.0-beta-2", "1.0-beta2"),        # a number after a qualifier is its ordinal
])
def test_maven_versions_that_are_equal(a, b):
    assert maven._cmp(maven.parse_version(a), maven.parse_version(b)) == 0


def test_maven_latest_is_never_a_prerelease():
    """Filtering on the literal string "snapshot" is not enough — Central
    serves rc, alpha and milestone builds too, and picking one as "latest"
    reports a version nobody's build resolves to."""
    versions = ["2.18.3", "2.18.4", "2.19.0-rc1", "2.19.0-rc2", "2.19.0",
                "2.19.1-SNAPSHOT", "3.0.0-alpha1", "3.0.0-M1"]
    stable = sorted((v for v in versions if maven.is_release(v)),
                    key=maven.sort_key)
    assert stable[-1] == "2.19.0"
    for pre in ("2.19.0-rc2", "3.0.0-alpha1", "2.19.1-SNAPSHOT", "3.0.0-M1"):
        assert maven.is_release(pre) is False
    # guava's -jre/-android are classifiers, not previews
    assert maven.is_release("33.4.8-jre") is True
    assert maven.is_release("33.4.8-android") is True


def test_maven_sorting_agrees_with_comparison():
    """A regression guard. `sort_key` and `satisfies` once disagreed: plain
    tuple comparison put 2.19.0 BELOW its own release candidate, because the
    release parses to the shorter tuple."""
    versions = ["3.0.0-alpha1", "2.19.0", "2.19.0-rc2", "1.0-sp1", "1.0", "2.18.4"]
    ordered = sorted(versions, key=maven.sort_key)
    for i in range(len(ordered) - 1):
        assert maven._cmp(maven.parse_version(ordered[i]),
                          maven.parse_version(ordered[i + 1])) <= 0


def test_maven_pom_property_substitution():
    pom = """<project>
  <properties>
    <jackson.version>2.19.0</jackson.version>
    <base.version>1.4</base.version>
    <derived.version>${base.version}.2</derived.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>com.fasterxml.jackson.core</groupId>
      <artifactId>jackson-core</artifactId>
      <version>${jackson.version}</version>
    </dependency>
    <dependency>
      <groupId>org.example</groupId>
      <artifactId>derived</artifactId>
      <version>${derived.version}</version>
    </dependency>
    <dependency>
      <groupId>org.example</groupId>
      <artifactId>mystery</artifactId>
      <version>${never.defined}</version>
    </dependency>
  </dependencies>
</project>"""
    props = maven.parse_properties(pom)
    assert props["jackson.version"] == "2.19.0"

    deps = {d["dep"]: d for d in maven.parse_dependencies(pom, props)}
    assert deps["com.fasterxml.jackson.core:jackson-core"]["range"] == "2.19.0"
    # one level of indirection resolves
    assert deps["org.example:derived"]["range"] == "1.4.2"
    # an undefined property is recorded as unresolved, NOT guessed
    assert deps["org.example:mystery"]["range"] == ""
    assert deps["org.example:mystery"]["unresolved"] is True


def test_maven_dependency_management_supplies_missing_versions():
    """Real POMs omit the version and inherit it from dependencyManagement."""
    pom = """<project>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.example</groupId>
        <artifactId>managed-lib</artifactId>
        <version>3.1.4</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>org.example</groupId>
      <artifactId>managed-lib</artifactId>
    </dependency>
  </dependencies>
</project>"""
    managed = maven.parse_managed(pom, {})
    assert managed["org.example:managed-lib"] == "3.1.4"

    deps = maven.parse_dependencies(pom, {}, managed)
    # the managed block itself must not be mistaken for a declared dependency
    assert len(deps) == 1
    assert deps[0]["range"] == "3.1.4"
    assert deps[0]["unresolved"] is False


def test_maven_scope_decides_what_reaches_a_consumer():
    """Only compile and runtime propagate transitively. A test-scoped JUnit is
    not part of anyone else's blast radius."""
    pom = """<project><dependencies>
    <dependency><groupId>g</groupId><artifactId>compiled</artifactId>
      <version>1.0</version></dependency>
    <dependency><groupId>g</groupId><artifactId>runtime-only</artifactId>
      <version>1.0</version><scope>runtime</scope></dependency>
    <dependency><groupId>g</groupId><artifactId>tested</artifactId>
      <version>1.0</version><scope>test</scope></dependency>
    <dependency><groupId>g</groupId><artifactId>given</artifactId>
      <version>1.0</version><scope>provided</scope></dependency>
    <dependency><groupId>g</groupId><artifactId>maybe</artifactId>
      <version>1.0</version><optional>true</optional></dependency>
</dependencies></project>"""
    kinds = {d["dep"]: d["kind"] for d in maven.parse_dependencies(pom, {})}
    assert kinds["g:compiled"] == "prod"
    assert kinds["g:runtime-only"] == "prod"
    assert kinds["g:tested"] == "dev"
    assert kinds["g:given"] == "provided"
    assert kinds["g:maybe"] == "optional"        # optional wins over scope


def test_maven_interval_split_respects_brackets():
    """A single interval contains a comma, so splitting a union on commas
    naively produces four broken fragments instead of two intervals."""
    assert maven._split_intervals("[1.0,2.0],[3.0,4.0)") == ["[1.0,2.0]", "[3.0,4.0)"]
    assert maven._split_intervals("[1.0,2.0]") == ["[1.0,2.0]"]
    assert maven._split_intervals("(,1.0]") == ["(,1.0]"]


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
