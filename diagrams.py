"""Render the repository's diagrams from hand-written HTML.

Everything here is drawn from scratch in the console's own visual language —
paper background, dotted grid, monospace numbers, colour reserved for severity.
No stock assets, no icon packs, no external fonts: the same reason the console
has no CDN dependency, plus these have to be regenerable when the numbers move.

  py diagrams.py
"""

import os
import sys

from playwright.sync_api import sync_playwright

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "images")

BASE_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #f4f4f2;
    color: #1a1a18;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    text-transform: lowercase;
    -webkit-font-smoothing: antialiased;
    background-image: radial-gradient(circle at 1px 1px, rgba(26,26,24,.10) 1px, transparent 0);
    background-size: 24px 24px;
  }
  .mono { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
          text-transform: none; }
  .card {
    background: #fffffd; border: 1px solid #d9d9d3; border-radius: 12px;
    box-shadow: 0 1px 2px rgba(26,26,24,.05), 0 10px 28px -14px rgba(26,26,24,.2);
  }
  .bar {
    display: flex; align-items: center; gap: 6px;
    padding: 9px 12px; border-bottom: 1px solid #ebebe6;
    background: linear-gradient(#fdfdfb, #f7f7f4); border-radius: 12px 12px 0 0;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; }
  .title { margin-left: 7px; font-size: 12px; color: #8d8d86; }
"""

# --------------------------------------------------------------------------
# 1. architecture — what lives where, and why
# --------------------------------------------------------------------------

ARCH = """
<style>
  %(base)s
  body { width: 1400px; height: 790px; padding: 38px 44px; }
  h1 { font-size: 34px; letter-spacing: -.03em; font-weight: 640; }
  .sub { color: #55554f; font-size: 15px; margin-top: 6px; }
  .row { display: flex; gap: 22px; margin-top: 26px; align-items: stretch; }
  .col { flex: 1; display: flex; flex-direction: column; gap: 14px; }
  .body { padding: 16px 18px; }
  .k { font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: #8d8d86; }
  .node {
    display: flex; align-items: baseline; gap: 10px; padding: 7px 0;
    border-bottom: 1px dotted #e4e4de; font-size: 14px;
  }
  .node:last-child { border-bottom: 0; }
  .pill { border-radius: 5px; padding: 2px 8px; font-size: 11.5px; border: 1px solid #d9d9d3; }
  .pill.pkg  { background: #eeeeea; color: #55554f; }
  .pill.mnt  { background: #ecf5ee; color: #2c7a45; border-color: #d4e6d9; }
  .pill.adv  { background: #fbf4e6; color: #a9761b; border-color: #ede0c4; }
  .n { margin-left: auto; color: #8d8d86; font-size: 12.5px; }
  .why {
    margin-top: 12px; font-size: 12.5px; color: #55554f; line-height: 1.55;
    border-left: 2px solid #d9d9d3; padding-left: 11px;
  }
  .why b { color: #1a1a18; }
  .chain { margin-top: 22px; }
  .chainrow {
    display: flex; align-items: center; gap: 0; flex-wrap: wrap;
    font-size: 13.5px; padding: 9px 0; border-bottom: 1px dotted #e4e4de;
  }
  .chainrow:last-child { border-bottom: 0; }
  .box { border: 1px solid #d9d9d3; border-radius: 7px; padding: 4px 10px; background: #fffffd; }
  .rel { color: #8d8d86; padding: 0 9px; font-size: 12px; }
  .out { margin-left: auto; color: #c0392b; font-weight: 600; }
</style>
<h1>blast radius — where everything lives</h1>
<div class="sub">topology in the graph, predicates in the sidecar, truth from live sources.
  each split is forced by a hydradb 0.1.0 constraint, not a preference.</div>

<div class="row">
  <div class="col">
    <div class="card">
      <div class="bar"><i class="dot" style="background:#e5837b"></i>
        <i class="dot" style="background:#e0bd76"></i>
        <i class="dot" style="background:#8fc79f"></i>
        <span class="title">hydradb — topology</span></div>
      <div class="body">
        <div class="k">nodes</div>
        <div class="node"><span class="pill pkg">Package</span><span class="mono">name, latest</span><span class="n mono">27,076</span></div>
        <div class="node"><span class="pill mnt">Maintainer</span><span class="mono">name</span><span class="n mono">1,617</span></div>
        <div class="node"><span class="pill adv">Advisory</span><span class="mono">osv_id, is_malware</span><span class="n mono">136</span></div>
        <div class="k" style="margin-top:14px">edges</div>
        <div class="node"><span class="mono">REQUIRED_BY</span><span class="n mono">91,544</span></div>
        <div class="node"><span class="mono">MAINTAINS / MAINTAINED_BY</span><span class="n mono">10,368</span></div>
        <div class="node"><span class="mono">AFFECTS / HAS_ADVISORY</span><span class="n mono">276</span></div>
        <div class="node"><span class="mono">SIMILAR_TO</span><span class="n mono">270</span></div>
        <div class="why">edges are stored <b>reversed</b> — (dependency)→(dependent) —
          because a variable-length MATCH needs a <b>fixed source id</b>, and in an
          incident the compromised package is the one thing you know.</div>
      </div>
    </div>
  </div>

  <div class="col">
    <div class="card">
      <div class="bar"><i class="dot" style="background:#e5837b"></i>
        <i class="dot" style="background:#e0bd76"></i>
        <i class="dot" style="background:#8fc79f"></i>
        <span class="title">sqlite sidecar — predicates</span></div>
      <div class="body">
        <div class="node"><span class="mono">declared semver ranges</span><span class="n mono">521,735</span></div>
        <div class="why">the only thing kept out of the graph, and it is <b>forced</b>:
          hydradb 0.1.0 cannot filter edge properties during a traversal, so a range
          stored on an edge would be unreadable exactly when it matters.</div>
      </div>
    </div>

    <div class="card">
      <div class="bar"><i class="dot" style="background:#e5837b"></i>
        <i class="dot" style="background:#e0bd76"></i>
        <i class="dot" style="background:#8fc79f"></i>
        <span class="title">live sources — no crawl needed</span></div>
      <div class="body">
        <div class="node"><span class="mono">osv.dev</span><span class="n">advisories · MAL- ids · CWE-506</span></div>
        <div class="node"><span class="mono">registry.npmjs.org</span><span class="n">versions · maintainers · tarballs</span></div>
        <div class="node"><span class="mono">replicate.npmjs.com</span><span class="n">live publishes</span></div>
        <div class="why">the lockfile audit needs <b>no graph coverage at all</b> — your
          lockfile is your tree — so it works on any project, not just the crawled 27k.</div>
      </div>
    </div>
  </div>
</div>

<div class="card chain">
  <div class="bar"><i class="dot" style="background:#e5837b"></i>
    <i class="dot" style="background:#e0bd76"></i>
    <i class="dot" style="background:#8fc79f"></i>
    <span class="title">traversals that chain across edge types — the queries a lookup table cannot answer</span></div>
  <div class="body" style="padding:10px 18px 14px">
    <div class="chainrow">
      <span class="box mono">Maintainer</span><span class="rel">—MAINTAINS→</span>
      <span class="box mono">Package</span><span class="rel">—REQUIRED_BY*1..4→</span>
      <span class="box mono">blast radius</span>
      <span class="out mono">qix controls 2 packages · 3,484 depend on them</span>
    </div>
    <div class="chainrow">
      <span class="box mono">Advisory</span><span class="rel">—AFFECTS→</span>
      <span class="box mono">Package</span><span class="rel">—REQUIRED_BY*→</span>
      <span class="box mono">everyone downstream</span>
      <span class="out mono">blast radius of a CVE, not a package</span>
    </div>
    <div class="chainrow">
      <span class="box mono">Package</span><span class="rel">—SIMILAR_TO→</span>
      <span class="box mono">impostor</span><span class="rel">—REQUIRED_BY*→</span>
      <span class="box mono">who already installed it</span>
      <span class="out mono">a typosquat with dependents is an incident</span>
    </div>
  </div>
</div>
"""

# --------------------------------------------------------------------------
# 2. social preview — what GitHub shows when the link is shared
# --------------------------------------------------------------------------

SOCIAL = """
<style>
  %(base)s
  body { width: 1280px; height: 640px; padding: 62px 70px; position: relative; overflow: hidden; }
  h1 { font-size: 92px; letter-spacing: -.055em; font-weight: 660; line-height: .9; }
  .tag { font-size: 25px; color: #55554f; margin-top: 16px; }
  .stats { display: flex; gap: 40px; margin-top: 46px; }
  .stat .v { font-family: ui-monospace, Consolas, monospace; font-size: 40px;
             font-weight: 600; letter-spacing: -.02em; }
  .stat .l { font-size: 13px; color: #8d8d86; margin-top: 3px; }
  .stat.bad .v { color: #c0392b; }
  .foot { position: absolute; left: 70px; bottom: 54px; display: flex; gap: 12px;
          align-items: center; font-size: 15px; color: #55554f; }
  .chip { border: 1px solid #d9d9d3; background: #fffffd; border-radius: 999px;
          padding: 6px 15px; font-size: 13.5px; }
  .rings { position: absolute; right: -46px; top: 50%%; transform: translateY(-50%%); }
</style>
<svg class="rings" width="620" height="620" viewBox="0 0 620 620">
  <circle cx="310" cy="310" r="60"  fill="none" stroke="#e6c9c5" stroke-width="1.5"/>
  <circle cx="310" cy="310" r="130" fill="none" stroke="#ecd6d3" stroke-width="1.5" stroke-dasharray="2 6"/>
  <circle cx="310" cy="310" r="200" fill="none" stroke="#efe0de" stroke-width="1.5" stroke-dasharray="2 6"/>
  <circle cx="310" cy="310" r="270" fill="none" stroke="#f2e8e7" stroke-width="1.5" stroke-dasharray="2 6"/>
  %(spokes)s
  <circle cx="310" cy="310" r="17" fill="#8f2318"/>
</svg>
<h1>blast radius</h1>
<div class="tag">find out who's poisoned before they do</div>
<div class="stats">
  <div class="stat"><div class="v">27,076</div><div class="l">packages in the graph</div></div>
  <div class="stat"><div class="v">102,464</div><div class="l">edges, six types</div></div>
  <div class="stat bad"><div class="v">3,650</div><div class="l">exposed by debug@4.4.2</div></div>
  <div class="stat"><div class="v">860ms</div><div class="l">depth-5 traversal, cold</div></div>
</div>
<div class="foot">
  <span class="chip">npm supply-chain incident response</span>
  <span class="chip">built on hydradb</span>
  <span class="chip">mit licensed</span>
</div>
"""


def spokes():
    """Nodes scattered on the rings — the shape of a blast radius."""
    import math
    out = []
    rings = [(130, 10, "#c0392b", 6.5), (200, 16, "#d4695c", 5.5),
             (270, 22, "#e0958c", 4.5)]
    for radius, count, colour, size in rings:
        for i in range(count):
            a = (i / count) * math.tau + radius * 0.017
            x = 310 + math.cos(a) * radius
            y = 310 + math.sin(a) * radius
            out.append(f'<line x1="310" y1="310" x2="{x:.1f}" y2="{y:.1f}" '
                       f'stroke="#eadedd" stroke-width="1"/>')
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{size}" fill="{colour}"/>')
    return "\n".join(out)


PAGES = [
    ("architecture.png", ARCH % {"base": BASE_CSS}, 1400, 790),
    ("social-preview.png", SOCIAL % {"base": BASE_CSS, "spokes": spokes()}, 1280, 640),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for name, html, w, h in PAGES:
            page = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=2).new_page()
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(400)
            page.screenshot(path=os.path.join(OUT, name))
            print(f"  {name}  {w}x{h}")
            page.close()
        browser.close()
    print(f"\nwrote to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
