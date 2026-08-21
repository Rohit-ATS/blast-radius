/* blast radius — console.
 *
 * No framework, no build step, no localStorage. Every number rendered here
 * arrives from an endpoint that measured a real query; when a call fails or a
 * package is not in the graph yet, this says so rather than drawing a zero.
 */

'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const DEPTH = 5;
const num = n => (typeof n === 'number' ? n.toLocaleString('en-US') : String(n ?? '—'));

/* A cold HydraDB exceeds its own 30s query timeout on deep traversals for up
 * to a minute and a half after a restart. That is a real state, not an error
 * worth showing someone — so a `graph_warming` 503 is retried here, with the
 * page saying what it is waiting for rather than flashing a failure. */
const WARM_RETRIES = 4;

async function api(path, opts, onWarming) {
  for (let attempt = 0; ; attempt++) {
    const res = await fetch(path, opts);
    let body;
    try { body = await res.json(); }
    catch { throw Object.assign(new Error('server sent a non-JSON response'), { status: res.status }); }
    if (res.ok) return body;

    const warming = res.status === 503 && body.error === 'graph_warming';
    if (warming && attempt < WARM_RETRIES) {
      if (onWarming) onWarming(body, attempt);
      await new Promise(r => setTimeout(r, 2000 * (attempt + 1)));
      continue;
    }
    throw Object.assign(new Error(body.message || body.error || `http ${res.status}`),
                        { status: res.status, payload: body, warming });
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------------------------------------------------------------- dragging */
/* The signature interaction, so it is done with pointer capture and a direct
 * transform write — no transition on transform, no rAF queue, no library. */

let zTop = 10;

function draggable(el, handle) {
  let x = +(el.dataset.x || 0);
  let y = +(el.dataset.y || 0);
  const rot = +(el.dataset.rot || 0);
  const paint = () => {
    el.style.transform = `translate3d(${x}px, ${y}px, 0) rotate(${rot}deg)`;
  };
  paint();

  handle.addEventListener('pointerdown', down);

  function down(e) {
    if (e.button !== 0) return;
    // Let the caret land normally when someone clicks into a field.
    if (e.target.closest('input, textarea, button, a')) return;
    const sx = e.clientX, sy = e.clientY, ox = x, oy = y;
    el.classList.add('dragging');
    el.style.zIndex = ++zTop;
    handle.setPointerCapture(e.pointerId);

    const move = ev => { x = ox + (ev.clientX - sx); y = oy + (ev.clientY - sy); paint(); };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      el.classList.remove('dragging');
      try { handle.releasePointerCapture(e.pointerId); } catch { /* already gone */ }
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up, { once: true });
    handle.addEventListener('pointercancel', up, { once: true });
    e.preventDefault();
  }
}

function wireDragging(root = document) {
  $$('.win', root).forEach(w => draggable(w, $('.bar', w) || w));
  $$('.sticker', root).forEach(s => draggable(s, s));
}

/* ------------------------------------------------------------- live stats */

let lastStats = null;

async function pollStats() {
  const pulse = $('#pulse'), line = $('#statline');
  try {
    const s = await api('/api/stats');
    lastStats = s;
    pulse.className = 'pulse live';
    const bits = [`${num(s.packages)} packages`, `${num(s.edges)} edges`];
    if (s.crawl?.running) bits.push(`crawling · ${num(s.crawl.crawled)} done`);
    line.textContent = bits.join(' · ');
  } catch (err) {
    pulse.className = 'pulse dead';
    line.textContent = err.warming ? 'hydradb warming up…'
                     : err.status === 503 ? 'hydradb unreachable'
                     : 'stats unavailable';
  }
}

/* ---------------------------------------------------- hero preview windows */

async function loadPeeks() {
  const seed = 'debug';

  api(`/api/blast?name=${encodeURIComponent(seed)}&depth=4`).then(b => {
    const max = Math.max(1, ...b.histogram.map(h => h.packages));
    $('#peek-hist').innerHTML =
      `<div style="color:var(--ink-3);margin-bottom:7px">${esc(seed)} · ${num(b.total)} exposed</div>` +
      b.histogram.map(h => `
        <div style="display:grid;grid-template-columns:52px 1fr 38px;gap:8px;align-items:center;margin-bottom:5px">
          <span style="font-size:11px;color:var(--ink-3)">depth ${h.depth}</span>
          <span style="background:var(--line-2);border-radius:3px;height:11px;overflow:hidden">
            <span style="display:block;height:100%;width:${(h.packages / max) * 100}%;background:var(--red);border-radius:3px"></span>
          </span>
          <span style="font-size:11px;text-align:right">${num(h.packages)}</span>
        </div>`).join('') +
      `<div style="color:var(--ink-3);margin-top:7px">${b.latency_ms}ms</div>`;
  }).catch(e => { $('#peek-hist').innerHTML = `<div class="skel">${esc(e.message)}</div>`; });

  api(`/api/maintainers?name=${encodeURIComponent(seed)}`).then(m => {
    const rows = m.also_controls.slice(0, 4);
    $('#peek-maint').innerHTML =
      `<div style="color:var(--ink-3);margin-bottom:7px">${esc(seed)} → ${m.maintainers.map(esc).join(', ') || 'unknown'}</div>` +
      (rows.length
        ? rows.map(r => `<div style="display:flex;gap:8px;margin-bottom:4px">
             <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.package)}</span>
             <span style="margin-left:auto;color:var(--ink-3)">${num(r.direct_dependents)}</span></div>`).join('')
        : '<div class="skel">no siblings recorded yet</div>') +
      `<div style="color:var(--ink-3);margin-top:7px">+${num(Math.max(0, m.sibling_count - rows.length))} more · ${m.latency_ms}ms</div>`;
  }).catch(e => { $('#peek-maint').innerHTML = `<div class="skel">${esc(e.message)}</div>`; });

  api('/api/stats').then(s => {
    $('#peek-graph').innerHTML = `
      <div style="display:flex;justify-content:space-between"><span style="color:var(--ink-3)">packages</span><b>${num(s.packages)}</b></div>
      <div style="display:flex;justify-content:space-between"><span style="color:var(--ink-3)">edges</span><b>${num(s.edges)}</b></div>
      <div style="display:flex;justify-content:space-between"><span style="color:var(--ink-3)">crawl</span><b>${s.crawl?.running ? 'running' : 'idle'}</b></div>
      <div style="display:flex;justify-content:space-between"><span style="color:var(--ink-3)">sidecar read</span><b>${s.latency_ms}ms</b></div>
      ${s.graph ? `<div style="display:flex;justify-content:space-between"><span style="color:var(--ink-3)">graph count(*)</span><b>${s.graph.measured_ms != null ? Math.round(s.graph.measured_ms) + 'ms' : 'n/a'}</b></div>` : ''}`;
  }).catch(e => { $('#peek-graph').innerHTML = `<div class="skel">${esc(e.message)}</div>`; });
}

/* --------------------------------------------------------------- autocomplete */

let suggestTimer = null, suggestIndex = -1;

function closeSuggest() {
  // Called from the query handlers and the reset button, both of which are
  // shared by pages that may not carry the autocomplete box.
  const box = $('#suggest');
  if (!box) return;
  box.hidden = true;
  box.innerHTML = '';
  suggestIndex = -1;
}

function wireSuggest() {
  const input = $('#pkg'), box = $('#suggest');
  if (!input || !box) return;

  input.addEventListener('input', () => {
    clearTimeout(suggestTimer);
    const q = input.value.trim();
    if (q.length < 2) return closeSuggest();
    suggestTimer = setTimeout(async () => {
      try {
        const { results } = await api(`/api/search?q=${encodeURIComponent(q)}`);
        if (!results.length) return closeSuggest();
        box.innerHTML = results.map(r =>
          `<button type="button" data-name="${esc(r.name)}">${esc(r.name)}
             <span class="v">${r.latest ? esc(r.latest) : (r.crawled ? '' : 'not crawled yet')}</span>
           </button>`).join('');
        box.hidden = false;
        suggestIndex = -1;
      } catch { closeSuggest(); }
    }, 110);
  });

  box.addEventListener('click', e => {
    const b = e.target.closest('button[data-name]');
    if (!b) return;
    input.value = b.dataset.name;
    closeSuggest();
    $('#ver').focus();
  });

  input.addEventListener('keydown', e => {
    const items = $$('button', box);
    if (box.hidden || !items.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      suggestIndex = (suggestIndex + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items.forEach((it, i) => it.classList.toggle('on', i === suggestIndex));
      items[suggestIndex].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && suggestIndex >= 0) {
      e.preventDefault();
      input.value = items[suggestIndex].dataset.name;
      closeSuggest();
    } else if (e.key === 'Escape') {
      closeSuggest();
    }
  });

  document.addEventListener('click', e => {
    if (!e.target.closest('.query')) closeSuggest();
  });
}

/* -------------------------------------------------------------- the query */

function errorBox(where, err) {
  const detail = err.payload?.message || err.message;
  const title = err.warming ? 'graph is still warming up'
              : err.status === 404 ? 'not in the graph'
              : err.status === 503 ? 'hydradb unreachable'
              : 'query failed';
  where.innerHTML = `<div class="errbox"><b>${title}</b>${esc(detail)}</div>`;
}

function renderHistogram(hist) {
  const box = $('#hist');
  const max = Math.max(1, ...hist.map(h => h.packages));
  box.innerHTML = hist.map(h => `
    <div class="hrow">
      <span class="lbl">depth ${h.depth}</span>
      <span class="track"><span class="fill${h.packages ? '' : ' zero'}" data-w="${(h.packages / max) * 100}"></span></span>
      <span class="num">${num(h.packages)} <small>pkg</small></span>
    </div>`).join('');
  // Paint at zero, then let the transition run on the next frame.
  requestAnimationFrame(() => {
    $$('.fill', box).forEach(f => { f.style.width = f.dataset.w + '%'; });
  });
}

function renderVictims(b) {
  const box = $('#victims');
  if (!b.victims.length) {
    box.innerHTML = `<div class="empty">nothing depends on ${esc(b.name)} within depth ${b.depth}.</div>`;
    return;
  }
  box.innerHTML = b.victims.map(v => `<div class="r"><span>${esc(v)}</span></div>`).join('') +
    (b.truncated ? `<div class="r"><span class="g">list truncated at ${num(b.victims.length)} — the count above is complete</span></div>` : '');
}

async function renderSemver(name, version) {
  const box = $('#semver');
  if (!version) {
    box.innerHTML = `<div class="note">add the malicious version above and this becomes the
      honest number: not who <i>lists</i> ${esc(name)}, but whose declared range would actually
      have resolved to it.</div>`;
    return;
  }
  box.innerHTML = '<div class="skel">checking every declared range…</div>';
  try {
    const r = await api(`/api/resolve?name=${encodeURIComponent(name)}&bad_version=${encodeURIComponent(version)}`);
    const sample = r.exposed.slice(0, 6);
    box.innerHTML = `
      <div class="split">
        <div class="stat bad"><div class="n">${num(r.exposed_count)}</div><div class="t">would have pulled ${esc(version)}</div></div>
        <div class="stat pin"><div class="n">${num(r.shielded_count)}</div><div class="t">shielded by a pin</div></div>
      </div>
      <div class="note">checked ${num(r.checked)} declared ranges across every crawled release.
        ${r.exposed_count === 0 && r.shielded_count === 0
          ? 'no crawled package declares a dependency on this one yet.'
          : `${sample.length ? 'e.g. ' + sample.map(s => `${esc(s.name)} <span class="tagpill bad">${esc(s.ranges[0])}</span>`).join(', ') : ''}`}
      </div>`;
  } catch (err) { errorBox(box, err); }
}

async function renderMaintainers(name) {
  const box = $('#maint');
  box.innerHTML = '<div class="empty">looking up ownership…</div>';
  try {
    const m = await api(`/api/maintainers?name=${encodeURIComponent(name)}`);
    if (!m.also_controls.length) {
      box.innerHTML = `<div class="empty">${esc(m.message || `no other packages recorded for ${(m.maintainers || []).join(', ') || 'these maintainers'} yet.`)}</div>`;
      return;
    }
    box.innerHTML =
      `<div class="r"><span class="g" style="margin:0">maintainers: ${m.maintainers.map(esc).join(', ')}</span></div>` +
      m.also_controls.map(s => `<div class="r"><span>${esc(s.package)}</span>
        <span class="g">${num(s.direct_dependents)} direct dependents</span></div>`).join('');
  } catch (err) { errorBox(box, err); }
}

let running = false;

async function runQuery(name, version) {
  if (running) return;
  running = true;
  const btn = $('#go');
  btn.disabled = true;
  btn.textContent = 'querying…';

  const results = $('#results');
  results.hidden = false;
  $('#verdictline').textContent = `traversing REQUIRED_BY from ${name} …`;
  $('#latency').textContent = '—';
  $('#latencysub').textContent = '';
  $('#hist').innerHTML = '<div class="skel">…</div>';
  $('#victims').innerHTML = '<div class="empty">…</div>';

  const t0 = performance.now();
  try {
    // Ask for the graph size alongside the traversal rather than trusting the
    // background poll to have landed — printing "0 edges" because a number has
    // not arrived yet is exactly the kind of invented figure this page avoids.
    const onWarming = (body, attempt) => {
      $('#verdictline').textContent =
        `hydradb is warming its cache — retrying (${attempt + 1}/${WARM_RETRIES})…`;
    };
    const [b, statsSettled] = await Promise.all([
      api(`/api/blast?name=${encodeURIComponent(name)}&depth=${DEPTH}`, undefined, onWarming),
      api('/api/stats').then(s => (lastStats = s)).catch(() => null),
    ]);
    const rtt = performance.now() - t0;

    $('#latency').textContent = `${Math.round(b.latency_ms)}ms`;
    const parts = [
      `depth ${b.depth}`,
      `${num(b.total)} packages exposed`,
      `${b.queries ?? DEPTH + 1} hydradb queries in parallel`,
    ];
    if (statsSettled?.edges != null) parts.push(`${num(statsSettled.edges)} edges in graph`);
    parts.push(`${Math.round(rtt)}ms round trip`);
    $('#latencysub').textContent = parts.join(' · ');
    $('#verdictline').innerHTML =
      `<b>${esc(name)}</b>${version ? '@' + esc(version) : ''} — ${num(b.total)} packages transitively depend on it`;

    renderHistogram(b.histogram);
    renderVictims(b);
    renderSemver(name, version);
    renderMaintainers(name);
    renderTyposquats(name);
    loadMap(name);
    syncUrl(name, version);
    results.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    $('#latency').textContent = '—';
    $('#latencysub').textContent = '';
    $('#verdictline').innerHTML = '';
    $('#hist').innerHTML = '';
    errorBox($('#victims'), err);
    $('#semver').innerHTML = '';
    $('#maint').innerHTML = '';
  } finally {
    running = false;
    btn.disabled = false;
    btn.textContent = 'check blast radius';
  }
}

/* ------------------------------------------------------------- lockfile */

async function scanLockfile(text, filename) {
  const name = $('#pkg').value.trim();
  const version = $('#ver').value.trim();
  const out = $('#lockresult');
  const verdict = $('#verdict');
  const detail = $('#lockdetail');
  out.hidden = false;

  if (!name) {
    verdict.className = 'verdict';
    verdict.innerHTML = `<div class="word" style="font-size:26px;color:var(--ink-2)">pick a package first</div>
      <div class="sub">type the compromised package in the box at the top, then drop the lockfile.</div>`;
    detail.innerHTML = '';
    return;
  }

  verdict.className = 'verdict';
  verdict.innerHTML = '<div class="word" style="font-size:26px;color:var(--ink-3)">scanning…</div>';
  detail.innerHTML = '';

  try {
    const q = new URLSearchParams({ name, depth: String(DEPTH) });
    if (version) q.set('bad_version', version);
    const r = await api(`/api/lockfile?${q}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: text,
    });

    const cls = { EXPOSED: 'exposed', SHIELDED: 'shielded', CLEAR: 'clear' }[r.verdict];
    verdict.className = `verdict ${cls}`;
    let sub;
    if (r.verdict === 'EXPOSED') {
      sub = r.direct
        ? `<b>${esc(filename)}</b> resolves ${esc(r.compromised)} at <b>${esc(r.direct.version)}</b> directly.`
        : `<b>${esc(filename)}</b> pulls ${esc(r.compromised)} through <b>${num(r.affected_count)}</b> of its ${num(r.resolved_count)} dependencies.`;
    } else if (r.verdict === 'SHIELDED') {
      sub = `${esc(r.compromised)} is in your tree at <b>${esc(r.direct.version)}</b> — not the malicious ${esc(r.bad_version)}.`;
    } else {
      sub = `none of the ${num(r.resolved_count)} packages in <b>${esc(filename)}</b> reach ${esc(r.compromised)} within depth ${DEPTH}.`;
    }
    if (r.paths_complete === false) {
      sub += ` <span style="color:var(--ink-3)">(from your lockfile alone — dependency paths unavailable)</span>`;
    }
    verdict.innerHTML = `<div class="word">${r.verdict}</div><div class="sub">${sub}</div>`;

    if (r.paths.length) {
      detail.innerHTML = `<div class="path">` + r.paths.map(p => {
        const hops = p.path.map((h, i) =>
          `<span class="hop${i === p.path.length - 1 ? ' last' : ''}">${esc(h)}</span>`);
        return `<div class="pathrow">${hops.join('<span class="arrow">→</span>')}
                  <span style="color:var(--ink-3)"> · ${p.depth} hop${p.depth === 1 ? '' : 's'}</span></div>`;
      }).join('') + `</div>`;
    } else if (r.paths_complete === false) {
      // Never print "no path reaches it" when we did not look. The verdict is
      // still sound — a lockfile records every package the install resolved —
      // but the paths that explain it are missing, and saying otherwise would
      // turn an outage into a false all-clear.
      detail.innerHTML = `<div class="note">scanned ${num(r.resolved_count)} resolved packages
        from your lockfile. ${esc(r.degraded || 'dependency paths are unavailable right now.')}</div>`;
    } else {
      detail.innerHTML = `<div class="note">no path from your direct dependencies reaches
        ${esc(r.compromised)} within depth ${DEPTH}. scanned ${num(r.resolved_count)} resolved packages
        in ${r.latency_ms}ms.</div>`;
    }
  } catch (err) {
    verdict.className = 'verdict';
    verdict.innerHTML = `<div class="word" style="font-size:26px;color:var(--red)">scan failed</div>
      <div class="sub">${esc(err.payload?.message || err.message)}</div>`;
    detail.innerHTML = '';
  }
}

function wireLockfile() {
  const drop = $('#drop'), file = $('#file');
  if (!drop || !file) return;

  const read = f => {
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => scanLockfile(String(reader.result), f.name);
    reader.onerror = () => scanLockfile('', f.name);
    reader.readAsText(f);
  };

  ['dragenter', 'dragover'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => read(e.dataTransfer?.files?.[0]));
  drop.addEventListener('click', () => file.click());
  drop.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); file.click(); }
  });
  $('#pick').addEventListener('click', e => { e.stopPropagation(); file.click(); });
  file.addEventListener('change', () => { read(file.files[0]); file.value = ''; });
}

/* ------------------------------------------------------------------ boot */

function boot() {
  wireDragging();
  wireSuggest();
  wireLockfile();
  wireAudit();
  wireMap();
  wireGraph();
  wireFeed();
  expandNode('debug', 'package', true);

  // This file runs on two pages that do not share their markup. /check has the
  // results panel; the landing page has the same search box and incident chips
  // but nowhere to render an answer. Asking whether the results panel exists is
  // what tells them apart — and on the landing page a query hands off to the
  // console rather than calling runQuery and writing into elements that are not
  // there.
  const hasResults = !!$('#results') && !!$('#latency');
  const ask = (name, ver) => {
    if (hasResults) return runQuery(name, ver);
    const q = new URLSearchParams({ pkg: name });
    if (ver) q.set('v', ver);
    location.href = '/check?' + q;
  };

  const form = $('#queryform');
  if (form) {
    form.addEventListener('submit', e => {
      e.preventDefault();
      closeSuggest();
      const name = $('#pkg').value.trim();
      if (!name) return $('#pkg').focus();
      ask(name, $('#ver').value.trim());
    });
  }

  const chips = $('#chips');
  if (chips) {
    chips.addEventListener('click', e => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      $('#pkg').value = chip.dataset.pkg;
      $('#ver').value = chip.dataset.ver || '';
      ask(chip.dataset.pkg, chip.dataset.ver || '');
    });
  }

  wireEvents();
  loadPeeks();

  // One control that puts the page back to a cold start. Recording a video
  // means running the same flow repeatedly, and reloading loses the warm
  // caches that make the second take fast.
  const reset = $('#resetall');
  if (reset) reset.addEventListener('click', () => {
    // Most of these exist only on /check. The header button is shared, so each
    // one is cleared only if this page actually has it.
    ['#results', '#auditresult', '#lockresult'].forEach(sel => {
      const el = $(sel); if (el) el.hidden = true;
    });
    ['#pkg', '#ver'].forEach(sel => { const el = $(sel); if (el) el.value = ''; });
    const vl = $('#verdictline'); if (vl) vl.innerHTML = '';
    const lat = $('#latency'); if (lat) lat.textContent = '—';
    const ls = $('#latencysub'); if (ls) ls.textContent = '';
    ['#hist', '#victims', '#semver', '#maint', '#typos', '#auditfindings',
     '#lockdetail'].forEach(sel => { const el = $(sel); if (el) el.innerHTML = ''; });
    closeSuggest();
    resetGraph();
    expandNode('debug', 'package', true);
    syncUrl('', '');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  // A result is a thing you send to a colleague at 2am, so every query is
  // reflected in the URL and every URL restores the query.
  const params = new URLSearchParams(location.search);
  const pkg = (params.get('pkg') || '').trim();
  if (pkg && $('#pkg') && hasResults) {
    $('#pkg').value = pkg;
    $('#ver').value = (params.get('v') || '').trim();
    runQuery(pkg, $('#ver').value);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}

/* ------------------------------------------------------------- blast map */
/* A literal blast radius. The compromised package sits at the centre and every
 * exposed package is placed on the ring for the depth it is *first* reached at
 * — which is a real property of the graph, taken from HydraDB's reachable set
 * at each bound, not a layout convenience. Red attenuates outward.
 *
 * Nodes on a ring are ordered by the circular mean of their parents' angles,
 * which keeps edges short and roughly radial without any force simulation. */

const MAP_SIZE = 760;
const DEPTH_FILL = ['#8f2318', '#c0392b', '#d4695c', '#e0958c', '#ebbdb7', '#f0cfca'];
let mapDepth = 3;
let mapAbort = null;

function circularMean(angles) {
  if (!angles.length) return null;
  const s = angles.reduce((a, x) => a + Math.sin(x), 0);
  const c = angles.reduce((a, x) => a + Math.cos(x), 0);
  return Math.atan2(s, c);
}

function layoutRadial(data) {
  const cx = MAP_SIZE / 2, cy = MAP_SIZE / 2;
  const maxDepth = Math.max(1, ...data.nodes.map(n => n.depth));
  const ring = (MAP_SIZE / 2 - 148) / maxDepth;

  const parentsOf = new Map();
  for (const e of data.edges) {
    if (!parentsOf.has(e.to)) parentsOf.set(e.to, []);
    parentsOf.get(e.to).push(e.from);
  }

  const angle = new Map([[data.root, 0]]);
  const pos = new Map([[data.root, { x: cx, y: cy, depth: 0 }]]);

  for (let d = 1; d <= maxDepth; d++) {
    const level = data.nodes.filter(n => n.depth === d);
    if (!level.length) continue;
    const sortKey = new Map();
    level.forEach((n, i) => {
      const known = (parentsOf.get(n.name) || [])
        .map(p => angle.get(p)).filter(a => a !== undefined);
      const m = circularMean(known);
      // No placed parent (a cross-link only): keep a stable spot rather than a
      // random one, so the picture does not reshuffle between renders.
      sortKey.set(n.name, m === null ? (i / level.length) * Math.PI * 2 : m);
    });
    level.sort((a, b) => sortKey.get(a.name) - sortKey.get(b.name));
    level.forEach((n, i) => {
      const a = (i / level.length) * Math.PI * 2 - Math.PI / 2;
      angle.set(n.name, a);
      pos.set(n.name, {
        x: cx + Math.cos(a) * ring * d,
        y: cy + Math.sin(a) * ring * d,
        depth: d,
      });
    });
  }
  return { pos, maxDepth, ring, cx, cy };
}

function nodeRadius(n) {
  if (n.depth === 0) return 13;
  return Math.max(3.2, Math.min(9, 3.2 + Math.log10(1 + (n.dependents || 0)) * 3.4));
}

function renderMap(data) {
  const svg = $('#map');
  const { pos, maxDepth, ring, cx, cy } = layoutRadial(data);
  const parts = [];

  for (let d = 1; d <= maxDepth; d++) {
    parts.push(`<circle class="ring" cx="${cx}" cy="${cy}" r="${ring * d}"/>`);
    parts.push(`<text class="ringlabel" x="${cx + 4}" y="${cy - ring * d - 5}">depth ${d}</text>`);
  }

  for (const e of data.edges) {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) continue;
    // Bow each edge toward the centre so parallel spokes stay legible.
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const qx = mx + (cx - mx) * 0.18, qy = my + (cy - my) * 0.18;
    parts.push(`<path class="edge" data-from="${esc(e.from)}" data-to="${esc(e.to)}" d="M${a.x.toFixed(1)} ${a.y.toFixed(1)} Q${qx.toFixed(1)} ${qy.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}"/>`);
  }

  // Plain nodes first, so labels and leaders always draw on top of them.
  for (const n of data.nodes) {
    const p = pos.get(n.name);
    if (!p) continue;
    const r = nodeRadius(n);
    const fill = DEPTH_FILL[Math.min(n.depth, DEPTH_FILL.length - 1)];
    const isRoot = n.depth === 0;
    const rootLabel = isRoot
      ? `<text x="${p.x.toFixed(1)}" y="${(p.y + r + 16).toFixed(1)}" text-anchor="middle">${esc(n.name)}</text>`
      : '';
    parts.push(`<g class="node${isRoot ? ' root' : ''}" data-name="${esc(n.name)}" data-depth="${n.depth}" data-dependents="${n.dependents || 0}"><circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r.toFixed(1)}" fill="${fill}"/>${rootLabel}</g>`);
  }

  // Labels live on the rim, not next to their node. Labels placed at the node
  // itself either collide (inner rings are crowded) or shoot off the canvas
  // (rotated ones run outward past the viewBox). Anchoring every label at one
  // radius outside the outermost ring, with a leader line back to its node,
  // gives each one its own angular lane and keeps them all inside the frame.
  const rim = ring * maxDepth + 12;
  const labels = data.nodes
    .filter(n => n.depth > 0)
    .sort((a, b) => (b.dependents || 0) - (a.dependents || 0))
    .slice(0, 16)
    .map(n => {
      const p = pos.get(n.name);
      return p ? { n, p, a: Math.atan2(p.y - cy, p.x - cx) } : null;
    })
    .filter(Boolean)
    .sort((x, y) => x.a - y.a);

  // Push apart anything closer together than one line of text.
  const minGap = 13 / rim;
  for (let i = 1; i < labels.length; i++) {
    if (labels[i].a - labels[i - 1].a < minGap) labels[i].a = labels[i - 1].a + minGap;
  }

  for (const { n, p, a } of labels) {
    const lx = cx + Math.cos(a) * rim;
    const ly = cy + Math.sin(a) * rim;
    const deg = a * 180 / Math.PI;
    const flip = deg > 90 || deg < -90;      // keep left-hand labels readable
    const text = n.name.length > 21 ? n.name.slice(0, 20) + '…' : n.name;
    parts.push(`<path class="leader" d="M${p.x.toFixed(1)} ${p.y.toFixed(1)} L${lx.toFixed(1)} ${ly.toFixed(1)}"/>`);
    parts.push(`<g class="node label" data-name="${esc(n.name)}" data-depth="${n.depth}" data-dependents="${n.dependents || 0}">`
      + `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" dy="3" text-anchor="${flip ? 'end' : 'start'}"`
      + ` transform="rotate(${(flip ? deg + 180 : deg).toFixed(1)} ${lx.toFixed(1)} ${ly.toFixed(1)})">${esc(text)}</text></g>`);
  }

  svg.innerHTML = parts.join('');

  const legend = [];
  for (let d = 1; d <= maxDepth; d++) {
    const h = data.histogram.find(x => x.depth === d);
    legend.push(`<span><i style="background:${DEPTH_FILL[Math.min(d, DEPTH_FILL.length - 1)]}"></i>depth ${d} · ${num(h ? h.packages : 0)}</span>`);
  }
  legend.push(`<span>${data.truncated
    ? `showing the ${num(data.shown)} best-connected of ${num(data.total_exposed)} exposed`
    : `all ${num(data.total_exposed)} exposed shown`} · ${Math.round(data.latency_ms)}ms</span>`);
  $('#maplegend').innerHTML = legend.join('');
}

function wireMap() {
  const svg = $('#map'), tip = $('#maptip');
  if (!svg) return;

  svg.addEventListener('mousemove', e => {
    const g = e.target.closest('.node');
    if (!g) return;
    const d = +g.dataset.depth;
    tip.hidden = false;
    tip.innerHTML = `<b>${esc(g.dataset.name)}</b> <span>· ${d === 0
      ? 'the compromised package'
      : `depth ${d} · ${num(+g.dataset.dependents)} direct dependents`}</span>`;
    const box = svg.getBoundingClientRect();
    tip.style.left = (e.clientX - box.left) + 'px';
    tip.style.top = (e.clientY - box.top) + 'px';
  });

  svg.addEventListener('mouseover', e => {
    const g = e.target.closest('.node');
    if (!g) return;
    const name = g.dataset.name;
    const touching = new Set([name]);
    $$('#map .edge').forEach(p => {
      const hit = p.dataset.from === name || p.dataset.to === name;
      p.classList.toggle('hot', hit);
      if (hit) { touching.add(p.dataset.from); touching.add(p.dataset.to); }
    });
    $$('#map .node').forEach(n => n.classList.toggle('dim', !touching.has(n.dataset.name)));
  });

  const clear = () => {
    tip.hidden = true;
    $$('#map .edge').forEach(p => p.classList.remove('hot'));
    $$('#map .node').forEach(n => n.classList.remove('dim'));
  };
  svg.addEventListener('mouseleave', clear);

  // Clicking a package pivots the whole console onto it — the point of having
  // the graph on screen rather than a list.
  svg.addEventListener('click', e => {
    const g = e.target.closest('.node');
    if (!g || g.classList.contains('root')) return;
    clear();
    $('#pkg').value = g.dataset.name;
    $('#ver').value = '';
    runQuery(g.dataset.name, '');
  });

  $$('.mini[data-mapdepth]').forEach(b => b.addEventListener('click', () => {
    $$('.mini[data-mapdepth]').forEach(x => x.classList.toggle('on', x === b));
    mapDepth = +b.dataset.mapdepth;
    const name = $('#pkg').value.trim();
    if (name) loadMap(name);
  }));
}

function mapMessage(text, colour) {
  $('#map').innerHTML = `<text x="${MAP_SIZE / 2}" y="${MAP_SIZE / 2}" text-anchor="middle" fill="${colour}" font-family="ui-monospace, monospace" font-size="13">${esc(text)}</text>`;
}

async function loadMap(name) {
  const token = {};
  mapAbort = token;
  $('#maplegend').innerHTML = `<span>drawing depth ${mapDepth}…</span>`;
  mapMessage('traversing…', '#bdbdb6');
  try {
    const d = await api(`/api/subgraph?name=${encodeURIComponent(name)}&depth=${mapDepth}`);
    if (mapAbort !== token) return;            // a newer query superseded this one
    if (!d.nodes.length || d.nodes.length === 1) {
      mapMessage(`nothing depends on ${name} within depth ${mapDepth}`, '#bdbdb6');
      $('#maplegend').innerHTML = '';
      return;
    }
    renderMap(d);
  } catch (err) {
    if (mapAbort !== token) return;
    mapMessage(err.message, '#c0392b');
    $('#maplegend').innerHTML = '';
  }
}

/* --------------------------------------------------------- typosquat ring */

async function renderTyposquats(name) {
  const box = $('#typos');
  box.innerHTML = '<div class="empty">checking one-edit neighbours…</div>';
  try {
    const t = await api(`/api/typosquats?name=${encodeURIComponent(name)}`);
    const where = t.checked_live ? 'checked live against the npm registry'
                                 : 'registry unreachable — crawled corpus only';
    if (!t.existing.length) {
      box.innerHTML = `<div class="empty">none of the ${num(t.candidates)} one-edit
        variants of ${esc(name)} are real packages. ${where}, in ${Math.round(t.latency_ms)}ms.</div>`;
      return;
    }
    // npm republishes a name it has taken down as 0.0.1-security. That version
    // string is not a version, it is a tombstone: somebody squatted this name
    // and npm removed it.
    const rows = t.existing.map(h => {
      const dead = h.latest === '0.0.1-security';
      const tag = dead ? '<span class="tagpill bad">taken down by npm</span>'
        : h.in_graph ? '<span class="tagpill">in the graph</span>'
        : '<span class="tagpill pin">live on npm</span>';
      return `<div class="r"><span>${esc(h.name)}</span>
        <span class="g">${h.latest && !dead ? esc(h.latest) + ' · ' : ''}${tag}</span></div>`;
    }).join('');
    box.innerHTML =
      `<div class="r"><span class="g" style="margin:0">${num(t.existing.length)} of
        ${num(t.candidates)} one-edit variants of ${esc(name)} are real packages —
        ${where}</span></div>` + rows;
  } catch (err) { errorBox(box, err); }
}

/* ------------------------------------------------------ live system state */

let sse = null, sseSeen = false, pollTimer = null;

function applyStats(s) {
  lastStats = s;
  const pulse = $('#pulse'), line = $('#statline');
  const warming = s.warmup && s.warmup !== 'warm';
  pulse.className = 'pulse ' + (s.hydradb === false || warming ? 'dead' : 'live');
  const bits = [`${num(s.packages)} packages`, `${num(s.edges)} edges`];
  if (warming) bits.push('warming');
  if (s.crawl && s.crawl.running) bits.push(`crawling · ${num(s.crawl.crawled)} done`);
  line.textContent = bits.join(' · ');
  renderStatusCards(s);
}

function card(k, v, d, cls) {
  return `<div class="card ${cls || ''}"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d || ''}</div></div>`;
}

function renderStatusCards(s) {
  const warming = s.warmup && s.warmup !== 'warm';
  const crawl = s.crawl || {};
  const cards = [
    card('hydradb',
         s.hydradb === false ? 'unreachable' : warming ? 'warming' : 'answering',
         warming ? 'cold store — deep traversals time out' : 'traversals served from cache',
         s.hydradb === false ? 'bad' : warming ? 'warn' : 'good'),
    card('graph', num(s.packages), `${num(s.edges)} REQUIRED_BY edges`, 'good'),
    card('sidecar', `${s.latency_ms}ms`, 'deps.db read latency', 'good'),
    card('writable',
         (s.writable === null || s.writable === undefined) ? 'probing…' : s.writable ? 'yes' : 'read-only',
         s.writable === false ? 'restarted store — run py rebuild.py' : 'writes round-trip',
         s.writable === false ? 'warn' : 'good'),
    card('crawl', crawl.running ? 'running' : 'idle',
         `${num(crawl.crawled || 0)} of ${num(crawl.known || 0)} crawled`, 'good'),
    card('nid collisions', num(crawl.collisions || 0),
         'name → integer id map', (crawl.collisions || 0) ? 'bad' : 'good'),
  ];
  $('#syscards').innerHTML = cards.join('');
  const g = s.graph;
  $('#sysnote').textContent = g
    ? `hydradb's own count(*) last measured ${num(g.packages)} vertices and ${num(g.edges)} edges, taking ${Math.round(g.measured_ms)}ms — a full scan, which is why the live figures above come from the sidecar.`
    : `hydradb's own count(*) is a full scan and runs on a background timer; no measurement has been taken yet this session.`;
}

function startPolling() {
  if (pollTimer) return;
  pollStats();
  pollTimer = setInterval(pollStats, 4000);
}

function wireEvents() {
  if (!window.EventSource) return startPolling();
  try {
    sse = new EventSource('/api/events');
  } catch { return startPolling(); }
  sse.addEventListener('stats', e => {
    sseSeen = true;
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    try { applyStats(JSON.parse(e.data)); } catch { /* keep the last good frame */ }
  });
  sse.addEventListener('publish', e => {
    try { renderTicks(JSON.parse(e.data).events || []); } catch { /* next frame */ }
  });
  sse.addEventListener('error', () => {
    // EventSource reconnects on its own, so polling only takes over if the
    // stream never worked at all — a brief blip must not leave the page stale.
    if (!sseSeen) { try { sse.close(); } catch {} startPolling(); }
  });
  setTimeout(() => { if (!sseSeen) startPolling(); }, 6000);
}

/* ---------------------------------------------------------- shareable url */

function syncUrl(name, version) {
  const q = new URLSearchParams();
  if (name) q.set('pkg', name);
  if (version) q.set('v', version);
  history.replaceState(null, '', q.toString() ? `?${q}` : location.pathname);
}

/* ----------------------------------------------------- live project audit */
/* "Am I exposed to this incident" needs the graph. "Is anything in my tree
 * already known-malicious" does not: the lockfile is the tree, so every entry
 * goes straight to OSV. That makes this the one feature that works on any
 * project on earth regardless of what our crawl reached. */

const VERDICT_COPY = {
  COMPROMISED: n => `${num(n)} package${n === 1 ? '' : 's'} in your tree ${n === 1 ? 'is' : 'are'} confirmed malicious.`,
  VULNERABLE: n => `no malware, but ${num(n)} package${n === 1 ? ' has' : 's have'} known vulnerabilities.`,
  CLEAN: () => 'nothing in your tree matches a known advisory.',
};

function copyButton(label, text) {
  const id = 'c' + Math.random().toString(36).slice(2, 9);
  window.__copy = window.__copy || {};
  window.__copy[id] = text;
  return `<button type="button" class="copybtn" data-copy="${id}">${esc(label)}</button>`;
}

function wireCopy(root) {
  $$('.copybtn[data-copy]', root).forEach(b => {
    b.addEventListener('click', async () => {
      const text = (window.__copy || {})[b.dataset.copy] || '';
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // Clipboard needs a secure context; select-and-copy still works.
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } finally { ta.remove(); }
      }
      const was = b.textContent;
      b.textContent = 'copied';
      b.classList.add('done');
      setTimeout(() => { b.textContent = was; b.classList.remove('done'); }, 1600);
    });
  });
}

function renderFinding(f) {
  const mal = f.malware.length;
  const advisories = [...f.malware, ...f.vulnerabilities];
  const rows = advisories.map(a => `
    <div class="adv-row">
      <span class="badge ${a.kind === 'malware' ? 'mal' : 'vuln'}">${a.kind}</span>
      <b>${esc(a.id)}</b>${a.severity ? ` <span class="ver">· ${esc(a.severity)}</span>` : ''}
      <div>${esc(a.summary || 'no summary published')}</div>
      <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.url)}</a>
    </div>`).join('');
  return `
    <details class="finding" data-name="${esc(f.name)}" data-version="${esc(f.version)}">
      <summary>
        <span class="badge ${mal ? 'mal' : 'vuln'}">${mal ? 'malware' : 'vuln'}</span>
        <span class="pkg">${esc(f.name)}</span><span class="ver">@${esc(f.version)}</span>
        <span class="adv">${num(advisories.length)} advisor${advisories.length === 1 ? 'y' : 'ies'} · click to fix</span>
      </summary>
      <div class="detail">
        ${rows}
        <div class="fixslot"><button type="button" class="copybtn loadfix">show me how to fix this</button></div>
      </div>
    </details>`;
}

async function loadFix(details) {
  const slot = $('.fixslot', details);
  const name = details.dataset.name, version = details.dataset.version;
  slot.innerHTML = '<span class="ver">working out the safe version…</span>';
  try {
    const f = await api(`/api/fix?name=${encodeURIComponent(name)}&bad_version=${encodeURIComponent(version)}`);
    const overrides = JSON.stringify(f.package_json_overrides, null, 2);
    slot.innerHTML = `
      <div class="fixbox">
        <h4>safe version</h4>
        <div style="margin-bottom:10px">
          ${f.recommended
            ? `upgrade to <b>${esc(f.recommended)}</b> — the nearest release above
               ${esc(version)} with no advisory against it${f.recommended !== f.latest
                 ? ` (latest is ${esc(f.latest)}, but you do not have to go that far)` : ''}`
            : `no clean release above ${esc(version)} was found. pin to a fork or remove it.`}
        </div>
        ${f.recommended ? `<h4>force it everywhere, including transitive copies</h4>
        <pre>${esc(overrides)}</pre>
        ${copyButton('copy overrides', overrides)}` : ''}
        <h4 style="margin-top:14px">hand it to your ai</h4>
        <pre>${esc(f.ai_prompt)}</pre>
        <div class="fixrow">
          ${copyButton('copy prompt for cursor / claude / codex', f.ai_prompt)}
          <span class="ver">a self-contained brief — paste it into any coding agent</span>
        </div>
      </div>`;
    wireCopy(slot);
  } catch (err) {
    slot.innerHTML = `<span class="ver">could not build a fix: ${esc(err.message)}</span>`;
  }
}

async function runAudit(text, filename) {
  const out = $('#auditresult'), verdict = $('#auditverdict'), list = $('#auditfindings');
  out.hidden = false;
  verdict.className = 'verdict';
  verdict.innerHTML = `<div class="word" style="font-size:26px;color:var(--ink-3)">scanning…</div>
    <div class="sub">checking every package in ${esc(filename)} against osv.dev</div>`;
  list.innerHTML = '';

  try {
    const r = await api('/api/audit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: text,
    });
    const cls = { COMPROMISED: 'compromised', VULNERABLE: 'vulnerable', CLEAN: 'clean' }[r.verdict];
    const n = r.verdict === 'COMPROMISED' ? r.malicious_count : r.vulnerable_count;
    verdict.className = `verdict ${cls}`;
    verdict.innerHTML = `<div class="word">${r.verdict}</div>
      <div class="sub">${VERDICT_COPY[r.verdict](n)}
        <b>${num(r.scanned)}</b> packages checked in ${Math.round(r.latency_ms)}ms.</div>`;

    if (!r.findings.length) {
      list.innerHTML = `<div class="empty" style="padding:16px 14px">
        every one of the ${num(r.scanned)} resolved packages in ${esc(filename)} was
        checked against osv.dev and none of them match a published advisory.</div>`;
      return;
    }
    list.innerHTML = r.findings.map(renderFinding).join('')
      + (r.truncated ? `<div class="empty" style="padding:12px 14px">
          ${num(r.flagged)} packages were flagged; the ${num(r.detailed)} with the most
          advisories are detailed above.</div>` : '');
    $$('.finding .loadfix', list).forEach(b => b.addEventListener('click', e => {
      e.preventDefault();
      loadFix(b.closest('.finding'));
    }, { once: true }));
  } catch (err) {
    verdict.className = 'verdict';
    verdict.innerHTML = `<div class="word" style="font-size:26px;color:var(--red)">scan failed</div>
      <div class="sub">${esc(err.payload?.message || err.message)}</div>`;
  }
}

function wireAudit() {
  const drop = $('#auditdrop'), file = $('#auditfile');
  if (!drop || !file) return;
  const read = f => {
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => runAudit(String(reader.result), f.name);
    reader.readAsText(f);
  };
  ['dragenter', 'dragover'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => read(e.dataTransfer?.files?.[0]));
  drop.addEventListener('click', () => file.click());
  drop.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); file.click(); }
  });
  $('#auditpick').addEventListener('click', e => { e.stopPropagation(); file.click(); });
  file.addEventListener('change', () => { read(file.files[0]); file.value = ''; });
}

/* ------------------------------------------------------------- graph explorer */
/* A force-directed view of the real graph. The simulation is hand-rolled rather
 * than pulled from a CDN: this has to keep working while a video is being
 * recorded on unreliable wifi, and a script tag pointing at someone else's
 * server is the one dependency that can fail at exactly the wrong moment.
 *
 * The browser holds no model of the graph. Clicking a node asks HydraDB what is
 * adjacent to it, and whatever comes back is merged in. */

const GW = 1000, GH = 620;
const KIND_FILL = { package: '#55554f', maintainer: '#2c7a45', advisory: '#a9761b' };
const MALICIOUS_FILL = '#c0392b';
const MAX_NODES = 300;

const G = { nodes: new Map(), links: [], frame: null, alpha: 0, dragging: null };

function gkey(name, kind) { return kind + ':' + name; }

function gradius(n) {
  if (n.kind !== 'package') return 9;
  return Math.max(5, Math.min(17, 5 + Math.log10(1 + (n.dependents || 0)) * 5));
}

function addNode(name, kind, extra = {}) {
  const key = gkey(name, kind);
  if (G.nodes.has(key)) {
    Object.assign(G.nodes.get(key), extra);
    return G.nodes.get(key);
  }
  if (G.nodes.size >= MAX_NODES) return null;
  // Seed near the middle with a little spread so the simulation has something
  // to push apart; starting everything at one point makes forces explode.
  const a = Math.random() * Math.PI * 2, r = 40 + Math.random() * 140;
  const node = {
    key, name, kind,
    x: GW / 2 + Math.cos(a) * r, y: GH / 2 + Math.sin(a) * r,
    vx: 0, vy: 0, expanded: false, ...extra,
  };
  G.nodes.set(key, node);
  return node;
}

function addLink(a, b, edge) {
  if (!a || !b) return;
  if (G.links.some(l => l.a === a.key && l.b === b.key && l.edge === edge)) return;
  G.links.push({ a: a.key, b: b.key, edge });
}

/* --------------------------------------------------------------- simulation */

function step() {
  const nodes = [...G.nodes.values()];
  const n = nodes.length;

  // Repulsion. O(n^2), which is fine at a few hundred nodes and avoids needing
  // a quadtree that would be more code than the rest of this put together.
  for (let i = 0; i < n; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < n; j++) {
      const b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
      if (d2 > 90000) continue;                 // ignore distant pairs
      const f = 2600 / d2;
      const d = Math.sqrt(d2);
      a.vx -= (dx / d) * f; a.vy -= (dy / d) * f;
      b.vx += (dx / d) * f; b.vy += (dy / d) * f;
    }
  }

  // Springs along edges.
  for (const l of G.links) {
    const a = G.nodes.get(l.a), b = G.nodes.get(l.b);
    if (!a || !b) continue;
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.max(1, Math.hypot(dx, dy));
    const f = (d - 90) * 0.0055;
    a.vx += (dx / d) * f; a.vy += (dy / d) * f;
    b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
  }

  for (const nd of nodes) {
    if (nd === G.dragging) { nd.vx = nd.vy = 0; continue; }
    nd.vx += (GW / 2 - nd.x) * 0.0016;          // gentle centring
    nd.vy += (GH / 2 - nd.y) * 0.0016;
    nd.vx *= 0.86; nd.vy *= 0.86;               // damping
    nd.x = Math.max(24, Math.min(GW - 24, nd.x + nd.vx));
    nd.y = Math.max(24, Math.min(GH - 24, nd.y + nd.vy));
  }
}

function paint() {
  const svg = $('#graph');
  const parts = [];
  for (const l of G.links) {
    const a = G.nodes.get(l.a), b = G.nodes.get(l.b);
    if (!a || !b) continue;
    parts.push(`<line class="glink ${l.edge}" data-a="${esc(l.a)}" data-b="${esc(l.b)}"
      x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"/>`);
  }
  for (const nd of G.nodes.values()) {
    const r = gradius(nd);
    const fill = nd.is_malware ? MALICIOUS_FILL : (KIND_FILL[nd.kind] || '#55554f');
    const label = (r >= 8 || nd.kind !== 'package')
      ? `<text x="${(nd.x + r + 4).toFixed(1)}" y="${(nd.y + 3).toFixed(1)}">${esc(
          nd.name.length > 22 ? nd.name.slice(0, 21) + '…' : nd.name)}</text>` : '';
    parts.push(`<g class="gnode${nd.seed ? ' seed' : ''}${nd.expanded ? ' done' : ''}"
        data-key="${esc(nd.key)}" data-name="${esc(nd.name)}" data-kind="${esc(nd.kind)}">
        <circle cx="${nd.x.toFixed(1)}" cy="${nd.y.toFixed(1)}" r="${r.toFixed(1)}"
                fill="${fill}" opacity="${nd.expanded ? 1 : 0.82}"/>${label}</g>`);
  }
  svg.innerHTML = parts.join('');
}

function tick() {
  step();
  paint();
  G.alpha -= 1;
  if (G.alpha > 0) {
    G.frame = requestAnimationFrame(tick);
  } else {
    G.frame = null;
  }
}

function kick(frames = 220) {
  G.alpha = Math.max(G.alpha, frames);
  if (!G.frame) G.frame = requestAnimationFrame(tick);
}

/* ------------------------------------------------------------- interaction */

function graphStat() {
  const kinds = { package: 0, maintainer: 0, advisory: 0 };
  for (const n of G.nodes.values()) kinds[n.kind] = (kinds[n.kind] || 0) + 1;
  $('#graphstat').innerHTML =
    `${num(G.nodes.size)} nodes · ${num(G.links.length)} edges`
    + (G.nodes.size >= MAX_NODES ? ` · capped at ${MAX_NODES}` : '');
  $('#graphlegend').innerHTML =
    `<span><i style="background:${KIND_FILL.package}"></i>package ${num(kinds.package)}</span>`
    + `<span><i style="background:${KIND_FILL.maintainer}"></i>maintainer ${num(kinds.maintainer)}</span>`
    + `<span><i style="background:${KIND_FILL.advisory}"></i>advisory ${num(kinds.advisory)}</span>`
    + `<span><i style="background:${MALICIOUS_FILL}"></i>malicious</span>`;
}

async function expandNode(name, kind, seed = false) {
  const key = gkey(name, kind);
  const existing = G.nodes.get(key);
  if (existing && existing.expanded) return;
  $('#graphstat').textContent = `asking hydradb about ${name}…`;
  try {
    const d = await api(`/api/expand?name=${encodeURIComponent(name)}&kind=${kind}&limit=24`);
    const root = addNode(d.node.name, d.node.kind, {
      dependents: d.node.dependents, is_malware: d.node.is_malware,
      expanded: true, seed,
    });
    for (const nb of d.neighbours) {
      const child = addNode(nb.name, nb.kind, {
        dependents: nb.dependents, is_malware: nb.is_malware,
      });
      if (child) addLink(root, child, nb.edge);
    }
    graphStat();
    kick();
  } catch (err) {
    $('#graphstat').textContent = err.message;
  }
}

function wireGraph() {
  const svg = $('#graph'), tip = $('#graphtip');
  if (!svg) return;

  const toSvg = e => {
    const box = svg.getBoundingClientRect();
    return {
      x: (e.clientX - box.left) * (GW / box.width),
      y: (e.clientY - box.top) * (GH / box.height),
    };
  };

  svg.addEventListener('pointerdown', e => {
    const g = e.target.closest('.gnode');
    if (!g) return;
    G.dragging = G.nodes.get(g.dataset.key) || null;
    G.moved = false;
    svg.setPointerCapture(e.pointerId);
  });

  svg.addEventListener('pointermove', e => {
    const g = e.target.closest('.gnode');
    if (G.dragging) {
      const p = toSvg(e);
      G.dragging.x = p.x; G.dragging.y = p.y;
      G.moved = true;
      kick(60);
      return;
    }
    if (!g) { tip.hidden = true; return; }
    const nd = G.nodes.get(g.dataset.key);
    if (!nd) return;
    tip.hidden = false;
    tip.innerHTML = `<b>${esc(nd.name)}</b> <span>· ${esc(nd.kind)}`
      + (nd.kind === 'package' && nd.dependents != null
          ? ` · ${num(nd.dependents)} direct dependents` : '')
      + (nd.is_malware ? ' · malicious' : '')
      + (nd.expanded ? '' : ' · click to expand') + '</span>';
    const box = svg.getBoundingClientRect();
    tip.style.left = (e.clientX - box.left) + 'px';
    tip.style.top = (e.clientY - box.top) + 'px';
  });

  const release = e => {
    if (G.dragging) { try { svg.releasePointerCapture(e.pointerId); } catch {} }
    G.dragging = null;
  };
  svg.addEventListener('pointerup', e => {
    const g = e.target.closest('.gnode');
    const wasDrag = G.moved;
    release(e);
    if (g && !wasDrag) expandNode(g.dataset.name, g.dataset.kind);
  });
  svg.addEventListener('pointercancel', release);
  svg.addEventListener('mouseleave', () => { tip.hidden = true; });

  $$('.mini[data-seed]').forEach(b => b.addEventListener('click', () => {
    resetGraph();
    expandNode(b.dataset.seed, b.dataset.kind, true);
  }));
  $('#explorerreset').addEventListener('click', () => {
    resetGraph();
    expandNode('debug', 'package', true);
  });
}

function resetGraph() {
  G.nodes.clear();
  G.links.length = 0;
  if (G.frame) cancelAnimationFrame(G.frame);
  G.frame = null;
  G.alpha = 0;
  paint();
  graphStat();
}

/* ---------------------------------------------------------------- live feed */
/* Publishes arrive on the same SSE stream as the system stats. Most of them are
 * somebody's first version with nothing downstream, and the ticker says so
 * rather than dressing every publish up as an event — the ones that matter are
 * the packages the graph already knows thousands of things depend on. */

const seenTicks = new Set();

function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function tickRow(e) {
  const reach = e.advisories && e.advisories.length
    ? esc(e.advisories[0].id)
    : e.dependents
      ? `${num(e.dependents)} depend on it`
      : e.in_graph ? 'no dependents yet' : 'not in graph';
  return `<div class="tick ${esc(e.level)}" data-at="${e.at}" data-name="${esc(e.name)}">
      <span class="ago">${ago(e.at)}</span>
      <span class="who">${esc(e.name)}<span style="color:var(--ink-3)">@${esc(e.version)}</span></span>
      <span class="reach">${reach}</span>
    </div>`;
}

function renderTicks(events) {
  const box = $('#ticker');
  const fresh = events.filter(e => !seenTicks.has(e.name + '@' + e.version + e.at));
  if (!fresh.length) return;
  fresh.forEach(e => seenTicks.add(e.name + '@' + e.version + e.at));
  // Newest first, and never let the list grow without bound.
  box.insertAdjacentHTML('afterbegin', fresh.map(tickRow).join(''));
  [...box.children].slice(40).forEach(el => el.remove());
  if (seenTicks.size > 400) seenTicks.clear();
}

function refreshAgo() {
  $$('#ticker .tick').forEach(el => {
    const at = +el.dataset.at;
    if (at) el.querySelector('.ago').textContent = ago(at);
  });
}

async function pollFeed() {
  try {
    const d = await api('/api/feed?limit=25');
    const stat = $('#feedstat');
    if (!d.running) {
      stat.textContent = 'feed off';
      if (!$('#ticker').children.length) {
        $('#ticker').innerHTML =
          '<div class="empty">the live feed is disabled in demo mode.</div>';
      }
      return;
    }
    stat.textContent =
      `${num(d.publishes_seen)} publishes seen · ${num(d.polls)} polls`
      + (d.npm_total ? ` · npm has ${num(d.npm_total)} packages` : '');
    if (d.events.length) renderTicks(d.events);
    else if (!$('#ticker').children.length) {
      $('#ticker').innerHTML =
        '<div class="empty">watching npm — nothing published in the last few seconds.</div>';
    }
  } catch (err) {
    $('#feedstat').textContent = 'feed unavailable';
  }
}

function wireFeed() {
  if (!$('#ticker')) return;
  pollFeed();
  // The SSE stream pushes publishes as they land; this is the floor under it
  // so the panel still fills if the stream never opened.
  setInterval(pollFeed, 15000);
  setInterval(refreshAgo, 5000);

  $('#ticker').addEventListener('click', e => {
    const row = e.target.closest('.tick');
    if (!row) return;
    $('#pkg').value = row.dataset.name;
    $('#ver').value = '';
    runQuery(row.dataset.name, '');
  });
}
