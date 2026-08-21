"""Read any of the four npm-ecosystem lockfiles into {name: resolved_version}.

A tool that only understands package-lock.json is useless to most of the teams
who need it. These four formats cover essentially every JavaScript project:

  package-lock.json   npm — v1 nests, v2/v3 use a flat `packages` map
  yarn.lock (v1)      not YAML. A bespoke format that only looks like it.
  yarn.lock (berry)   YAML, with `pkg@npm:range` keys
  pnpm-lock.yaml      YAML, with `/pkg@version` or `/pkg/version` keys

Only the resolved version matters here — the ranges are somebody else's
problem by the time a lockfile exists. Every parser therefore returns the same
shape, and the format is detected from content rather than trusted from the
filename, because people rename files.
"""

import json
import re
import tomllib

try:
    import yaml
except ImportError:                                   # yaml is optional
    yaml = None


class LockfileError(ValueError):
    pass


# --------------------------------------------------------------------------

def detect(text: str, filename: str = "") -> str:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return "npm"
    low = filename.lower()
    if "pnpm" in low:
        return "pnpm"
    if "yarn" in low:
        return "yarn-berry" if "__metadata" in text else "yarn-v1"
    if "__metadata" in text:
        return "yarn-berry"
    if "lockfileVersion:" in text or "\npackages:" in text:
        return "pnpm"
    if re.search(r'^"?[^\s"]+@[^\s"]+"?:\s*$', text, re.M):
        return "yarn-v1"
    raise LockfileError("unrecognised lockfile format")


def _strip_range(spec: str) -> str:
    """`lodash@^4.17.0` -> `lodash`, `@babel/core@npm:^7` -> `@babel/core`.

    Splitting on the last `@` is what makes scoped packages work; splitting on
    the first turns every `@scope/name` into an empty string.
    """
    spec = spec.strip().strip('"')
    if spec.startswith("@"):
        at = spec.find("@", 1)
        return spec[:at] if at > 0 else spec
    at = spec.find("@")
    return spec[:at] if at > 0 else spec


# --------------------------------------------------------------------------

def parse_npm(text: str) -> dict[str, str]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise LockfileError("lockfile must be a JSON object")
    out: dict[str, str] = {}
    for path, meta in (data.get("packages") or {}).items():
        if not path or not isinstance(meta, dict):
            continue
        name = meta.get("name") or path.split("node_modules/")[-1]
        if name and meta.get("version"):
            out[name] = meta["version"]

    def walk(tree):
        for name, meta in (tree or {}).items():
            if not isinstance(meta, dict):
                continue
            if meta.get("version"):
                out.setdefault(name, meta["version"])
            walk(meta.get("dependencies") or {})

    walk(data.get("dependencies") or {})
    return out


def parse_yarn_v1(text: str) -> dict[str, str]:
    """yarn.lock v1 is its own format — indentation-significant, comma-separated
    keys, quoted selectively. Parsed line by line rather than guessed at."""
    out: dict[str, str] = {}
    names: list[str] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith((" ", "\t")):
            if raw.rstrip().endswith(":"):
                names = [_strip_range(part) for part in
                         raw.rstrip()[:-1].split(",") if part.strip()]
            continue
        m = re.match(r'\s+version\s+"?([^"\s]+)"?', raw)
        if m and names:
            for n in names:
                if n:
                    out[n] = m.group(1)
            names = []
    return out


def parse_yaml_lock(text: str, kind: str) -> dict[str, str]:
    if yaml is None:
        raise LockfileError("pyyaml is required to read yarn-berry and pnpm "
                            "lockfiles (pip install pyyaml)")
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        raise LockfileError(f"not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise LockfileError("lockfile must be a YAML mapping")
    out: dict[str, str] = {}

    if kind == "yarn-berry":
        for key, meta in data.items():
            if key == "__metadata" or not isinstance(meta, dict):
                continue
            version = meta.get("version")
            if not version:
                continue
            for part in str(key).split(","):
                name = _strip_range(part.replace("@npm:", "@").replace("@workspace:", "@"))
                if name:
                    out[name] = str(version)
        return out

    # pnpm: keys look like `/lodash@4.17.21` or the older `/lodash/4.17.21`,
    # sometimes with a peer-suffix in parentheses.
    for key, meta in (data.get("packages") or {}).items():
        entry = str(key).lstrip("/")
        entry = entry.split("(")[0]
        name = version = None
        if "@" in entry[1:]:
            at = entry.rfind("@")
            if at > 0:
                name, version = entry[:at], entry[at + 1:]
        if not name and "/" in entry:
            name, _, version = entry.rpartition("/")
        if isinstance(meta, dict) and meta.get("version"):
            version = str(meta["version"])
        if name and version:
            out[name] = version

    for name, meta in (data.get("importers") or {}).items():
        if not isinstance(meta, dict):
            continue
        for section in ("dependencies", "devDependencies"):
            for dep, spec in (meta.get(section) or {}).items():
                v = spec.get("version") if isinstance(spec, dict) else spec
                if v:
                    out.setdefault(dep, str(v).split("(")[0])
    return out


PARSERS = {
    "npm": parse_npm,
    "yarn-v1": parse_yarn_v1,
    "yarn-berry": lambda t: parse_yaml_lock(t, "yarn-berry"),
    "pnpm": lambda t: parse_yaml_lock(t, "pnpm"),
}


def parse_any(text: str, filename: str = "") -> tuple[dict[str, str], str]:
    """(resolved, format). Raises LockfileError with something readable."""
    if not text.strip():
        raise LockfileError("lockfile is empty")
    kind = detect(text, filename)
    try:
        resolved = PARSERS[kind](text)
    except LockfileError:
        raise
    except json.JSONDecodeError as e:
        raise LockfileError(f"not valid JSON: {e}") from None
    except Exception as e:
        raise LockfileError(f"could not read {kind} lockfile: {e}") from None
    # A version string that is a range or a URL is not a resolved version.
    clean = {n: v for n, v in resolved.items()
             if n and v and not v.startswith(("http", "file:", "link:", "workspace:"))}
    if not clean:
        raise LockfileError(f"no resolved packages found in this {kind} lockfile")
    return clean, kind


# ==========================================================================
# The other four ecosystems
#
# A project integrating this API has whatever file its language gave it, so
# the detection below works from the filename first and the content second.
# The important output is not just the dependency list but the *precision*:
#
#   exact      the file is a resolved tree — everything installed is named in
#              it, so watching it is complete and needs no traversal
#   inferred   the file is a manifest — it names direct dependencies and ranges,
#              and reaching the rest means traversing the crawled graph, which
#              is as complete as our coverage and no more
#
# Conflating those would be the same mistake as reporting a semver range as a
# pin: it claims a completeness the input never had.
# ==========================================================================

# filename -> (ecosystem, kind, precision)
FILE_KINDS = {
    "package-lock.json": ("npm", "npm", "exact"),
    "npm-shrinkwrap.json": ("npm", "npm", "exact"),
    "yarn.lock": ("npm", "yarn", "exact"),
    "pnpm-lock.yaml": ("npm", "pnpm", "exact"),
    "package.json": ("npm", "package.json", "inferred"),
    "requirements.txt": ("pypi", "requirements", "exact"),
    "poetry.lock": ("pypi", "poetry.lock", "exact"),
    "pipfile.lock": ("pypi", "Pipfile.lock", "exact"),
    "uv.lock": ("pypi", "uv.lock", "exact"),
    "pyproject.toml": ("pypi", "pyproject.toml", "inferred"),
    "cargo.lock": ("crates", "Cargo.lock", "exact"),
    "cargo.toml": ("crates", "Cargo.toml", "inferred"),
    "go.sum": ("go", "go.sum", "exact"),
    "go.mod": ("go", "go.mod", "inferred"),
    "pom.xml": ("maven", "pom.xml", "inferred"),
    "gradle.lockfile": ("maven", "gradle.lockfile", "exact"),
    "build.gradle": ("maven", "build.gradle", "inferred"),
    "build.gradle.kts": ("maven", "build.gradle.kts", "inferred"),
}


def parse_requirements(text: str) -> tuple[dict[str, str], bool]:
    """(resolved, fully_pinned) from a requirements.txt.

    Only `==` is a resolved version. A line carrying `>=` or `~=` is a range,
    and treating it as pinned would report a project as watching a version it
    may never install.
    """
    out, pinned = {}, True
    for raw in text.splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue                       # -r includes, -e editables, flags
        line = line.split(";")[0].strip()  # environment markers
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*(.*)$", line)
        if not m:
            continue
        name, _extras, rest = m.group(1), m.group(2), (m.group(3) or "").strip()
        exact = re.match(r"^==\s*([^\s,]+)$", rest)
        if exact:
            out[name] = exact.group(1)
        else:
            out[name] = ""
            if rest:
                pinned = False
    return out, pinned


def parse_poetry_lock(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    return {p["name"]: p.get("version", "")
            for p in data.get("package", []) if p.get("name")}


def parse_uv_lock(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    return {p["name"]: p.get("version", "")
            for p in data.get("package", []) if p.get("name")}


def parse_pipfile_lock(text: str) -> dict[str, str]:
    data = json.loads(text)
    out = {}
    for section in ("default", "develop"):
        for name, meta in (data.get(section) or {}).items():
            version = (meta or {}).get("version", "") if isinstance(meta, dict) else ""
            out[name] = version.lstrip("=") if version else ""
    return out


def parse_pyproject(text: str) -> dict[str, str]:
    """PEP 621 `dependencies`, plus Poetry's own table."""
    data = tomllib.loads(text)
    out = {}
    for spec in (data.get("project") or {}).get("dependencies") or []:
        m = re.match(r"^\s*([A-Za-z0-9._-]+)", str(spec))
        if m:
            out[m.group(1)] = ""
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        out[name] = spec if isinstance(spec, str) and spec[:1].isdigit() else ""
    return out


def parse_cargo_lock(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    return {p["name"]: p.get("version", "")
            for p in data.get("package", []) if p.get("name")}


def parse_cargo_toml(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    out = {}
    for table in ("dependencies", "build-dependencies"):
        for name, spec in (data.get(table) or {}).items():
            if isinstance(spec, str):
                out[name] = ""             # a Cargo requirement is a caret range
            elif isinstance(spec, dict) and not spec.get("path"):
                out[name] = ""
    return out


def parse_go_sum(text: str) -> dict[str, str]:
    """go.sum names the modules actually selected, one line per hash.

    The `/go.mod` lines are hashes of the manifest rather than of the module,
    so they are skipped: the same module appears twice and the second entry is
    not a separate dependency.
    """
    out = {}
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 3 or parts[1].endswith("/go.mod"):
            continue
        out[parts[0]] = parts[1]
    return out


def parse_go_mod(text: str) -> dict[str, str]:
    from ecosystems.golang import GoAdapter
    return dict(GoAdapter.parse_gomod(text))


def parse_gradle_lockfile(text: str) -> dict[str, str]:
    """Lines of `group:artifact:version=configurations`."""
    out = {}
    for raw in text.splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("empty="):
            continue
        coord = line.split("=")[0]
        parts = coord.split(":")
        if len(parts) >= 3:
            out[f"{parts[0]}:{parts[1]}"] = parts[2]
    return out


def parse_pom(text: str) -> dict[str, str]:
    from ecosystems import maven as mavenmod
    props = mavenmod.parse_properties(text)
    managed = mavenmod.parse_managed(text, props)
    out = {}
    for d in mavenmod.parse_dependencies(text, props, managed):
        if d["kind"] in ("prod", "provided", "optional"):
            # An unresolved ${property} stays empty rather than being guessed.
            out[d["dep"]] = "" if d["unresolved"] else d["range"]
    return out


def parse_gradle_build(text: str) -> dict[str, str]:
    """Groovy and Kotlin DSL both write coordinates as a single string."""
    out = {}
    for m in re.finditer(
            r"""["']([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+)(?::([^"'\s]+))?["']""",
            text):
        group, artifact, version = m.group(1), m.group(2), m.group(3)
        if "." not in group:
            continue                       # not a groupId; almost certainly noise
        out[f"{group}:{artifact}"] = (version or "") if not (
            version or "").startswith("$") else ""
    return out


MULTI_PARSERS = {
    "requirements": lambda t: parse_requirements(t)[0],
    "poetry.lock": parse_poetry_lock,
    "uv.lock": parse_uv_lock,
    "Pipfile.lock": parse_pipfile_lock,
    "pyproject.toml": parse_pyproject,
    "Cargo.lock": parse_cargo_lock,
    "Cargo.toml": parse_cargo_toml,
    "go.sum": parse_go_sum,
    "go.mod": parse_go_mod,
    "pom.xml": parse_pom,
    "gradle.lockfile": parse_gradle_lockfile,
    "build.gradle": parse_gradle_build,
    "build.gradle.kts": parse_gradle_build,
}


def parse_package_json(text: str) -> dict[str, str]:
    data = json.loads(text)
    out = {}
    for table in ("dependencies", "optionalDependencies"):
        for name, spec in (data.get(table) or {}).items():
            out[name] = "" if not str(spec)[:1].isdigit() else str(spec)
    return out


def detect_project(text: str, filename: str = "") -> tuple[str, str, str]:
    """(ecosystem, kind, precision) from the filename, then the content."""
    base = (filename or "").replace("\\", "/").split("/")[-1].lower()
    if base in FILE_KINDS:
        return FILE_KINDS[base]

    stripped = text.lstrip()
    if stripped.startswith("<?xml") or "<project" in stripped[:400]:
        return "maven", "pom.xml", "inferred"
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
            data = {}
        if "_meta" in data and ("default" in data or "develop" in data):
            return "pypi", "Pipfile.lock", "exact"
        if "lockfileVersion" in data or "packages" in data:
            return "npm", "npm", "exact"
        if "dependencies" in data and "name" in data:
            return "npm", "package.json", "inferred"
        return "npm", "npm", "exact"
    if re.search(r"^\[\[package\]\]", text, re.M):
        # Cargo.lock, poetry.lock and uv.lock are all [[package]] TOML, so the
        # discriminator has to be a marker only one of them writes. Checked in
        # order of how conclusive each one is.
        if "crates.io-index" in text:
            return "crates", "Cargo.lock", "exact"
        if any(k in text for k in ("python-versions", "pypi.org/simple",
                                   "requires-dist", "wheels = [", "sdist = {")):
            return "pypi", ("uv.lock" if ("wheels = [" in text
                                          or "pypi.org/simple" in text)
                            else "poetry.lock"), "exact"
        # Nothing conclusive. `checksum` is Cargo's. Guessing the other way
        # would parse a Rust lockfile as Python and watch packages that do not
        # exist on PyPI.
        return (("crates", "Cargo.lock", "exact")
                if re.search(r"^checksum = ", text, re.M)
                else ("pypi", "poetry.lock", "exact"))
    if re.search(r"^module\s+\S+", text, re.M):
        return "go", "go.mod", "inferred"
    if re.search(r"^\S+\s+v\d[^\s]*\s+h1:", text, re.M):
        return "go", "go.sum", "exact"
    return "npm", detect(text, filename), "exact"


def parse_project(text: str, filename: str = "",
                  ecosystem: str | None = None) -> tuple[dict[str, str], str, str, str]:
    """(resolved, kind, ecosystem, precision) for any supported project file."""
    if not text.strip():
        raise LockfileError("file is empty")

    eco, kind, precision = detect_project(text, filename)
    if ecosystem and ecosystem != eco:
        # An explicit ecosystem overrides sniffing, but only the ecosystem —
        # the parser still has to match the bytes it was actually given.
        eco = ecosystem

    try:
        if kind == "requirements":
            resolved, pinned = parse_requirements(text)
            precision = "exact" if pinned else "inferred"
        elif kind == "package.json":
            resolved = parse_package_json(text)
        elif kind in MULTI_PARSERS:
            resolved = MULTI_PARSERS[kind](text)
        else:
            resolved, kind = parse_any(text, filename)
    except LockfileError:
        raise
    except Exception as exc:
        raise LockfileError(f"could not read this {kind}: {exc}") from None

    clean = {n.strip(): (v or "").strip() for n, v in resolved.items()
             if n and n.strip()
             and not str(v or "").startswith(("http", "file:", "link:",
                                              "workspace:", "git+"))}
    if not clean:
        raise LockfileError(f"no dependencies found in this {kind}")
    return clean, kind, eco, precision
