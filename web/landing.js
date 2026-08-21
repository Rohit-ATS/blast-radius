/* blast radius — landing surfaces.
 *
 * Everything app.js does not own: the incident comparison chart, the stats
 * band, the clone-line copy button and the scroll reveal. app.js is untouched.
 *
 * The same rule applies here as everywhere else in this project — no number is
 * written into the page that did not come back from a query. If an endpoint
 * fails or a package is not in the graph yet, the row says so instead of
 * drawing a bar.
 */

'use strict';

(() => {
  const q  = (s, r = document) => r.querySelector(s);
  const qq = (s, r = document) => [...r.querySelectorAll(s)];
  const fmt = n => (typeof n === 'number' ? n.toLocaleString('en-US') : '—');

  const esc = s => String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const getJSON = async path => {
    const res = await fetch(path);
    const body = await res.json();
    if (!res.ok) throw Object.assign(new Error(body.message || body.error || `http ${res.status}`), { body });
    return body;
  };

  /* ------------------------------------------------------- incident chart */
  /* The reference layout puts a comparison chart beside the argument. Ours
     compares three documented compromises by the only figure that matters
     during one — how many packages the graph says are transitively exposed —
     and every bar is one live traversal, measured when the page loaded. */

  const INCIDENTS = [
    { name: 'debug',        version: '4.4.2',  note: 'qix account takeover · sep 2025',       color: '#d63a2f' },
    { name: 'event-stream', version: '3.3.6',  note: 'flatmap-stream backdoor · nov 2018',    color: '#0a0f1c' },
    { name: 'ua-parser-js', version: '0.7.29', note: 'cryptominer · oct 2021',                color: '#2f6bff' },
  ];

  async function drawIncidentChart() {
    const host = q('#cmpchart');
    const tag  = q('#shifttag');
    if (!host) return;

    const rows = [];
    for (const inc of INCIDENTS) {
      try {
        const r = await getJSON(`/api/blast?name=${encodeURIComponent(inc.name)}&depth=5`);
        rows.push({ ...inc, total: r.total, ms: r.latency_ms, known: true });
      } catch (e) {
        // `not_yet` — the crawler has not reached this package. Say that.
        rows.push({ ...inc, known: false, why: (e.body && e.body.message) || e.message });
      }
    }

    const measured = rows.filter(r => r.known);
    if (!measured.length) {
      host.innerHTML = `<div class="cmprow skel">the graph could not be reached — ${esc(rows[0].why || 'no response')}</div>`;
      if (tag) { tag.textContent = 'unavailable'; tag.style.cssText = 'background:#fdeeed;border-color:#f6d3cf;color:#d63a2f'; }
      return;
    }

    const max = Math.max(...measured.map(r => r.total)) || 1;

    host.innerHTML = rows.map(r => {
      if (!r.known) {
        return `<div class="cmpitem">
          <div class="cmptrack">
            <div class="cmpbar flat" style="width:44%">${esc(r.name)}@${esc(r.version)}</div>
            <span class="cmpn muted">not in the graph yet</span>
          </div>
          <div class="cmpmeta">${esc(r.note)}</div>
        </div>`;
      }
      // The count lives outside the bar so a short bar never clips its own number.
      const pct = Math.max(22, Math.round((r.total / max) * 84));
      return `<div class="cmpitem">
        <div class="cmptrack">
          <div class="cmpbar" data-w="${pct}" style="background:${r.color}">${esc(r.name)}@${esc(r.version)}</div>
          <span class="cmpn">${fmt(r.total)}</span>
        </div>
        <div class="cmpmeta">${esc(r.note)} · ${r.ms}ms</div>
      </div>`;
    }).join('');

    // width is animated in after layout so the bars grow rather than appear
    requestAnimationFrame(() => {
      qq('.cmpbar[data-w]', host).forEach(b => { b.style.width = b.dataset.w + '%'; });
    });

    if (tag) {
      const worst = measured.reduce((a, b) => (b.total > a.total ? b : a));
      tag.textContent = `${fmt(worst.total)} exposed · worst of ${measured.length}`;
    }
    const quote = q('#shiftquote');
    if (quote) {
      const totalMs = measured.reduce((a, b) => a + b.ms, 0);
      quote.textContent =
        `${measured.length} traversals, ${Math.round(totalMs)}ms of database time, run against the graph behind this page when you loaded it.`;
    }
  }

  /* ---------------------------------------------------------- stats band */

  async function fillBand() {
    const cells = {
      pkgs:    q('#b-pkgs'),
      edges:   q('#b-edges'),
      crawled: q('#b-crawled'),
      latency: q('#b-latency'),
    };
    if (!cells.pkgs) return;
    try {
      const s = await getJSON('/api/stats');
      cells.pkgs.textContent    = fmt(s.packages);
      cells.edges.textContent   = fmt(s.edges);
      cells.crawled.textContent = fmt(s.crawl && s.crawl.crawled);
      cells.latency.textContent = `${s.latency_ms}ms`;
    } catch {
      Object.values(cells).forEach(c => { if (c) c.textContent = 'n/a'; });
    }
  }

  /* -------------------------------------------------------- clone copying */

  function wireCopy() {
    qq('[data-clip]').forEach(btn => {
      btn.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(btn.dataset.clip); }
        catch { return; }
        const was = btn.textContent;
        btn.textContent = 'Copied';
        btn.classList.add('done');
        setTimeout(() => { btn.textContent = was; btn.classList.remove('done'); }, 1600);
      });
    });
  }

  /* -------------------------------------------------------- scroll reveal */

  function wireReveal() {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    if (!('IntersectionObserver' in window)) return;

    const targets = qq('.sechead, .featgrid > .feat, .panel, .splitleft, .bandcell, .plan, .planaside > .asidecard, .logostrip');
    const io = new IntersectionObserver((entries, obs) => {
      entries.forEach(e => {
        if (!e.isIntersecting) return;
        e.target.classList.add('in');
        obs.unobserve(e.target);
      });
    }, { rootMargin: '0px 0px -12% 0px', threshold: .08 });

    targets.forEach((t, i) => {
      t.classList.add('reveal');
      t.style.transitionDelay = `${Math.min(i % 4, 3) * 70}ms`;
      io.observe(t);
    });
  }

  /* ------------------------------------------------------------- topbar */
  /* The bar gains its border and shadow only once the hero has scrolled
     under it, so the landing opens flush with the gradient. */

  function wireTopbar() {
    const bar = q('.topbar');
    if (!bar) return;
    const paint = () => bar.classList.toggle('scrolled', window.scrollY > 12);
    paint();
    addEventListener('scroll', paint, { passive: true });
  }

  wireCopy();
  wireReveal();
  wireTopbar();
  fillBand();
  drawIncidentChart();
})();
