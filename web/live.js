/* Blast Radius — the real-time panels.
 *
 * Two things live here: the per-registry ingestion rail, and project
 * monitoring. Both read the same endpoints an integrating project would, so
 * nothing on screen is reachable only from inside the page.
 *
 * The rail deliberately shows unflattering states. An ecosystem that is backed
 * off or erroring is drawn as such, with the reason, because a status panel
 * that is always green is not telling you anything.
 */
(function () {
  'use strict';

  const q = (s, r) => (r || document).querySelector(s);
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
  const fmt = n => (typeof n === 'number' ? n.toLocaleString('en-US') : '—');

  const LABEL = { npm: 'npm', pypi: 'PyPI', crates: 'crates.io',
                  go: 'Go', maven: 'Maven' };

  async function getJSON(path, opts) {
    const r = await fetch(path, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { /* non-JSON error page */ }
    if (!r.ok) {
      const err = new Error((body && body.message) || `HTTP ${r.status}`);
      err.body = body;
      throw err;
    }
    return body;
  }

  function ago(seconds) {
    if (seconds == null) return 'never';
    if (seconds < 60) return `${Math.round(seconds)}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
  }

  /* ==================================================== 1 · ingestion rail */

  async function ingestion() {
    const rail = q('#ecorail');
    const stat = q('#ingeststat');
    if (!rail) return;

    let d;
    try {
      d = await getJSON('/api/live/status');
    } catch (e) {
      rail.innerHTML = `<div class="skel">ingestion status unavailable — ${esc(e.message)}</div>`;
      if (stat) stat.textContent = 'unavailable';
      return;
    }

    if (!d.running) {
      rail.innerHTML =
        `<div class="skel">${esc(d.reason || 'continuous ingestion is not running')}</div>`;
      if (stat) stat.textContent = 'ingestion off';
      return;
    }

    rail.innerHTML = (d.ecosystems || []).map(e => {
      // `state` is whatever the daemon actually recorded, not a derived guess.
      const cls = e.state === 'live' ? 'ok'
        : e.state === 'starting' ? 'wait'
        : e.state === 'degraded' ? 'warn' : 'bad';
      const detail = e.state === 'backoff'
        ? `backing off ${Math.round(e.backoff_seconds)}s`
        : e.last_error ? esc(e.last_error.slice(0, 48))
        : `${fmt(e.packages_written)} written · polled ${ago(e.seconds_since_poll)}`;
      return `<div class="eco ${cls}" title="${esc(e.last_error || e.state)}">
          <div class="eco-top">
            <span class="eco-dot"></span>
            <b>${esc(LABEL[e.ecosystem] || e.ecosystem)}</b>
            <span class="eco-state">${esc(e.state)}</span>
          </div>
          <div class="eco-n">${fmt(e.events_seen)}</div>
          <div class="eco-t">publishes seen · every ${e.poll_interval}s</div>
          <div class="eco-d">${detail}</div>
        </div>`;
    }).join('');

    if (stat) {
      const budget = d.growth_paused
        ? ' · growth paused at the edge budget'
        : (d.budget_used != null ? ` · ${Math.round(d.budget_used * 100)}% of edge budget` : '');
      stat.textContent =
        `${fmt(d.graph_writes)} graph writes · ${fmt(d.edges_written)} edges this run`
        + ` · ${d.ecosystems_live}/${d.ecosystems_total} live` + budget;
    }
  }

  let lastEventKey = '';

  async function ingestTicker() {
    const box = q('#ingestticker');
    if (!box) return;
    let d;
    try {
      d = await getJSON('/api/live/events?limit=24');
    } catch (e) { return; }

    const events = d.events || [];
    if (!events.length) {
      if (!box.querySelector('.tick')) {
        box.innerHTML = '<div class="empty">no publish has landed yet — '
          + 'the registries are quiet this second.</div>';
      }
      return;
    }
    const key = events[0].at + ':' + events[0].qualified;
    if (key === lastEventKey) return;        // nothing new; leave the DOM alone
    lastEventKey = key;

    box.innerHTML = events.map(e => {
      const when = new Date(e.at * 1000).toLocaleTimeString('en-US', { hour12: false });
      return `<div class="tick" data-name="${esc(e.name)}" data-eco="${esc(e.ecosystem)}">
          <span class="tick-t">${esc(when)}</span>
          <span class="badge eco-${esc(e.ecosystem)}">${esc(LABEL[e.ecosystem] || e.ecosystem)}</span>
          <span class="tick-n">${esc(e.name)}</span>
          <span class="tick-v">${esc(e.version || '')}</span>
          <span class="tick-d">${e.was_known ? 'refreshed' : 'new'} · ${fmt(e.deps)} deps</span>
        </div>`;
    }).join('');
  }

  /* ====================================================== 2 · monitoring */

  const mon = { id: null, token: null, stream: null, seen: 0 };

  function credBlock() {
    const host = q('#moncred');
    if (!host || !mon.id) return;
    host.hidden = false;
    host.innerHTML =
      `<div class="cred-row"><span>project</span><code>${esc(mon.id)}</code></div>
       <div class="cred-row"><span>token</span><code>${esc(mon.token)}</code></div>
       <div class="cred-row"><span>poll</span><code>GET /api/watch/${esc(mon.id)}/alerts?token=…&amp;since=N</code></div>
       <div class="cred-row"><span>stream</span><code>GET /api/watch/${esc(mon.id)}/stream?token=…</code></div>
       <button type="button" class="btn ghost-sm" id="monstop">Stop watching</button>`;
    q('#monstop').addEventListener('click', unregister);
  }

  async function register(text, filename) {
    const stat = q('#mon-stat');
    const name = (q('#monname').value || 'my-service').trim().slice(0, 80);
    const hook = (q('#monhook').value || '').trim();
    if (stat) stat.textContent = 'registering…';

    const params = new URLSearchParams({ name, filename: filename || '' });
    if (hook) params.set('webhook', hook);

    try {
      const d = await getJSON(`/api/watch/register?${params}`,
                              { method: 'POST', body: text });
      mon.id = d.project_id;
      mon.token = d.token;
      mon.seen = 0;
      if (stat) {
        stat.textContent = `watching ${fmt(d.watching)} packages · `
          + `${d.ecosystem} · ${d.precision}`;
      }
      const hint = q('#monhint');
      if (hint) hint.textContent = `${d.source_kind} — ${d.note}`;
      credBlock();
      openStream();
    } catch (e) {
      if (stat) stat.textContent = 'could not register';
      const box = q('#monalerts');
      if (box) box.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  }

  async function unregister() {
    if (!mon.id) return;
    closeStream();
    try {
      await getJSON(`/api/watch/${mon.id}?token=${encodeURIComponent(mon.token)}`,
                    { method: 'DELETE' });
    } catch (e) { /* the panel resets either way */ }
    mon.id = mon.token = null;
    const host = q('#moncred');
    if (host) { host.hidden = true; host.innerHTML = ''; }
    const stat = q('#mon-stat');
    if (stat) stat.textContent = 'no project registered';
    const as = q('#mon-alertstat');
    if (as) as.textContent = 'not streaming';
    const box = q('#monalerts');
    if (box) {
      box.innerHTML = '<div class="empty">register a project and this streams '
        + 'its alerts live over SSE.</div>';
    }
  }

  function closeStream() {
    if (mon.stream) { mon.stream.close(); mon.stream = null; }
  }

  function openStream() {
    closeStream();
    if (!mon.id) return;
    const as = q('#mon-alertstat');
    const box = q('#monalerts');
    const es = new EventSource(
      `/api/watch/${mon.id}/stream?token=${encodeURIComponent(mon.token)}`);
    mon.stream = es;

    es.addEventListener('ready', ev => {
      let d = {};
      try { d = JSON.parse(ev.data); } catch (e) { /* keep the default text */ }
      if (as) as.textContent = `streaming · ${fmt(d.watching)} packages`;
      if (box && !box.querySelector('.tick')) {
        box.innerHTML = '<div class="empty">connected. nothing you depend on has '
          + 'published yet — this fills the moment one does.</div>';
      }
    });

    es.addEventListener('alert', ev => {
      let a;
      try { a = JSON.parse(ev.data); } catch (e) { return; }
      pushAlert(a);
    });

    es.onerror = () => {
      // Distinguishing "quiet" from "dead" is the whole point of the heartbeat,
      // so a dropped stream has to say so rather than just stop updating.
      if (as) as.textContent = 'stream dropped — retrying';
    };
  }

  function pushAlert(a) {
    const box = q('#monalerts');
    if (!box) return;
    if (!box.querySelector('.tick')) box.innerHTML = '';
    mon.seen += 1;
    const when = new Date((a.at || Date.now() / 1000) * 1000)
      .toLocaleTimeString('en-US', { hour12: false });
    const advisories = (a.detail && a.detail.advisories) || [];
    const row = document.createElement('div');
    row.className = `tick sev-${esc(a.severity)}`;
    row.dataset.name = a.package;
    row.innerHTML =
      `<span class="tick-t">${esc(when)}</span>
       <span class="badge sev-${esc(a.severity)}">${esc(a.severity)}</span>
       <span class="tick-n">${esc(a.package)}</span>
       <span class="tick-v">${esc(a.version || '')}</span>
       <span class="tick-d">${esc(a.kind)} · ${a.hops} hop${a.hops === 1 ? '' : 's'}`
      + (advisories.length ? ` · ${esc(advisories[0].id)}` : '') + `</span>`;
    box.prepend(row);
    while (box.children.length > 40) box.removeChild(box.lastChild);
    const as = q('#mon-alertstat');
    if (as) as.textContent = `streaming · ${fmt(mon.seen)} alert${mon.seen === 1 ? '' : 's'}`;
  }

  function wireMonitor() {
    const drop = q('#mondrop');
    const file = q('#monfile');
    const pick = q('#monpick');
    if (!drop || !file) return;

    const read = f => {
      const rd = new FileReader();
      rd.onload = () => register(rd.result, f.name);
      rd.readAsText(f);
    };

    pick && pick.addEventListener('click', () => file.click());
    drop.addEventListener('click', () => file.click());
    drop.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); file.click(); }
    });
    file.addEventListener('change', () => file.files[0] && read(file.files[0]));
    ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => {
      e.preventDefault(); drop.classList.add('over');
    }));
    ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => {
      e.preventDefault(); drop.classList.remove('over');
    }));
    drop.addEventListener('drop', e => {
      const f = e.dataTransfer && e.dataTransfer.files[0];
      if (f) read(f);
    });
  }

  /* ============================================================== 3 · boot */

  function start() {
    ingestion();
    ingestTicker();
    setInterval(ingestion, 6000);
    setInterval(ingestTicker, 6000);
    wireMonitor();

    // A registered project that outlives the tab would keep taking up graph
    // edges for nobody, so the stream is closed on the way out.
    window.addEventListener('beforeunload', closeStream);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
