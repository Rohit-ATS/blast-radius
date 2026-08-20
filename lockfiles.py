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
