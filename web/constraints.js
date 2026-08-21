/* The constraints page — renders whatever the live sweep actually found.
 *
 * Nothing here has a hardcoded expectation. If a constraint stops holding, the
 * page says SURPRISE and shows what came back instead; the point of publishing
 * a measurement rather than a claim is that it is allowed to disagree with us.
 */
(function () {
  'use strict';

  const q = s => document.querySelector(s);
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  const ago = s => s == null ? 'never'
    : s < 60 ? `${Math.round(s)}s ago`
    : s < 3600 ? `${Math.round(s / 60)}m ago`
    : `${Math.round(s / 3600)}h ago`;

  let timer = null;

  async function load(force) {
    let d;
    try {
      const r = await fetch('/api/constraints' + (force ? '?refresh=1' : ''));
      d = await r.json();
    } catch (e) {
      q('#cstatline').textContent = 'could not reach the API';
      q('#cpulse').className = 'pulse dead';
      return;
    }

    if (d.ok === false) {
      q('#cstatline').textContent = esc(d.message || 'probe failed');
      q('#cpulse').className = 'pulse dead';
      q('#cbanner').innerHTML =
        `<div class="skel">${esc(d.message || 'the probe could not run')}</div>`;
      return;
    }

    if (!d.ready) {
      // A sweep takes about a minute, most of it one count(*) over a label —
      // which is finding number seven, so it is worth saying out loud rather
      // than hiding behind a spinner.
      q('#cpulse').className = 'pulse';
      q('#cstatline').textContent = 'probing…';
      q('#cbanner').innerHTML = `<div class="skel">${esc(d.message || 'measuring…')}</div>`;
      if (!timer) timer = setInterval(() => load(false), 4000);
      return;
    }

    if (timer) { clearInterval(timer); timer = null; }
    render(d);
    setTimeout(() => load(false), 60000);
  }

  function render(d) {
    const s = d.summary || {};
    q('#cpulse').className = 'pulse ' + (d.measuring ? '' : 'live');
    q('#cstatline').textContent =
      `${s.probes} probes · measured ${ago(d.age_s)}` + (d.measuring ? ' · re-running' : '');
    const hy = q('#chydra');
    if (hy) hy.textContent = d.hydra_url || 'hydradb';

    const surprises = d.surprises || [];
    q('#cbanner').innerHTML = `
      <div class="cb-grid">
        <div class="cb"><b>${s.probes}</b><span>probes executed live</span></div>
        <div class="cb"><b>${s.predictions}</b><span>were predictions</span></div>
        <div class="cb ${surprises.length ? 'bad' : 'ok'}">
          <b>${surprises.length}</b><span>${surprises.length ? 'contradicted us' : 'surprises — the map holds'}</span></div>
        <div class="cb"><b>${s.traps_confirmed}/${s.traps_total}</b><span>silent traps reproduced now</span></div>
      </div>` + (surprises.length ? `
      <div class="cb-surprise">
        <b>The documented map is wrong here.</b>
        ${surprises.map(x => `<div class="csrow"><code>${esc(x.label)}</code>
          expected <b>${esc(x.expected)}</b>, got <b>${esc(x.observed)}</b>
          <span class="cdet">${esc(x.detail)}</span></div>`).join('')}
      </div>` : '');

    q('#trapcards').innerHTML = (d.traps || []).map(trapCard).join('');
    q('#groups').innerHTML = (d.groups || []).map(groupTable).join('');
    q('#narrativerows').innerHTML = (d.narrative || []).map(n => `
      <div class="nrow">
        <div class="nlabel">${esc(n.label)}</div>
        <div class="ndetail">${esc(n.detail)}</div>
      </div>`).join('');
  }

  function trapCard(t) {
    if (t.unavailable) {
      return `<div class="trap na">
        <div class="trap-head"><span class="tbadge na">could not run</span>
          <b>${esc(t.label)}</b></div>
        <div class="trap-cost">${esc(t.detail || '')}</div></div>`;
    }
    // The read-only trap is the one where "does not hold" is the good news, so
    // the badge has to say what was observed rather than pass/fail.
    const state = t.holds === true ? 'reproduced'
      : t.holds === false ? 'not right now' : 'measured';
    const cls = t.holds === true ? 'yes' : t.holds === false ? 'no' : 'na';
    return `<div class="trap">
      <div class="trap-head">
        <span class="tbadge ${cls}">${esc(state)}</span><b>${esc(t.label)}</b>
      </div>
      ${t.cost ? `<div class="trap-cost">${esc(t.cost)}</div>` : ''}
      <div class="trap-split">
        ${side('the trap', t.wrong, 'bad')}
        ${side('what we do instead', t.right, 'good')}
      </div>
    </div>`;
  }

  function side(title, b, cls) {
    if (!b) return '';
    return `<div class="tside ${cls}">
      <div class="tside-t">${esc(title)}</div>
      <pre class="tq">${esc(b.query)}</pre>
      <div class="tres">${esc(b.result)}</div>
      <div class="tread">${esc(b.reading)}</div>
      ${b.ms ? `<div class="tms">${b.ms}ms</div>` : ''}
    </div>`;
  }

  function groupTable(g) {
    return `<div class="gwrap">
      <h3 class="gtitle">${esc(g.title)} <span class="gcount">${g.rows.length}</span></h3>
      <div class="gtable">
        ${g.rows.map(r => {
          const cls = r.holds === false ? 'surprise'
            : r.observed === 'FAILS' ? 'fails' : 'works';
          const badge = r.holds === false ? 'SURPRISE' : r.observed;
          return `<div class="grow ${cls}">
            <div class="gcell gbadge"><span class="rbadge ${cls}">${esc(badge)}</span></div>
            <div class="gcell gmain">
              <div class="glabel">${esc(r.label)}</div>
              <pre class="gq">${esc(r.query)}</pre>
              <div class="gdetail">${esc(r.detail)}</div>
              ${r.instead ? `<div class="ginstead"><b>Instead:</b> ${esc(r.instead)}</div>` : ''}
            </div>
            <div class="gcell gms">${r.ms}ms</div>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }

  q('#creload').addEventListener('click', () => {
    q('#cstatline').textContent = 'probing…';
    load(true);
  });

  load(false);
})();
