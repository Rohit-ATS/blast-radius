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

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body;
  try { body = await res.json(); }
  catch { throw Object.assign(new Error('server sent a non-JSON response'), { status: res.status }); }
  if (!res.ok) {
    throw Object.assign(new Error(body.message || body.error || `http ${res.status}`),
                        { status: res.status, payload: body });
  }
  return body;
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
    line.textContent = err.status === 503 ? 'hydradb unreachable' : 'stats unavailable';
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
  $('#suggest').hidden = true;
  $('#suggest').innerHTML = '';
  suggestIndex = -1;
}

function wireSuggest() {
  const input = $('#pkg'), box = $('#suggest');

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
  where.innerHTML = `<div class="errbox"><b>${err.status === 404 ? 'not in the graph' : 'query failed'}</b>${esc(detail)}</div>`;
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
    const [b, statsSettled] = await Promise.all([
      api(`/api/blast?name=${encodeURIComponent(name)}&depth=${DEPTH}`),
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
    verdict.innerHTML = `<div class="word">${r.verdict}</div><div class="sub">${sub}</div>`;

    if (r.paths.length) {
      detail.innerHTML = `<div class="path">` + r.paths.map(p => {
        const hops = p.path.map((h, i) =>
          `<span class="hop${i === p.path.length - 1 ? ' last' : ''}">${esc(h)}</span>`);
        return `<div class="pathrow">${hops.join('<span class="arrow">→</span>')}
                  <span style="color:var(--ink-3)"> · ${p.depth} hop${p.depth === 1 ? '' : 's'}</span></div>`;
      }).join('') + `</div>`;
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

  $('#queryform').addEventListener('submit', e => {
    e.preventDefault();
    closeSuggest();
    const name = $('#pkg').value.trim();
    if (!name) return $('#pkg').focus();
    runQuery(name, $('#ver').value.trim());
  });

  $('#chips').addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    $('#pkg').value = chip.dataset.pkg;
    $('#ver').value = chip.dataset.ver || '';
    runQuery(chip.dataset.pkg, chip.dataset.ver || '');
  });

  pollStats();
  setInterval(pollStats, 4000);
  loadPeeks();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
