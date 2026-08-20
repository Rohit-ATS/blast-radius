"""Blast Radius from the command line — and from CI.

  py cli.py audit ./package-lock.json          # human output
  py cli.py audit ./pnpm-lock.yaml --json      # machine output
  py cli.py blast debug --depth 5
  py cli.py intel debug 4.4.2
  py cli.py fix debug 4.4.2

Exit codes are the point of this file. A scanner that always exits 0 cannot
fail a build, and a security tool that cannot fail a build gets ignored:

  0  clean
  1  something in the tree is confirmed malicious
  2  known vulnerabilities, no malware  (use --fail-on vuln to make this fail)
  3  the scan could not be completed — unreadable file, no network, bad args

The distinction between 1 and 2 matters. Malware means someone attacked you and
the build must stop; a ReDoS advisory in a dev dependency usually should not
wake anybody at 2am, so it is a different code and non-fatal by default.
"""

import argparse
import json
import os
import sys

# Run against a server if one is given, otherwise import the engine directly so
# the CLI works with nothing else running.
BASE = os.environ.get("BLAST_BASE", "")

EXIT_CLEAN, EXIT_MALWARE, EXIT_VULN, EXIT_ERROR = 0, 1, 2, 3

BOLD, RED, AMBER, GREEN, DIM, OFF = (
    "\033[1m", "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _no_colour():
    return not sys.stdout.isatty() or os.environ.get("NO_COLOR")


def paint(text, colour):
    return text if _no_colour() else f"{colour}{text}{OFF}"


# --------------------------------------------------------------------------

def cmd_audit(args):
    import intel
    import lockfiles

    path = args.lockfile
    if not os.path.exists(path):
        return err(f"no such file: {path}")
    try:
        text = open(path, encoding="utf-8").read()
    except UnicodeDecodeError:
        return err(f"{path} is not valid UTF-8")
    try:
        resolved, kind = lockfiles.parse_any(text, os.path.basename(path))
    except lockfiles.LockfileError as e:
        return err(str(e))

    result = intel.audit_tree(resolved, max_detail=args.max_detail)
    result["lockfile_format"] = kind
    result["lockfile"] = path
    malicious = result["malicious_count"]
    vulnerable = result["vulnerable_count"]
    verdict = ("COMPROMISED" if malicious else
               "VULNERABLE" if vulnerable else "CLEAN")
    result["verdict"] = verdict

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_audit(result, verdict, malicious, vulnerable)

    if malicious:
        return EXIT_MALWARE
    if vulnerable:
        return EXIT_VULN if args.fail_on == "vuln" else EXIT_CLEAN
    return EXIT_CLEAN


def _print_audit(result, verdict, malicious, vulnerable):
    colour = RED if malicious else (AMBER if vulnerable else GREEN)
    print()
    print(paint(f"  {verdict}", BOLD + colour))
    print(f"  {result['scanned']:,} packages from {result['lockfile']} "
          f"({result['lockfile_format']}) checked against osv.dev "
          f"in {result['latency_ms']:.0f}ms")
    if malicious:
        print(paint(f"  {malicious} confirmed malicious, "
                    f"{vulnerable} with known vulnerabilities", RED))
    elif vulnerable:
        print(paint(f"  {vulnerable} with known vulnerabilities, no malware",
                    AMBER))
    else:
        print(paint("  nothing in this tree matches a published advisory", GREEN))
    print()
    for f in result["findings"]:
        tag = paint("MALWARE", RED) if f["malware"] else paint("vuln  ", AMBER)
        top = (f["malware"] or f["vulnerabilities"])[0]
        print(f"  [{tag}] {f['name']}@{f['version']}")
        print(f"           {top['id']}  {top['summary'][:76]}")
        extra = len(f["malware"]) + len(f["vulnerabilities"]) - 1
        if extra > 0:
            print(paint(f"           +{extra} more advisor"
                        f"{'y' if extra == 1 else 'ies'}", DIM))
    if result.get("truncated"):
        print(paint(f"\n  {result['flagged']} packages flagged; the "
                    f"{result['detailed']} worst are shown. --max-detail to widen.",
                    DIM))
    if malicious:
        first = result["findings"][0]
        print(paint(f"\n  next: py cli.py fix {first['name']} {first['version']}",
                    DIM))
    print()


def cmd_blast(args):
    from hydra import Hydra
    import blast
    h = Hydra(budget=30.0)
    known, _ = blast.resolve_package(h, args.package)
    if not known:
        return err(f"'{args.package}' is not in the graph. "
                   f"Try `py cli.py intel {args.package}` for live registry data.")
    r, ms = blast.blast_radius(h, args.package, args.depth)
    if args.json:
        print(json.dumps({**r, "latency_ms": ms}, indent=2))
        return EXIT_CLEAN
    print()
    print(paint(f"  {r['total']:,} packages transitively depend on "
                f"{args.package}", BOLD))
    print(f"  depth {r['depth']} · {ms:.0f}ms · {r['queries']} hydradb queries")
    print()
    for row in r["histogram"]:
        bar = "█" * max(0, min(46, round(row["packages"] / max(
            1, max(x["packages"] for x in r["histogram"])) * 46)))
        print(f"    depth {row['depth']}  {bar} {row['packages']:,}")
    print()
    return EXIT_CLEAN


def cmd_intel(args):
    import intel
    a = intel.assess(args.package, args.version)
    if args.json:
        print(json.dumps(a, indent=2))
    elif not a.get("exists"):
        print(paint(f"\n  {a['message']}\n", AMBER))
        return EXIT_ERROR
    else:
        colour = {"malicious": RED, "vulnerable": AMBER,
                  "watch": AMBER, "clean": GREEN}.get(a["verdict"], DIM)
        print()
        print(paint(f"  {a['name']}@{a['checked_version']} — {a['verdict']}",
                    BOLD + colour))
        print(f"  latest is {a['package']['latest']} · "
              f"{a['package']['versions']} versions · "
              f"maintainers: {', '.join(a['package']['maintainers'][:4]) or 'unknown'}")
        for v in a["advisories"][:6]:
            print(f"    [{v['kind']}] {v['id']}  {v['summary'][:64]}")
        for s in a["signals"][:4]:
            print(paint(f"    (signal) {s['signal']}: {s['detail'][:60]}", DIM))
        print()
    return EXIT_MALWARE if a.get("verdict") == "malicious" else EXIT_CLEAN


def cmd_fix(args):
    import intel
    r = intel.remediation(args.package, args.version)
    if args.json:
        print(json.dumps(r, indent=2))
        return EXIT_CLEAN
    print()
    if r.get("recommended"):
        print(paint(f"  upgrade {args.package} {args.version} -> "
                    f"{r['recommended']}", BOLD + GREEN))
    else:
        print(paint(f"  no clean release above {args.version} was found", AMBER))
    if r.get("package_json_overrides"):
        print(f"\n  package.json:\n")
        for line in json.dumps(r["package_json_overrides"], indent=2).splitlines():
            print(f"    {line}")
    print(paint("\n  --- brief for a coding agent ---", DIM))
    print(r["ai_prompt"])
    print()
    return EXIT_CLEAN


def err(message):
    print(paint(f"\n  {message}\n", RED), file=sys.stderr)
    return EXIT_ERROR


def main():
    p = argparse.ArgumentParser(
        prog="blast-radius",
        description="npm supply-chain incident response. Exit 1 on malware, "
                    "2 on vulnerabilities with --fail-on vuln, 3 on error.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("audit", help="scan a lockfile against osv.dev")
    a.add_argument("lockfile")
    a.add_argument("--max-detail", type=int, default=60)
    a.add_argument("--fail-on", choices=("malware", "vuln"), default="malware",
                   help="exit non-zero on vulnerabilities too (default: malware only)")
    a.set_defaults(func=cmd_audit)

    b = sub.add_parser("blast", help="who transitively depends on a package")
    b.add_argument("package")
    b.add_argument("--depth", type=int, default=5)
    b.set_defaults(func=cmd_blast)

    i = sub.add_parser("intel", help="is a package real, current, compromised")
    i.add_argument("package")
    i.add_argument("version", nargs="?")
    i.set_defaults(func=cmd_intel)

    f = sub.add_parser("fix", help="how to remediate a compromised package")
    f.add_argument("package")
    f.add_argument("version")
    f.set_defaults(func=cmd_fix)

    args = p.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_ERROR
    except Exception as e:
        return err(f"{e.__class__.__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
