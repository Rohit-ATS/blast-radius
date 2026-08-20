"""Capture the README screenshots from the running console.

Every image in docs/ is a photograph of the real thing answering a real query —
no mockups, no composited numbers. That is the same rule the product holds
itself to, and a screenshot is the easiest place to quietly break it.

  py server.py          # in another terminal, warm
  py shots.py
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BLAST_BASE", "http://127.0.0.1:8000")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "images")
SCALE = 2                     # retina; GitHub renders these at half size


HIDE_STICKY = """
  .topbar { display: none !important; }
"""


def shot(page, selector, name):
    """Screenshot one section with the sticky header out of the way.

    Scrolling the element clear is not enough: the header has a backdrop blur,
    so it does not merely cover the top of a section, it smears it. Hiding it
    for the capture is the only way to get a clean edge.
    """
    page.add_style_tag(content=HIDE_STICKY)
    page.eval_on_selector(
        selector,
        "el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
    page.wait_for_timeout(500)
    page.locator(selector).screenshot(path=os.path.join(OUT, name))
    print(f"  {name}")


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_context(
            viewport={"width": 1500, "height": 1000},
            device_scale_factor=SCALE).new_page()

        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_selector("#peek-hist .skel", state="detached", timeout=90_000)
        page.wait_for_selector("#graph .gnode", timeout=90_000)
        page.wait_for_timeout(2500)

        print("capturing:")
        page.screenshot(path=os.path.join(OUT, "hero.png"))  # header intentionally kept
        print("  hero.png")

        # Run the headline incident so the result panels hold real numbers.
        page.click(".chip")
        page.wait_for_function(
            "document.querySelector('#latency').textContent !== '—'", timeout=120_000)
        page.wait_for_selector("#map .node", timeout=120_000)
        page.wait_for_timeout(3000)

        shot(page, ".results .latencyblock", "latency.png")
        shot(page, ".resultgrid .win.span2:last-of-type", "blast-map.png")
        shot(page, "#explorer .win", "explorer.png")
        shot(page, "#live .win", "live-feed.png")

        # A real compromised tree through the real audit path.
        page.set_input_files("#auditfile", "tests/fixtures/lock-compromised.json")
        page.wait_for_function(
            "document.querySelector('#auditverdict .word')?.textContent"
            "?.match(/COMPROMISED|VULNERABLE|CLEAN/)", timeout=180_000)
        page.wait_for_timeout(1200)
        page.eval_on_selector(
            ".finding",
            "el => { el.open = true; el.querySelector('.loadfix').click(); }")
        page.wait_for_selector(".fixbox", timeout=180_000)
        page.wait_for_timeout(1500)
        shot(page, "#auditresult", "audit.png")

        browser.close()
    if errors:
        print("console errors during capture:", errors[:3], file=sys.stderr)
        return 1
    print(f"\nwrote to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
