"""Capture the README screenshots from the running console.

Every image in docs/ is a photograph of the real thing answering a real query —
no mockups, no composited numbers. That is the same rule the product holds
itself to, and a screenshot is the easiest place to quietly break it.

The console lives at /check now, the API at /developers and the account at
/dashboard, so this drives all four surfaces rather than one page.

  py server.py          # in another terminal, warm
  py shots.py
"""

import os
import secrets
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BLAST_BASE", "http://127.0.0.1:8000")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "images")
SCALE = 2                     # retina; GitHub renders these at half size

HIDE_STICKY = """
  .hdr, .topbar { display: none !important; }
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


def full(page, name, height=1000):
    page.wait_for_timeout(400)
    page.screenshot(path=os.path.join(OUT, name),
                    clip={"x": 0, "y": 0, "width": 1500, "height": height})
    print(f"  {name}")


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 1000},
                                  device_scale_factor=SCALE)
        page = ctx.new_page()

        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("capturing:")

        # ---------------------------------------------------------- landing
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function(
            "!document.querySelector('#statline').textContent.includes('connecting')",
            timeout=90_000)
        page.wait_for_timeout(3000)
        full(page, "hero.png")

        page.eval_on_selector("#console", "el => el.scrollIntoView({block:'start'})")
        page.wait_for_timeout(1200)
        shot(page, ".console-card", "console-card.png")
        shot(page, "#shift .panel", "incident-chart.png")

        # ------------------------------------------------------------ check
        page.goto(f"{BASE}/check", wait_until="domcontentloaded")
        page.wait_for_selector("#peek-hist .skel", state="detached", timeout=90_000)
        page.wait_for_timeout(2000)
        full(page, "check.png", height=860)

        page.click(".chip")
        page.wait_for_function(
            "document.querySelector('#latency').textContent !== '—'", timeout=120_000)
        page.wait_for_selector("#map .node", timeout=120_000)
        page.wait_for_timeout(3000)

        shot(page, "#results .latencyblock", "latency.png")
        shot(page, ".resultgrid .win.span2:last-of-type", "blast-map.png")
        shot(page, "#explorer .win", "explorer.png")
        shot(page, "#live .win", "live-feed.png")

        # ------------------------------------------------------- developers
        page.goto(f"{BASE}/developers", wait_until="domcontentloaded")
        page.wait_for_selector("#endpoints .row", timeout=60_000)
        page.wait_for_timeout(1500)
        full(page, "api.png", height=900)
        shot(page, "#quickstart .surface", "quickstart.png")

        # --------------------------------------------------------- account
        # A throwaway account so the dashboard has something real in it, rather
        # than a screenshot of empty states.
        email = f"shots-{secrets.token_hex(4)}@example.com"
        made = ctx.request.post(f"{BASE}/api/auth/signup", data={
            "email": email, "password": secrets.token_urlsafe(16), "name": "Demo"})
        if made.ok:
            ctx.request.post(f"{BASE}/api/keys", data={"name": "CI"})
            ctx.request.post(f"{BASE}/api/monitors", data={"package": "debug"})
            ctx.request.post(f"{BASE}/api/monitors", data={"package": "chalk"})
            page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
            page.wait_for_selector("#stats .statbox", timeout=60_000)
            page.wait_for_timeout(4000)
            full(page, "dashboard.png", height=900)
        else:
            print("  (skipped dashboard.png — signup returned "
                  f"{made.status}; email confirmation may be required)")

        if errors:
            print("\npage errors during capture:", errors[:4], file=sys.stderr)
            return 1
        browser.close()
    print("\nall images written to docs/images/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
