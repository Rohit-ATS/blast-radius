/* The account dashboard.
 *
 * Monitors, alerts, keys and the security log. Alerts and monitor observations
 * arrive over server-sent events from /api/account/events, so nothing here
 * polls — a monitor finishing on the server repaints this page within a frame.
 */

'use strict';

(async () => {
  const { q, qq, api, esc, fmt, when, ago, toast } = window.BR;
  await window.BR.mount({ active: '' });

  const account = await window.BR.requireSession('/signin');
  if (!account) return;

  q('#hello').textContent = account.name ? `${account.name}'s watch` : 'Your watch';
  q('#subhead').textContent =
    `${account.email} · member since ${when(account.created_at)} · everything below updates live.`;

  let state = { monitors: [], alerts: [], keys: [], log: [], hooks: [], delivery: {} };
  let liveKey = null;                       // in memory only, never persisted

  /* ================================================================= tabs */

  function showTab(name) {
    qq('.dashnav button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
    qq('.tabpane').forEach(p => { p.hidden = p.dataset.pane !== name; });
    history.replaceState(null, '', `#${name}`);
  }
  qq('.dashnav button').forEach(b =>
    b.addEventListener('click', () => showTab(b.dataset.tab)));
  document.addEventListener('click', e => {
    const jump = e.target.closest('[data-tab-jump]');
    if (jump) showTab(jump.dataset.tabJump);
  });
  showTab((location.hash || '#overview').slice(1));

  /* ============================================================== loading */

  async function loadAll() {
    const [mon, al, keys, log, hooks] = await Promise.all([
      api('/api/monitors').catch(() => ({ monitors: [], interval_s: 0 })),
      api('/api/alerts?limit=80').catch(() => ({ alerts: [] })),
      api('/api/keys').catch(() => ({ keys: [] })),
      api('/api/security-log?limit=120').catch(() => ({ events: [] })),
      api('/api/webhooks').catch(() => ({ webhooks: [], delivery: {} })),
    ]);
    state = { monitors: mon.monitors, alerts: al.alerts, keys: keys.keys,
              log: log.events, hooks: hooks.webhooks, delivery: hooks.delivery || {} };
    q('#monint').textContent = mon.interval_s
      ? `re-measured every ${Math.round(mon.interval_s / 60) || 1}m`
      : 'watch idle';
    q('#watchnote').textContent = mon.interval_s
      ? `Each monitor is re-measured against the live graph roughly every ${Math.round(mon.interval_s / 60) || 1} minutes, `
        + `continuously, around the clock.`
      : 'The watch worker is not running.';
    paintAll();
  }

  function paintAll() {
    paintCounts();
    paintStats();
    paintMonitors();
    paintAlerts();
    paintKeys();
    paintHooks();
    paintLog();
  }

  function paintCounts() {
    q('#c-mon').textContent = state.monitors.length;
    q('#c-alert').textContent = state.alerts.filter(a => !a.read_at).length;
    q('#c-key').textContent = state.keys.filter(k => !k.revoked_at).length;
    q('#c-hook').textContent = state.hooks.filter(h => h.active).length;
  }

  /* ============================================================= overview */

  function paintStats() {
    const watched = state.monitors.length;
    const exposure = state.monitors.reduce((a, m) => a + (m.last_total || 0), 0);
    const calls = state.keys.reduce((a, k) => a + (k.calls || 0), 0);
    const unread = state.alerts.filter(a => !a.read_at).length;

    q('#stats').innerHTML = [
      ['Packages watched', fmt(watched), watched ? 'around the clock' : 'add one to begin'],
      ['Exposure under watch', fmt(exposure), 'packages downstream'],
      ['Unread alerts', fmt(unread), unread ? 'needs a look' : 'all clear'],
      ['API calls made', fmt(calls), `${state.keys.filter(k => !k.revoked_at).length} active keys`],
    ].map(([k, v, d]) =>
      `<div class="statbox"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`
    ).join('');

    q('#recentalerts').innerHTML = state.alerts.length
      ? state.alerts.slice(0, 4).map(alertRow).join('')
      : empty('i-alert', 'No alerts yet. Add a monitor and the watch starts reporting.');

    q('#recentmonitors').innerHTML = state.monitors.length
      ? state.monitors.slice(0, 5).map(monitorRow).join('')
      : empty('i-pulse', 'Nothing is being watched yet.');
    wireMonitorButtons();
  }

  const empty = (icon, text) => `
    <div class="empty-state">
      <svg viewBox="0 0 24 24"><use href="#${icon}"/></svg>
      <p style="margin:0">${esc(text)}</p>
    </div>`;

  /* ============================================================= monitors */

  const monitorRow = m => `
    <div class="row" data-monitor="${esc(m.id)}">
      <div class="grow">
        <div class="t">${esc(m.package)} ${m.label ? `<span class="muted" style="font-weight:400">· ${esc(m.label)}</span>` : ''}</div>
        <div class="s">
          ${m.last_total === null || m.last_total === undefined
            ? 'measuring…'
            : `${fmt(m.last_total)} packages exposed`}
          · checked ${ago(m.last_check_at)} · ${fmt(m.checks)} checks
        </div>
      </div>
      <div class="acts">
        ${m.last_status === 'error'
          ? '<span class="st bad">unreachable</span>'
          : (m.last_total || 0) >= 1000 ? '<span class="st bad">critical reach</span>'
          : (m.last_total || 0) >= 100 ? '<span class="st warn">high reach</span>'
          : '<span class="st ok">watched</span>'}
        <a class="icon-btn" href="/check?pkg=${encodeURIComponent(m.package)}" title="Run a full check">
          <svg viewBox="0 0 24 24"><use href="#i-search"/></svg></a>
        <button class="icon-btn danger" data-drop="${esc(m.id)}" title="Stop monitoring">
          <svg viewBox="0 0 24 24"><use href="#i-trash"/></svg></button>
      </div>
    </div>`;

  function paintMonitors() {
    q('#monlist').innerHTML = state.monitors.length
      ? state.monitors.map(monitorRow).join('')
      : empty('i-pulse', 'No monitors yet. Add a package above to start the watch.');
    wireMonitorButtons();
  }

  function wireMonitorButtons() {
    qq('[data-drop]').forEach(b => {
      if (b.dataset.wired) return;
      b.dataset.wired = '1';
      b.addEventListener('click', async () => {
        if (!confirm('Stop monitoring this package?')) return;
        try {
          await api(`/api/monitors/${b.dataset.drop}`, { method: 'DELETE' });
          toast('Monitor removed');
          loadAll();
        } catch (e) { toast(e.message, 'bad'); }
      });
    });
  }

  q('#monform').addEventListener('submit', async e => {
    e.preventDefault();
    const pkg = q('#monpkg').value.trim();
    if (!pkg) return;
    try {
      await api('/api/monitors', {
        method: 'POST',
        body: JSON.stringify({ package: pkg, label: q('#monlabel').value.trim() }),
      });
      q('#monpkg').value = ''; q('#monlabel').value = '';
      toast(`${pkg} added — measuring now`);
      loadAll();
    } catch (err) { toast(err.message, 'bad'); }
  });

  /* =============================================================== alerts */

  const ALERT_ICON = { info: 'i-check', notable: 'i-pulse', high: 'i-alert', critical: 'i-alert' };

  const alertRow = a => `
    <div class="alert-row ${esc(a.level)} ${a.read_at ? '' : 'unread'}">
      <span class="alert-ico"><svg viewBox="0 0 24 24"><use href="#${ALERT_ICON[a.level] || 'i-check'}"/></svg></span>
      <div class="grow">
        <div class="t">${esc(a.title)}</div>
        ${a.detail ? `<div class="d">${esc(a.detail)}</div>` : ''}
        <div class="w">${when(a.created_at)} · ${ago(a.created_at)}${a.data && a.data.package ? ` · ${esc(a.data.package)}` : ''}</div>
      </div>
      <span class="st ${a.level === 'critical' ? 'bad' : a.level === 'high' ? 'warn' : a.level === 'info' ? 'blue' : ''}">${esc(a.level)}</span>
    </div>`;

  function paintAlerts() {
    q('#alertlist').innerHTML = state.alerts.length
      ? state.alerts.map(alertRow).join('')
      : empty('i-alert', 'Nothing has moved yet. That is the good outcome.');
  }

  q('#markread').addEventListener('click', async () => {
    try {
      await api('/api/alerts/read', { method: 'POST' });
      state.alerts = state.alerts.map(a => ({ ...a, read_at: Date.now() / 1000 }));
      paintAll();
      toast('Alerts marked read');
    } catch (e) { toast(e.message, 'bad'); }
  });

  /* ================================================================= keys */

  function paintKeys() {
    const live = state.keys.filter(k => !k.revoked_at);
    q('#keysurface').innerHTML = `
      <div class="surface">
        <div class="surface-hd">
          <h3>Key vault</h3>
          <div class="sp">
            <span class="st">${live.length} active</span>
            <button class="btn btn-primary" id="newkey">
              <svg viewBox="0 0 24 24"><use href="#i-plus"/></svg> Create key
            </button>
          </div>
        </div>
        <div id="revealslot"></div>
        <div class="rows">${state.keys.length ? state.keys.map(keyRow).join('')
          : empty('i-key', 'No keys yet. Create one — the secret is shown once.')}</div>
      </div>`;

    q('#newkey').addEventListener('click', createKey);
    qq('[data-revoke]').forEach(b => b.addEventListener('click', () => revokeKey(b.dataset.revoke)));
  }

  const keyRow = k => `
    <div class="row">
      <div class="grow">
        <div class="t">${esc(k.name)} ${k.revoked_at ? '<span class="st bad">revoked</span>' : ''}</div>
        <div class="s">${esc(k.prefix)}…&nbsp; created ${when(k.created_at)} · ${fmt(k.calls)} calls · last used ${ago(k.last_used_at)}</div>
      </div>
      <div class="acts">${k.revoked_at ? '' :
        `<button class="icon-btn danger" data-revoke="${esc(k.id)}" title="Revoke">
           <svg viewBox="0 0 24 24"><use href="#i-trash"/></svg></button>`}</div>
    </div>`;

  async function createKey() {
    const name = prompt('Name this key (e.g. "CI", "staging")', 'New key');
    if (name === null) return;
    try {
      const out = await api('/api/keys', {
        method: 'POST', body: JSON.stringify({ name: name || 'New key' }),
      });
      liveKey = out.key.secret;
      await loadAll();
      showTab('keys');
      q('#revealslot').innerHTML = `
        <div style="padding:22px 24px;border-bottom:1px solid var(--line-2)">
          <div class="notice">
            <svg viewBox="0 0 24 24"><use href="#i-alert"/></svg>
            <div><b>${esc(out.key.name)}</b> is shown once. It is stored only as a SHA-256
              digest — if you lose it, revoke it and make another.</div>
          </div>
          <div class="secret">
            <code id="newsecret">${esc(out.key.secret)}</code>
            <button class="ghost-btn" data-copy="#newsecret">
              <svg viewBox="0 0 24 24"><use href="#i-copy"/></svg> Copy</button>
          </div>
        </div>`;
      window.BR.wireCopy(q('#revealslot'));
      toast('Key created — copy it now');
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function revokeKey(id) {
    if (!confirm('Revoke this key? Anything using it stops working immediately.')) return;
    try {
      await api(`/api/keys/${id}`, { method: 'DELETE' });
      toast('Key revoked');
      loadAll();
    } catch (e) { toast(e.message, 'bad'); }
  }

  /* =========================================================== webhooks */

  const hookRow = h => `
    <div class="row">
      <div class="grow">
        <div class="t">${esc(h.label || h.url)} ${h.active ? '' : '<span class="st bad">disabled</span>'}</div>
        <div class="s">${esc(h.url)}</div>
        <div class="s">${fmt(h.deliveries)} delivered · ${fmt(h.failures)} failed
          ${h.last_at ? `· last ${ago(h.last_at)}: ${esc(h.last_detail || '')}` : '· never fired'}</div>
      </div>
      <div class="acts">
        ${h.last_ok === null || h.last_ok === undefined ? ''
          : h.last_ok ? '<span class="st ok">healthy</span>' : '<span class="st bad">failing</span>'}
        <button class="icon-btn" data-hooktest="${esc(h.id)}" title="Send a real test delivery">
          <svg viewBox="0 0 24 24"><use href="#i-bolt"/></svg></button>
        <button class="icon-btn danger" data-hookdrop="${esc(h.id)}" title="Remove">
          <svg viewBox="0 0 24 24"><use href="#i-trash"/></svg></button>
      </div>
    </div>`;

  function paintHooks() {
    q('#hooklist').innerHTML = state.hooks.length
      ? state.hooks.map(hookRow).join('')
      : empty('i-out', 'No endpoints yet. Add one and alerts start arriving as signed POSTs.');

    const email = state.delivery.email;
    q('#emailstate').innerHTML = email === 'smtp'
      ? `<svg viewBox="0 0 24 24"><use href="#i-check"/></svg>
         <div><b>Email delivery is on.</b> High and critical alerts are emailed to
         ${esc(account.email)} as well as appearing here.</div>`
      : `<svg viewBox="0 0 24 24"><use href="#i-at"/></svg>
         <div><b>Email delivery is off.</b> Alerts reach this dashboard and any webhooks
         below. To also email them, set <code>SMTP_HOST</code>, <code>SMTP_FROM</code> and
         credentials in this deployment's <code>.env</code>.</div>`;

    qq('[data-hooktest]').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        const out = await api(`/api/webhooks/${b.dataset.hooktest}/test`, { method: 'POST' });
        toast(out.delivered ? `Delivered: ${out.detail}` : `Failed: ${out.detail}`,
              out.delivered ? 'ok' : 'bad');
        loadAll();
      } catch (e) { toast(e.message, 'bad'); b.disabled = false; }
    }));
    qq('[data-hookdrop]').forEach(b => b.addEventListener('click', async () => {
      if (!confirm('Remove this endpoint? Alerts stop being delivered to it.')) return;
      try {
        await api(`/api/webhooks/${b.dataset.hookdrop}`, { method: 'DELETE' });
        toast('Endpoint removed');
        loadAll();
      } catch (e) { toast(e.message, 'bad'); }
    }));
  }

  q('#hookform').addEventListener('submit', async e => {
    e.preventDefault();
    const url = q('#hookurl').value.trim();
    if (!url) return;
    try {
      const out = await api('/api/webhooks', {
        method: 'POST',
        body: JSON.stringify({ url, label: q('#hooklabel').value.trim() }),
      });
      q('#hookurl').value = ''; q('#hooklabel').value = '';
      await loadAll();
      showTab('delivery');
      // The signing secret is shown once, in the same breath as the endpoint.
      q('#hooklist').insertAdjacentHTML('afterbegin', `
        <div style="padding:22px 24px;border-bottom:1px solid var(--line-2)">
          <div class="notice">
            <svg viewBox="0 0 24 24"><use href="#i-alert"/></svg>
            <div>This endpoint's signing secret is shown once. Store it where your
              receiver can read it — you need it to verify every delivery.</div>
          </div>
          <div class="secret">
            <code id="hooksecret">${esc(out.webhook.secret)}</code>
            <button class="ghost-btn" data-copy="#hooksecret">
              <svg viewBox="0 0 24 24"><use href="#i-copy"/></svg> Copy</button>
          </div>
        </div>`);
      window.BR.wireCopy(q('#hooklist'));
      toast('Endpoint added — copy the secret now');
    } catch (err) { toast(err.message, 'bad'); }
  });

  /* ========================================================== security log */

  function paintLog() {
    q('#logtable').innerHTML =
      `<tr><th>Event</th><th>Detail</th><th>Address</th><th>When</th></tr>` +
      (state.log.length
        ? state.log.map(e => `<tr>
            <td class="ev">${esc(e.event)}</td>
            <td class="dim">${esc(e.detail || '—')}</td>
            <td class="dim">${esc(e.ip || '—')}</td>
            <td class="dim">${when(e.at)}</td>
          </tr>`).join('')
        : `<tr><td colspan="4" class="dim" style="padding:26px 24px">Nothing logged yet.</td></tr>`);
  }

  /* ============================================================= realtime */

  function connect() {
    const stat = q('#rtstat');
    const es = new EventSource('/api/account/events');

    es.onopen = () => {
      stat.className = 'st ok';
      stat.innerHTML = `<svg viewBox="0 0 24 24"><use href="#i-pulse"/></svg> live`;
    };

    es.onmessage = ev => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }

      if (msg.type === 'alert') {
        state.alerts.unshift(msg.alert);
        paintAll();
        toast(msg.alert.title, msg.alert.level === 'critical' || msg.alert.level === 'high' ? 'bad' : 'ok');
      } else if (msg.type === 'monitor') {
        const m = state.monitors.find(x => x.id === msg.monitor_id);
        if (m) { m.last_total = msg.total; m.last_check_at = msg.at; m.checks = (m.checks || 0) + 1; }
        paintAll();
      }
    };

    es.onerror = () => {
      stat.className = 'st warn';
      stat.innerHTML = `<svg viewBox="0 0 24 24"><use href="#i-pulse"/></svg> reconnecting`;
      // EventSource reconnects on its own; this only reports the gap honestly.
    };
  }

  /* ================================================================= boot */

  q('#signout').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
    location.href = '/';
  });

  await loadAll();
  connect();

  // The relative timestamps go stale on a page left open; the data does not.
  setInterval(paintAll, 30000);
})();
