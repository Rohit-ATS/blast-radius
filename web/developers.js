/* The API page: keys, quickstarts, a live playground and the full reference.
 *
 * The reference is fetched from /api/docs.json — the same structure that
 * renders /api/docs.md and /api/docs.txt — so this page can never describe an
 * endpoint the server does not route.
 */

'use strict';

(async () => {
  const { q, qq, api, esc, when, ago, toast, copy } = window.BR;
  await window.BR.mount({ active: '/developers' });

  const ORIGIN = location.origin;
  const PLACEHOLDER = 'brk_live_YOUR_KEY';
  let docs = null;
  let liveKey = null;          // only ever the one just created, in memory

  // A key just created in this tab, or one the reader pasted into the
  // playground. Either way it lives in this closure and nowhere else — a
  // reload loses it, which is the correct trade for never persisting a secret.
  const activeKey = () => (q('#playkey')?.value.trim() || liveKey || '');
  const keyForSnippets = () => activeKey() || PLACEHOLDER;

  /* ================================================================= keys */

  async function paintKeys() {
    const host = q('#keysurface');

    if (!window.BR.session.account) {
      host.innerHTML = `
        <div class="surface surface-pad">
          <div class="empty-state">
            <svg viewBox="0 0 24 24"><use href="#i-key"/></svg>
            <p style="margin:0 0 20px">Sign in to create API keys. It takes one form and no card.</p>
            <a class="btn btn-primary" href="/signin?next=%2Fdevelopers">Create a free account</a>
          </div>
        </div>`;
      return;
    }

    let keys = [];
    try { keys = (await api('/api/keys')).keys; } catch (e) { toast(e.message, 'bad'); }

    const live = keys.filter(k => !k.revoked_at);
    host.innerHTML = `
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
        <div class="rows">${keys.length ? keys.map(row).join('') : emptyKeys()}</div>
      </div>`;

    q('#newkey').addEventListener('click', createKey);
    qq('[data-revoke]').forEach(b => b.addEventListener('click', () => revoke(b.dataset.revoke)));
  }

  const emptyKeys = () => `
    <div class="empty-state">
      <svg viewBox="0 0 24 24"><use href="#i-key"/></svg>
      <p style="margin:0">No keys yet. Create one and the secret appears here, once.</p>
    </div>`;

  const row = k => `
    <div class="row">
      <div class="grow">
        <div class="t">${esc(k.name)} ${k.revoked_at ? '<span class="st bad">revoked</span>' : ''}</div>
        <div class="s">${esc(k.prefix)}…&nbsp; created ${when(k.created_at)} · ${k.calls.toLocaleString('en-US')} calls · last used ${ago(k.last_used_at)}</div>
      </div>
      <div class="acts">
        ${k.revoked_at ? '' :
          `<button class="icon-btn danger" data-revoke="${esc(k.id)}" title="Revoke this key">
             <svg viewBox="0 0 24 24"><use href="#i-trash"/></svg></button>`}
      </div>
    </div>`;

  async function createKey() {
    const name = prompt('Name this key (e.g. "CI", "staging")', 'New key');
    if (name === null) return;
    try {
      const out = await api('/api/keys', {
        method: 'POST', body: JSON.stringify({ name: name || 'New key' }),
      });
      liveKey = out.key.secret;
      await paintKeys();
      reveal(out.key);
      const field = q('#playkey');
      if (field) field.value = out.key.secret;     // ready to send immediately
      paintQuickstart();
      paintPlayground();
      toast('Key created — copy it now');
    } catch (e) { toast(e.message, 'bad'); }
  }

  function reveal(key) {
    q('#revealslot').innerHTML = `
      <div style="padding:22px 24px;border-bottom:1px solid var(--line-2)">
        <div class="notice">
          <svg viewBox="0 0 24 24"><use href="#i-alert"/></svg>
          <div><b>${esc(key.name)}</b> is shown once. It is stored only as a SHA-256 digest —
            if you lose it, revoke it and make another.</div>
        </div>
        <div class="secret">
          <code id="newsecret">${esc(key.secret)}</code>
          <button class="ghost-btn" data-copy="#newsecret">
            <svg viewBox="0 0 24 24"><use href="#i-copy"/></svg> Copy
          </button>
        </div>
      </div>`;
    window.BR.wireCopy(q('#revealslot'));
  }

  async function revoke(id) {
    if (!confirm('Revoke this key? Anything using it stops working immediately.')) return;
    try {
      await api(`/api/keys/${id}`, { method: 'DELETE' });
      toast('Key revoked');
      paintKeys();
    } catch (e) { toast(e.message, 'bad'); }
  }

  /* =========================================================== quickstart */

  let qsIndex = 0;

  function paintQuickstart() {
    if (!docs) return;
    q('#qstabs').innerHTML = docs.quickstarts.map((s, i) =>
      `<button class="tab ${i === qsIndex ? 'on' : ''}" data-qs="${i}">${esc(s.label)}</button>`).join('');
    qq('[data-qs]').forEach(b => b.addEventListener('click', () => {
      qsIndex = +b.dataset.qs; paintQuickstart();
    }));

    const s = docs.quickstarts[qsIndex];
    const code = s.code.replaceAll(PLACEHOLDER, keyForSnippets());
    q('#qsbody').innerHTML = `
      <div class="code">
        <div class="code-hd">
          <span class="lang">${esc(s.lang)}</span>
          <span class="sp"><button class="ghost-btn" data-copy="#qscode">
            <svg viewBox="0 0 24 24"><use href="#i-copy"/></svg> Copy</button></span>
        </div>
        <pre id="qscode">${esc(code)}</pre>
      </div>
      ${liveKey ? '' : `<p class="lede" style="margin-top:14px;font-size:14px">
        <span class="muted">Create a key above and these snippets fill themselves in.</span></p>`}`;
    window.BR.wireCopy(q('#qsbody'));
  }

  /* =========================================================== playground */

  const PLAY = [
    { id: 'blast',       label: 'GET /api/v1/blast',       needs: ['name'] },
    { id: 'resolve',     label: 'GET /api/v1/resolve',     needs: ['name', 'ver'] },
    { id: 'maintainers', label: 'GET /api/v1/maintainers', needs: ['name'] },
    { id: 'typosquats',  label: 'GET /api/v1/typosquats',  needs: ['name'] },
    { id: 'subgraph',    label: 'GET /api/v1/subgraph',    needs: ['name'] },
    { id: 'whoami',      label: 'GET /api/v1/whoami',      needs: [] },
    { id: 'alerts',      label: 'GET /api/v1/alerts',      needs: [] },
  ];

  function playURL() {
    const ep = q('#playep').value;
    const name = q('#playname').value.trim();
    const ver = q('#playver').value.trim();
    const p = new URLSearchParams();
    const spec = PLAY.find(x => x.id === ep);
    if (spec.needs.includes('name')) p.set('name', name);
    if (spec.needs.includes('ver')) p.set('bad_version', ver);
    if (ep === 'blast') p.set('depth', '5');
    const qs = p.toString();
    return `/api/v1/${ep}${qs ? '?' + qs : ''}`;
  }

  function paintPlayground() {
    const url = playURL();
    q('#playreq').textContent =
      `curl -H 'Authorization: Bearer ${keyForSnippets()}' \\\n  '${ORIGIN}${url}'`;
  }

  async function runPlayground() {
    const btn = q('#playrun'), stat = q('#playstat');
    const url = playURL();

    const key = activeKey();
    if (!key) {
      if (!window.BR.session.account) {
        toast('Sign in to create a key, or paste one above', 'bad');
        location.href = '/signin?next=%2Fdevelopers';
      } else {
        toast('Paste a key above, or create one — this sends a real authenticated call', 'bad');
        q('#playkey').focus();
      }
      return;
    }

    btn.disabled = true;
    stat.textContent = 'sending…';
    stat.className = 'st';
    const t0 = performance.now();
    try {
      const res = await fetch(url, { headers: { Authorization: `Bearer ${key}` } });
      const body = await res.json();
      const ms = Math.round(performance.now() - t0);
      q('#playres').textContent = JSON.stringify(body, null, 2);
      stat.textContent = `${res.status} · ${ms}ms`;
      stat.className = `st ${res.ok ? 'ok' : 'bad'}`;
      if (window.BR.session.account) paintKeys();   // call counters just moved
    } catch (err) {
      q('#playres').textContent = String(err);
      stat.textContent = 'failed';
      stat.className = 'st bad';
    }
    btn.disabled = false;
  }

  /* ============================================================ reference */

  function paintReference() {
    q('#endpoints').innerHTML = docs.endpoints.map(e => `
      <div class="row" style="align-items:flex-start;flex-direction:column;gap:14px">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;width:100%">
          <span class="st ${e.method === 'POST' ? 'warn' : 'blue'}">${e.method}</span>
          <code style="font-family:var(--mono);font-size:14.5px;font-weight:600">${esc(e.path)}</code>
          <span class="muted" style="font-size:13.5px">${esc(e.title)}</span>
        </div>
        <p class="lede" style="font-size:14.5px;margin:0">${esc(e.summary)}</p>
        ${e.params && e.params.length && e.params[0][0] !== '(none)' ? `
          <table class="logtable" style="border:1px solid var(--line);border-radius:10px;overflow:hidden">
            <tr><th>Parameter</th><th>Type</th><th>Notes</th><th>Description</th></tr>
            ${e.params.map(p => `<tr>
              <td class="ev">${esc(p[0])}</td><td class="dim">${esc(p[1])}</td>
              <td class="dim">${esc(p[2])}</td>
              <td style="font-family:var(--sans);font-size:13.5px">${esc(p[3])}</td>
            </tr>`).join('')}
          </table>` : ''}
        ${e.body ? `<p class="mono-sm muted" style="margin:0"><b>Body</b> — ${esc(e.body)}</p>` : ''}
        <div class="code" style="width:100%">
          <div class="code-hd">
            <span class="lang">example response</span>
            <span class="sp"><button class="ghost-btn" data-copy="#res-${esc(e.path.replace(/\W/g, ''))}">
              <svg viewBox="0 0 24 24"><use href="#i-copy"/></svg> Copy</button></span>
          </div>
          <pre id="res-${esc(e.path.replace(/\W/g, ''))}">${esc(JSON.stringify(e.response, null, 2))}</pre>
        </div>
      </div>`).join('');
    window.BR.wireCopy(q('#endpoints'));

    q('#errtable').innerHTML =
      `<tr><th>Status</th><th>Code</th><th>Meaning</th></tr>` +
      docs.errors.map(e => `<tr>
        <td class="ev">${esc(e.status)}</td>
        <td class="dim">${esc(e.code)}</td>
        <td style="font-family:var(--sans);font-size:13.5px">${esc(e.meaning)}</td>
      </tr>`).join('');

    q('#monitorsnip').textContent =
      `# start watching a package\n` +
      `curl -X POST '${ORIGIN}/api/v1/monitors' \\\n` +
      `  -H 'Authorization: Bearer ${keyForSnippets()}' \\\n` +
      `  -H 'Content-Type: application/json' \\\n` +
      `  -d '{"package":"debug"}'\n\n` +
      `# read what the watch has raised\n` +
      `curl -H 'Authorization: Bearer ${keyForSnippets()}' \\\n` +
      `  '${ORIGIN}/api/v1/alerts?limit=20'`;
  }

  /* ================================================================= boot */

  try {
    docs = await api('/api/docs.json');
  } catch (e) {
    toast('could not load the API reference', 'bad');
    return;
  }

  q('#playep').innerHTML = PLAY.map(p =>
    `<option value="${p.id}">${esc(p.label)}</option>`).join('');
  ['#playep', '#playname', '#playver', '#playkey'].forEach(s =>
    q(s).addEventListener('input', paintPlayground));
  q('#playrun').addEventListener('click', runPlayground);
  q('#copyreq').addEventListener('click', e =>
    copy(q('#playreq').textContent, e.currentTarget));
  q('#copyres').addEventListener('click', e =>
    copy(q('#playres').textContent, e.currentTarget));

  // Whole-reference copy, straight from the server's own renderers.
  q('#copytxt').addEventListener('click', async e => {
    const text = await (await fetch('/api/docs.txt')).text();
    copy(text, e.currentTarget);
  });
  q('#copymd').addEventListener('click', async e => {
    const text = await (await fetch('/api/docs.md')).text();
    copy(text, e.currentTarget);
  });

  await paintKeys();
  paintQuickstart();
  paintPlayground();
  paintReference();
})();
