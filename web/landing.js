/* blast radius — landing surfaces.
 *
 * Everything app.js does not own: the incident comparison chart, the console
 * dashboard, the semver resolution card, the incident timeline, the reach
 * field, the live publish exchange, the depth curves and the stats band.
 * app.js is untouched and still owns the query form, the results grid, the
 * map, the ticker, the explorer and both drop zones.
 *
 * The project rule holds here too: no number reaches the page that did not
 * come back from a query. When an endpoint fails or a package is not in the
 * graph yet, the surface says so instead of drawing something.
 */

'use strict';

(() => {
  const q   = (s, r = document) => r.querySelector(s);
  const qq  = (s, r = document) => [...r.querySelectorAll(s)];
  const fmt = n => (typeof n === 'number' ? n.toLocaleString('en-US') : '—');
  const esc = s => String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const getJSON = async path => {
    const res = await fetch(path);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(body.message || body.error || `http ${res.status}`), { body });
    return body;
  };

  const INCIDENTS = [
    { name: 'debug',        version: '4.4.2',  note: 'qix account takeover · sep 2025',    color: '#d63a2f' },
    { name: 'event-stream', version: '3.3.6',  note: 'flatmap-stream backdoor · nov 2018', color: '#0b0f19' },
    { name: 'ua-parser-js', version: '0.7.29', note: 'cryptominer · oct 2021',             color: '#2f6bff' },
  ];

  /* =================================================== 2 · incident bars */

  async function incidentBars() {
    const host = q('#cmpchart');
    const tag  = q('#shifttag');
    if (!host) return [];

    const rows = [];
    for (const inc of INCIDENTS) {
      try {
        const r = await getJSON(`/api/blast?name=${encodeURIComponent(inc.name)}&depth=5`);
        rows.push({ ...inc, total: r.total, ms: r.latency_ms, hist: r.histogram || [], known: true });
      } catch (e) {
        rows.push({ ...inc, known: false, why: (e.body && e.body.message) || e.message });
      }
    }

    const seen = rows.filter(r => r.known);
    if (!seen.length) {
      host.innerHTML = `<div class="skel">the graph could not be reached — ${esc(rows[0].why || 'no response')}</div>`;
      if (tag) tag.textContent = 'unavailable';
      return rows;
    }

    const max = Math.max(...seen.map(r => r.total)) || 1;

    host.innerHTML = rows.map(r => {
      if (!r.known) {
        return `<div><div class="bar-row">
            <div class="bar-fill" style="background:#eef1f6;color:#939cab;width:34%">${esc(r.name)}</div>
            <span class="bar-out">not in the graph yet</span>
          </div><p class="bar-meta">${esc(r.note)}</p></div>`;
      }
      // bars top out at 84% so the count beside them never collides with the edge
      const pct   = Math.max(20, Math.round((r.total / max) * 84));
      const label = `${esc(r.name)}@${esc(r.version)}`;
      const wide  = pct >= 36;
      return `<div><div class="bar-row">
          <div class="bar-fill" data-w="${pct}" style="background:${r.color}">${wide ? label : ''}</div>
          ${wide ? '' : `<span class="bar-out">${label}</span>`}
          <span class="bar-n">${fmt(r.total)}</span>
        </div><p class="bar-meta">${esc(r.note)} · ${r.ms}ms</p></div>`;
    }).join('');

    requestAnimationFrame(() => {
      qq('.bar-fill[data-w]', host).forEach(b => { b.style.width = b.dataset.w + '%'; });
    });

    if (tag) {
      const worst = seen.reduce((a, b) => (b.total > a.total ? b : a));
      tag.innerHTML = tag.innerHTML.replace(/measuring…|[\d,]+ exposed.*$/, '') +
        `${fmt(worst.total)} exposed at worst`;
    }
    const quote = q('#shiftquote');
    if (quote) {
      const ms = Math.round(seen.reduce((a, b) => a + b.ms, 0));
      quote.textContent =
        `${seen.length} traversals and ${ms}ms of database time, run against the graph behind this page the moment you loaded it.`;
    }
    return rows;
  }

  /* ======================================================= 3 · dashboard */

  let current = INCIDENTS[0];

  async function dashboard(inc) {
    current = inc;
    const title = q('#dashtitle');
    if (title) title.textContent = `${inc.name}@${inc.version} exposure`;

    const rows = q('#dashrows');
    if (rows) rows.innerHTML = `<tr><td class="empty" colspan="5">walking the graph…</td></tr>`;
    ['#k-exposed', '#k-queries', '#k-latency'].forEach(s => { const e = q(s); if (e) e.textContent = '…'; });

    let blast;
    try {
      blast = await getJSON(`/api/blast?name=${encodeURIComponent(inc.name)}&depth=5`);
    } catch (e) {
      const why = (e.body && e.body.message) || e.message;
      if (rows) rows.innerHTML = `<tr><td class="empty" colspan="5">${esc(why)}</td></tr>`;
      ['#k-exposed', '#k-queries', '#k-latency'].forEach(s => { const el = q(s); if (el) el.textContent = 'n/a'; });
      return;
    }

    q('#k-exposed').textContent   = fmt(blast.total);
    q('#k-exposed-d').textContent = `depth ${blast.depth} closure`;
    q('#k-queries').textContent   = fmt(blast.queries);
    q('#k-latency').textContent   = `${Math.round(blast.latency_ms)}ms`;

    // the table wants per-package depth and dependent counts, which is exactly
    // what the subgraph endpoint returns
    try {
      const sub = await getJSON(`/api/subgraph?name=${encodeURIComponent(inc.name)}&depth=3&limit=90`);
      const nodes = (sub.nodes || []).filter(n => n.depth > 0);
      const byName = Object.fromEntries((sub.nodes || []).map(n => [n.name, n]));
      const parent = {};
      (sub.edges || []).forEach(e => {
        const from = e.from ?? e.source, to = e.to ?? e.target;
        if (to && !parent[to]) parent[to] = from;
      });

      const top = nodes.sort((a, b) => (b.dependents || 0) - (a.dependents || 0)).slice(0, 6);
      const peak = Math.max(...top.map(n => n.dependents || 0), 1);

      rows.innerHTML = top.length ? top.map(n => {
        const via = parent[n.name] && byName[parent[n.name]] ? parent[n.name] : inc.name;
        const pct = Math.max(4, Math.round(((n.dependents || 0) / peak) * 100));
        return `<tr>
          <td class="name">${esc(n.name)}</td>
          <td><span class="depth-tag d${n.depth}">depth ${n.depth}</span></td>
          <td class="via">${esc(via)}</td>
          <td class="num">${fmt(n.dependents || 0)}</td>
          <td><div class="reach"><span class="rt"><i class="rf" style="width:${pct}%"></i></span>
              <span class="rn">${pct}%</span></div></td>
        </tr>`;
      }).join('') : `<tr><td class="empty" colspan="5">nothing downstream of ${esc(inc.name)} in the crawled graph</td></tr>`;
    } catch (e) {
      rows.innerHTML = `<tr><td class="empty" colspan="5">${esc(e.message)}</td></tr>`;
    }

    reachField(blast.total);
    resolution(inc);
    timeline(inc, blast);
  }

  /* ================================================== 4 · semver + timeline */

  async function resolution(inc) {
    const bad = q('#res-bad'), pin = q('#res-pin');
    const hot = q('#res-hot'), safe = q('#res-safe');
    const meter = q('#res-meter');
    if (!bad) return;

    bad.textContent = pin.textContent = '…';
    try {
      const r = await getJSON(
        `/api/resolve?name=${encodeURIComponent(inc.name)}&bad_version=${encodeURIComponent(inc.version)}`);

      const wouldPull = r.exposed_count || 0;
      const shielded  = r.shielded_count || 0;
      const total = wouldPull + shielded || 1;

      bad.textContent = fmt(wouldPull);
      pin.textContent = fmt(shielded);
      meter.querySelector('.m1').style.width = `${(wouldPull / total) * 100}%`;
      meter.querySelector('.m2').style.width = `${(shielded / total) * 100}%`;

      // the declared ranges that admitted the bad version, straight off the row
      const ranges = [];
      (r.exposed || []).forEach(e => (e.ranges || []).forEach(x => {
        if (!ranges.includes(x)) ranges.push(x);
      }));

      hot.innerHTML = ranges.length
        ? ranges.slice(0, 4).map(x => `<span class="tag hot">${esc(x)}</span>`).join('')
        : `<span class="tag hot">${fmt(wouldPull)} ranges</span>`;
      safe.innerHTML =
        `<span class="tag">${fmt(shielded)} of ${fmt(r.checked || total)} checked</span>` +
        `<span class="tag">exact pins</span>`;
    } catch (e) {
      bad.textContent = pin.textContent = 'n/a';
      hot.innerHTML = `<span class="tag">${esc((e.body && e.body.message) || e.message)}</span>`;
      safe.innerHTML = '';
    }
  }

  function timeline(inc, blast) {
    const host = q('#timeline');
    if (!host) return;
    const cov = blast.graph_coverage || {};
    const steps = [
      { ico: 'i-search', when: `${blast.lookup_ms}ms`, what: `Resolved <b>${esc(inc.name)}</b> to vertex ${blast.vertex_id}`, cls: 'done' },
      { ico: 'i-radius', when: `${Math.round(blast.latency_ms)}ms`, what: `Walked ${fmt(blast.queries)} queries to depth ${blast.depth}`, cls: 'done' },
      { ico: 'i-graph',  when: `${fmt(blast.total)} found`, what: `Closure over ${fmt(cov.packages_in_graph)} crawled packages`, cls: 'done' },
      { ico: blast.truncated ? 'i-alert' : 'i-check',
        when: blast.source || 'hydradb',
        what: blast.truncated ? 'Result truncated at the row limit' : 'Full closure returned, nothing truncated',
        cls: blast.truncated ? 'warn' : 'done' },
      { ico: 'i-lock', when: 'next', what: 'Drop a lockfile below to see the path that reaches you', cls: '' },
    ];
    host.innerHTML = steps.map(s => `
      <div class="tl-row ${s.cls}">
        <span class="tl-ico"><svg viewBox="0 0 24 24"><use href="#${s.ico}"/></svg></span>
        <div><div class="tl-when">${esc(s.when)}</div><div class="tl-what">${s.what}</div></div>
      </div>`).join('');
  }

  /* ================================================== 5 · reach + publishes */

  /* A scatter of the exposed set: one dot per package, denser toward the
     centre, sized so the field reads as a quantity rather than a decoration. */
  function reachField(total) {
    const host = q('#reachfield');
    if (!host) return;
    const svg = host.querySelector('svg');
    q('#reachnum').textContent = fmt(total);
    q('#reachtxt').textContent = total === 1 ? 'package in range' : 'packages in range';

    const W = 620, H = 300, CX = W / 2, CY = H / 2;
    const dots = Math.max(120, Math.min(620, Math.round(total / 6)));
    let out = '';
    for (let i = 0; i < dots; i++) {
      // golden-angle spiral keeps the density even without random jitter
      const t = i / dots;
      const a = i * 2.39996;
      const rr = Math.sqrt(t);
      const x = CX + Math.cos(a) * rr * (W * .49);
      const y = CY + Math.sin(a) * rr * (H * .49);
      const op = (.72 - t * .46).toFixed(3);
      const rad = (3.1 - t * 1.5).toFixed(2);
      const fill = t < .18 ? '#d63a2f' : t < .5 ? '#5b8bff' : '#b3bac6';
      out += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${rad}" fill="${fill}" opacity="${op}"/>`;
    }
    out += `<circle cx="${CX}" cy="${CY}" r="58" fill="none" stroke="#d5e1ff" stroke-dasharray="3 6"/>`;
    out += `<circle cx="${CX}" cy="${CY}" r="104" fill="none" stroke="#eef1f6" stroke-dasharray="3 6"/>`;
    svg.innerHTML = out;
  }

  async function publishes() {
    const when = q('#pub-when'), qb = q('#pub-q'), ab = q('#pub-a');
    if (!when) return;
    try {
      const f = await getJSON('/api/feed?limit=8');
      const events = f.events || [];
      // prefer something the graph actually has an opinion about
      const e = events.find(x => x.in_graph) || events[0];
      if (!e) { when.textContent = 'no publishes in the last window'; return; }

      const t = new Date(e.published);
      when.textContent = t.toLocaleString('en-US',
        { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
      qb.textContent = `${e.name}@${e.version} just published`;

      if (e.advisories && e.advisories.length) {
        ab.textContent = `${e.advisories.length} advisory match on this name — treat as malicious until checked.`;
      } else if (e.in_graph) {
        ab.textContent = `In the graph with ${fmt(e.dependents || 0)} dependents. ` +
          (e.level === 'high' ? 'High reach — worth watching this one.'
                              : 'Reach is low enough that a bad version stays contained.');
      } else {
        ab.textContent = 'Not in the crawled graph — nothing downstream depends on it yet.';
      }
    } catch {
      when.textContent = 'the publish feed is not answering right now';
    }
  }

  /* ================================================ 6 · band, 7 · the curves */

  async function band() {
    const cells = { p: q('#b-pkgs'), e: q('#b-edges'), c: q('#b-crawled') };
    if (!cells.p) return;
    try {
      const s = await getJSON('/api/stats');
      cells.p.textContent = fmt(s.packages);
      cells.e.textContent = fmt(s.edges);
      cells.c.textContent = fmt(s.crawl && s.crawl.crawled);
    } catch {
      Object.values(cells).forEach(c => c && (c.textContent = 'n/a'));
    }
  }

  /* Cumulative exposure by depth, one curve per incident, drawn as a filled
     area exactly like the reference's analytics panel — except the shape here
     is the real histogram each traversal returned. */
  function curves(rows) {
    const host = q('#curve');
    if (!host) return;
    const svg = host.querySelector('svg');
    const legend = q('#curvelegend');

    const series = rows.filter(r => r.known && r.hist && r.hist.length).map(r => {
      let run = 0;
      const pts = [{ d: 0, v: 0 }];
      r.hist.forEach(h => { run += h.packages || 0; pts.push({ d: h.depth, v: run }); });
      return { name: `${r.name}@${r.version}`, color: r.color, pts };
    });
    if (!series.length) { host.style.display = 'none'; return; }

    const W = 1000, H = 380, PAD = 6;
    const maxD = Math.max(...series.flatMap(s => s.pts.map(p => p.d)), 1);
    const maxV = Math.max(...series.flatMap(s => s.pts.map(p => p.v)), 1);
    const X = d => (d / maxD) * (W - PAD * 2) + PAD;
    // sqrt keeps the small incidents visible next to a 3,600-package one
    const Y = v => H - Math.sqrt(v / maxV) * (H - 40) - 4;

    // a smooth path through the points, so the curves read like the reference
    const smooth = pts => {
      let d = `M ${X(pts[0].d)} ${Y(pts[0].v)}`;
      for (let i = 1; i < pts.length; i++) {
        const p0 = pts[i - 1], p1 = pts[i];
        const cx = (X(p0.d) + X(p1.d)) / 2;
        d += ` C ${cx} ${Y(p0.v)}, ${cx} ${Y(p1.v)}, ${X(p1.d)} ${Y(p1.v)}`;
      }
      return d;
    };

    let defs = '', body = '';
    // vertical gridline per depth
    for (let d = 1; d <= maxD; d++) {
      body += `<line x1="${X(d)}" y1="0" x2="${X(d)}" y2="${H}" stroke="#f1f4f9" stroke-width="1"/>`;
    }
    series.forEach((s, i) => {
      const id = `g${i}`;
      defs += `<linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
                 <stop offset="0%" stop-color="${s.color}" stop-opacity=".22"/>
                 <stop offset="100%" stop-color="${s.color}" stop-opacity="0"/>
               </linearGradient>`;
      const line = smooth(s.pts);
      body += `<path d="${line} L ${X(maxD)} ${H} L ${X(0)} ${H} Z" fill="url(#${id})"/>`;
      body += `<path d="${line}" fill="none" stroke="${s.color}" stroke-width="2.4"
                     stroke-linecap="round" stroke-linejoin="round"/>`;
    });

    svg.innerHTML = `<defs>${defs}</defs>${body}`;
    if (legend) {
      legend.innerHTML = series.map(s =>
        `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join('') +
        `<span style="color:#939cab">cumulative packages exposed · depth 1 → ${maxD}</span>`;
    }
  }

  /* ============================================================ chrome bits */

  function copyables() {
    qq('[data-clip]').forEach(btn => {
      btn.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(btn.dataset.clip); } catch { return; }
        const was = btn.textContent;
        btn.textContent = 'Copied';
        btn.classList.add('done');
        setTimeout(() => { btn.textContent = was; btn.classList.remove('done'); }, 1600);
      });
    });
  }

  function stickyHeader() {
    const bar = q('.hdr');
    if (!bar) return;
    const paint = () => bar.classList.toggle('stuck', window.scrollY > 12);
    paint();
    addEventListener('scroll', paint, { passive: true });
  }

  function reveal() {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    if (!('IntersectionObserver' in window)) return;
    const targets = qq('.head-split, .head-center, .two > *, .console-card, .four > div, ' +
                       '.duo-card, .split, .band-cell, .chart-card, .plan, .plan-side > *');
    const io = new IntersectionObserver((es, obs) => {
      es.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); obs.unobserve(e.target); } });
    }, { rootMargin: '0px 0px -10% 0px', threshold: .06 });
    targets.forEach((t, i) => {
      t.classList.add('rv');
      t.style.transitionDelay = `${Math.min(i % 4, 3) * 70}ms`;
      io.observe(t);
    });
  }

  /* The incident chips are also app.js's `.chip[data-pkg]` buttons, so app.js
     runs the full query while this repaints the dashboard around it. */
  function wireIncidents() {
    qq('.prompt[data-pkg]').forEach(btn => {
      btn.addEventListener('click', () => {
        qq('.prompt[data-pkg]').forEach(b => b.classList.toggle('on', b === btn));
        dashboard({
          name: btn.dataset.pkg,
          version: btn.dataset.ver,
          color: (INCIDENTS.find(i => i.name === btn.dataset.pkg) || INCIDENTS[0]).color,
        });
      });
    });
  }

  /* ================================================================= boot */

  copyables();
  stickyHeader();
  reveal();
  wireIncidents();
  band();
  publishes();
  dashboard(INCIDENTS[0]);
  incidentBars().then(curves);
})();
