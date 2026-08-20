"""Click every interactive element on the console and prove it did something.

"The buttons work" is a claim that is easy to make and easy to get wrong — a
handler can be wired to the wrong selector, throw silently, or do nothing
visible. So this enumerates every control on the page, activates it the way a
person would, and asserts on the observable consequence: a request went out, a
value changed, an element appeared, the URL updated.

Run:  py web_audit.py            (needs the server up and Chrome installed)
      py web_audit.py --head     (watch it happen)
"""

import argparse
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

# The Windows console is cp1252 and package data is full of characters it
# cannot encode; without this a stray arrow in a page string aborts the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{GREEN}PASS{OFF}]" if ok else f"  [{RED}FAIL{OFF}]", name,
          f"{DIM}{detail}{OFF}" if detail else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", action="store_true")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not args.head)
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        errors, requests_made = [], []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: requests_made.append(r.url))

        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_selector("#peek-hist .skel", state="detached", timeout=60_000)

        # ---- header ---------------------------------------------------------
        print("\nheader")
        check("live stats populated from the API",
              "packages" in (page.text_content("#statline") or ""),
              page.text_content("#statline"))
        check("event stream is the source, not polling",
              any("/api/events" in u for u in requests_made))

        for label, href, target in (("lockfile check", "#lockfile", "#lockfile"),
                                    ("status", "#status", "#status"),
                                    ("how it works", "#how", "#how")):
            page.click(f'.nav a[href="{href}"]')
            page.wait_for_timeout(700)
            in_view = page.eval_on_selector(
                target, "el => { const r = el.getBoundingClientRect();"
                        " return r.top < window.innerHeight && r.bottom > 0; }")
            check(f"nav '{label}' scrolls to its section", in_view)

        page.click(".brand")
        page.wait_for_timeout(600)
        check("brand returns to the top", page.evaluate("window.scrollY") < 400,
              f"scrollY={page.evaluate('window.scrollY')}")

        # ---- search + autocomplete -----------------------------------------
        print("\nsearch")
        page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        page.wait_for_timeout(300)
        page.fill("#pkg", "expr")
        page.wait_for_selector("#suggest button", timeout=30_000)
        n = page.eval_on_selector_all("#suggest button", "e => e.length")
        # A dropdown that paints under a later sibling is still "visible" to a
        # selector but unclickable to a person, so this asserts on the real
        # hit-target rather than on the element existing.
        hit = page.eval_on_selector(
            "#suggest button",
            "el => { const r = el.getBoundingClientRect();"
            " const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);"
            " return top && el.contains(top) ? 'ok' : (top ? top.className : 'none'); }")
        check("autocomplete is actually on top and clickable", hit == "ok", str(hit))
        check("typing opens autocomplete with live results", n > 0, f"{n} suggestions")
        first = page.eval_on_selector("#suggest button", "e => e.dataset.name")
        page.click("#suggest button")
        check("clicking a suggestion fills the field",
              page.input_value("#pkg") == first, first)
        check("autocomplete closes after selection", page.is_hidden("#suggest"))

        page.fill("#pkg", "expr")
        page.wait_for_selector("#suggest button", timeout=30_000)
        page.press("#pkg", "ArrowDown")
        page.press("#pkg", "Enter")
        check("keyboard navigation selects a suggestion",
              page.input_value("#pkg") != "expr", page.input_value("#pkg"))
        page.press("#pkg", "Escape")

        # ---- preset chips ---------------------------------------------------
        print("\npreset chips")
        chips = page.query_selector_all(".chip")
        check("three real-incident chips are present", len(chips) == 3)
        for i, chip in enumerate(chips):
            pkg = chip.get_attribute("data-pkg")
            page.eval_on_selector_all(
                ".chip", f"els => els[{i}].click()")
            page.wait_for_function(
                "document.querySelector('#latency').textContent !== '—'",
                timeout=120_000)
            page.wait_for_timeout(900)
            check(f"chip '{pkg}' runs a real query",
                  page.input_value("#pkg") == pkg
                  and page.text_content("#latency").endswith("ms"),
                  f"{page.text_content('#latency')} · "
                  f"{page.text_content('#verdictline')[:60]}")

        # ---- the submit button ---------------------------------------------
        print("\nquery form")
        page.fill("#pkg", "chalk")
        page.fill("#ver", "5.6.1")
        page.click("#go")
        page.wait_for_function(
            "document.querySelector('#go').textContent.includes('querying')",
            timeout=30_000)
        check("submit button shows progress while running", True,
              page.text_content("#go"))
        page.wait_for_function(
            "!document.querySelector('#go').disabled", timeout=120_000)
        page.wait_for_timeout(800)
        for panel, label in (("#hist .hrow", "depth histogram"),
                             ("#victims .r", "victim list"),
                             ("#maint .r, #maint .empty", "maintainer pivot"),
                             ("#typos .r, #typos .empty", "typosquat ring"),
                             ("#semver .stat, #semver .note", "semver panel")):
            check(f"{label} rendered", page.eval_on_selector_all(panel, "e => e.length") > 0)
        check("url reflects the query", "pkg=chalk" in page.url and "v=5.6.1" in page.url,
              page.url)

        # ---- blast map ------------------------------------------------------
        print("\nblast map")
        page.wait_for_selector("#map .node", timeout=120_000)
        base_nodes = page.eval_on_selector_all("#map .node", "e => e.length")
        check("map drew the graph", base_nodes > 1,
              f"{base_nodes} nodes, "
              f"{page.eval_on_selector_all('#map .edge', 'e => e.length')} edges")
        check("legend reports what it left out",
              "exposed" in (page.text_content("#maplegend") or ""),
              (page.text_content("#maplegend") or "")[-60:])

        for d in ("1", "2", "4"):
            page.click(f'.mini[data-mapdepth="{d}"]')
            page.wait_for_timeout(2500)
            rings = page.eval_on_selector_all("#map .ring", "e => e.length")
            check(f"depth button '{d}' redraws the map", rings == int(d),
                  f"{rings} rings")
        page.click('.mini[data-mapdepth="3"]')
        page.wait_for_timeout(2500)

        node = page.query_selector_all("#map .node:not(.root):not(.label)")[3]
        node.hover()
        page.wait_for_timeout(400)
        check("hovering a node shows a tooltip", not page.is_hidden("#maptip"),
              (page.text_content("#maptip") or "")[:60])
        check("hovering isolates its edges",
              page.eval_on_selector_all("#map .edge.hot", "e => e.length") > 0
              and page.eval_on_selector_all("#map .node.dim", "e => e.length") > 0)

        before = page.input_value("#pkg")
        page.eval_on_selector_all(
            "#map .node",
            "els => els.find(e => !e.classList.contains('root') &&"
            " !e.classList.contains('label')).dispatchEvent("
            "new MouseEvent('click', {bubbles: true}))")
        page.wait_for_function("v => document.querySelector('#pkg').value !== v",
                               arg=before, timeout=90_000)
        check("clicking a node pivots the whole console",
              page.input_value("#pkg") != before,
              f"{before} -> {page.input_value('#pkg')}")

        # ---- lockfile -------------------------------------------------------
        print("\nlockfile")
        page.fill("#pkg", "debug")
        page.fill("#ver", "2.6.9")
        page.set_input_files("#file", "tests/fixtures/lock-v3.json")
        page.wait_for_function(
            "document.querySelector('#verdict .word')?.textContent"
            "?.match(/EXPOSED|SHIELDED|CLEAR/)", timeout=120_000)
        check("dropping a lockfile returns a verdict",
              page.text_content("#verdict .word") == "EXPOSED",
              page.text_content("#verdict .sub")[:70])
        check("the exact dependency path is shown",
              page.eval_on_selector_all("#lockdetail .pathrow", "e => e.length") > 0,
              (page.text_content("#lockdetail") or "")[:60])

        page.set_input_files("#file", "tests/fixtures/lock-clean.json")
        page.wait_for_function(
            "document.querySelector('#verdict .word')?.textContent === 'CLEAR'",
            timeout=120_000)
        check("a clean lockfile returns CLEAR",
              page.text_content("#verdict .word") == "CLEAR",
              (page.text_content("#verdict .sub") or "")[:70])

        # ---- status ---------------------------------------------------------
        print("\nstatus")
        cards = page.eval_on_selector_all("#syscards .card", "e => e.length")
        check("component cards rendered", cards >= 6, f"{cards} cards")
        check("status names the components",
              all(k in (page.text_content("#syscards") or "")
                  for k in ("hydradb", "writable", "crawl")))
        check("the graph-count note is present",
              "count(*)" in (page.text_content("#sysnote") or ""))

        # ---- windows are draggable -----------------------------------------
        print("\ndraggable windows")
        moved, tried = 0, 0
        for sel in (".scatter .win", ".results .win", ".how .win"):
            el = page.query_selector(f"{sel} .bar")
            if not el:
                continue
            tried += 1
            # Centre it first: the sticky header would otherwise swallow the
            # press for any window sitting at the top of the viewport.
            page.eval_on_selector(
                sel, "el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
            page.wait_for_timeout(250)
            root = page.eval_on_selector(sel, "el => el.style.transform")
            bb = el.bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.mouse.down()
            page.mouse.move(bb["x"] + bb["width"] / 2 + 90,
                            bb["y"] + bb["height"] / 2 + 55, steps=8)
            page.mouse.up()
            if page.eval_on_selector(sel, "el => el.style.transform") != root:
                moved += 1
        check("every window drags by its title bar", moved == tried,
              f"{moved} of {tried} moved")

        # ---- deep link ------------------------------------------------------
        print("\nshareable url")
        page2 = browser.new_page(viewport={"width": 1400, "height": 900})
        page2.on("pageerror", lambda e: errors.append(str(e)))
        page2.goto(f"{BASE}/?pkg=ms&v=2.0.0", wait_until="domcontentloaded")
        page2.wait_for_function(
            "document.querySelector('#latency').textContent !== '—'", timeout=120_000)
        check("a pasted url restores the whole result",
              page2.input_value("#pkg") == "ms" and page2.input_value("#ver") == "2.0.0",
              page2.text_content("#latency"))
        page2.close()

        check("no console errors during the entire audit", not errors, str(errors[:3]))
        browser.close()

    bad = [r for r in results if not r[1]]
    print(f"\n{'=' * 70}")
    colour = GREEN if not bad else RED
    print(f"{colour}{len(results) - len(bad)}/{len(results)} controls verified{OFF}")
    for name, _, detail in bad:
        print(f"  - {name}: {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
