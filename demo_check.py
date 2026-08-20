"""Run this immediately before recording. It fails loudly rather than subtly.

A demo does not break because something is obviously broken — it breaks because
one panel quietly says "—" while you are talking over it. This loads the page
cold, waits for everything that is supposed to fill in, and asserts that no
region is still empty and that nothing errored.

  py demo_check.py                 # against a live server
  py demo_check.py --demo          # verify DEMO_MODE fixtures are complete
"""

import argparse
import json
import os
import sys
import time

import requests

BASE = os.environ.get("BLAST_BASE", "http://127.0.0.1:8000")
ROOT = os.path.dirname(os.path.abspath(__file__))
GREEN, RED, AMBER, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{GREEN}PASS{OFF}]" if ok else f"  [{RED}FAIL{OFF}]", name,
          f"{DIM}{detail}{OFF}" if detail else "")


# The panels a viewer will be looking at, and what "still empty" looks like.
PANELS = [
    ("#statline", "header graph size", ("connecting", "unavailable", "")),
    ("#peek-hist", "hero: depth histogram", ("querying", "")),
    ("#peek-maint", "hero: maintainer pivot", ("querying", "")),
    ("#peek-graph", "hero: graph stats", ("counting", "")),
    ("#syscards", "status cards", ("",)),
    ("#sysnote", "status note", ("",)),
    ("#graphstat", "explorer node count", ("", "asking hydradb")),
]


def fixtures_present():
    print(f"\n{AMBER}demo fixtures{OFF}")
    path = os.path.join(ROOT, "fixtures", "demo.json")
    if not os.path.exists(path):
        check("fixtures/demo.json exists", False, "run the capture step first")
        return
    data = json.load(open(path, encoding="utf-8"))
    check("fixtures/demo.json exists", True,
          f"{len(data)} captures, {os.path.getsize(path) // 1024}KB")
    need = ["blast_debug", "blast_ua", "intel_debug", "fix_debug",
            "attack_surface_qix", "audit_compromised", "expand_debug"]
    missing = [n for n in need if n not in data]
    check("every preset the video uses is captured", not missing,
          ", ".join(missing) if missing else "debug, ua-parser-js, qix, audit")
    stale = [n for n, v in data.items()
             if time.time() - v.get("captured_at", 0) > 86400 * 7]
    check("captures are recent", not stale,
          f"{len(stale)} older than a week" if stale else "all within a week")


def api_presets():
    print(f"\n{AMBER}the three preset incidents{OFF}")
    for name, version in (("debug", "4.4.2"), ("ua-parser-js", "0.7.29"),
                          ("event-stream", "3.3.6")):
        try:
            t0 = time.perf_counter()
            r = requests.get(f"{BASE}/api/blast",
                             params={"name": name, "depth": 5}, timeout=90)
            ms = (time.perf_counter() - t0) * 1000
            d = r.json()
            check(f"blast {name}", r.status_code == 200 and d.get("total", 0) > 0,
                  f"{d.get('total', 0):,} exposed, {ms:.0f}ms"
                  + (" (fixture)" if d.get("demo") else ""))
        except Exception as e:
            check(f"blast {name}", False, f"{e.__class__.__name__}")
    try:
        d = requests.get(f"{BASE}/api/intel",
                         params={"name": "debug", "version": "4.4.2"},
                         timeout=90).json()
        check("intel says debug@4.4.2 is malicious", d.get("verdict") == "malicious",
              f"verdict={d.get('verdict')}")
    except Exception as e:
        check("intel debug@4.4.2", False, str(e)[:80])
    try:
        body = open(os.path.join(ROOT, "tests", "fixtures",
                                 "lock-compromised.json"), "rb").read()
        d = requests.post(f"{BASE}/api/audit", data=body, timeout=200).json()
        check("audit returns COMPROMISED", d.get("verdict") == "COMPROMISED",
              f"{d.get('malicious_count')} malicious of {d.get('scanned')}")
    except Exception as e:
        check("audit compromised tree", False, str(e)[:80])


def cold_page(headed=False):
    print(f"\n{AMBER}cold page load{OFF}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright available", False, "pip install playwright")
        return
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not headed)
        # A brand new context: no cache, no service worker, nothing warm.
        page = browser.new_context(viewport={"width": 1600, "height": 1100}).new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("#peek-hist .skel", state="detached", timeout=90_000)
            page.wait_for_selector("#graph .gnode", timeout=90_000)
            page.wait_for_selector("#syscards .card", timeout=60_000)
        except Exception as e:
            check("page finished loading", False, str(e)[:110])
        page.wait_for_timeout(2500)

        for sel, label, empties in PANELS:
            text = (page.text_content(sel) or "").strip().lower()
            filled = bool(text) and not any(text.startswith(e) for e in empties if e)
            check(f"{label} is filled", filled, (text[:56] or "EMPTY"))

        check("explorer drew nodes",
              page.eval_on_selector_all("#graph .gnode", "e => e.length") > 1,
              f'{page.eval_on_selector_all("#graph .gnode", "e => e.length")} nodes')
        check("no console errors on cold load", not errors, str(errors[:2]))
        browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--skip-page", action="store_true")
    args = ap.parse_args()

    try:
        h = requests.get(f"{BASE}/api/health", timeout=60).json()
        c = h.get("components", {})
        print(f"\n{AMBER}server{OFF}")
        check("server responding", True, f"status={h.get('status')}")
        check("hydradb answering", c.get("hydradb", {}).get("up"),
              str(c.get("hydradb", {}))[:70])
        check("graph is warm", c.get("warmup", {}).get("state") == "warm",
              f"state={c.get('warmup', {}).get('state')} — a cold graph times "
              f"out on deep traversals for ~90s after a restart")
        check("osv reachable", c.get("osv", {}).get("up"),
              str(c.get("osv", {}))[:70])
    except Exception as e:
        check("server responding", False, f"{e.__class__.__name__} — is it running?")
        print(f"\n{RED}start it with: py server.py{OFF}\n")
        return 1

    fixtures_present()
    api_presets()
    if not args.skip_page:
        cold_page(args.headed)

    bad = [r for r in results if not r[1]]
    print(f"\n{'=' * 70}")
    if bad:
        print(f"{RED}{len(bad)} of {len(results)} checks failed — "
              f"do not record yet{OFF}")
        for name, _, detail in bad:
            print(f"  - {name}: {detail}")
        return 1
    print(f"{GREEN}{len(results)}/{len(results)} — safe to record{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
