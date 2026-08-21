/* The shell every page shares: icon sprite, header, footer, session state.
 *
 * There is no build step and no templating engine in this project, so a page
 * that hand-copied its own header would drift from the others within a day.
 * Instead each page ships only its own content and calls into this, which
 * renders identical chrome and keeps the signed-in state in one place.
 */

'use strict';

window.BR = (() => {
  const q  = (s, r = document) => r.querySelector(s);
  const qq = (s, r = document) => [...r.querySelectorAll(s)];
  const fmt = n => (typeof n === 'number' ? n.toLocaleString('en-US') : '—');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const REPO = 'https://github.com/Rohit-ATS/blast-radius';

  /* ------------------------------------------------------------- requests */

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      credentials: 'same-origin',
      headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
      ...opts,
    });
    let body = {};
    try { body = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok || body.ok === false) {
      throw Object.assign(
        new Error(body.message || body.error || `request failed (${res.status})`),
        { status: res.status, code: body.error, body });
    }
    return body;
  }

  /* ----------------------------------------------------------------- time */

  function ago(ts) {
    if (!ts) return 'never';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  function when(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString('en-US',
      { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  }

  /* --------------------------------------------------------------- icons */

  const SPRITE = `
<svg class="spritesheet" aria-hidden="true" focusable="false"><defs>
  <symbol id="i-logo" viewBox="0 0 32 32">
    <circle cx="16" cy="16" r="15" fill="none" stroke="currentColor" stroke-width="1" opacity=".22"/>
    <circle cx="16" cy="16" r="10.5" fill="none" stroke="currentColor" stroke-width="1.7" opacity=".5"/>
    <circle cx="16" cy="16" r="5.6" fill="currentColor"/>
  </symbol>
  <symbol id="i-npm" viewBox="0 0 24 24">
    <rect width="24" height="24" rx="2.6" fill="currentColor"/>
    <path d="M5 6.2h14v11.6h-7V8.9H9.6v8.9H5z" fill="#fff"/>
  </symbol>
  <symbol id="i-github" viewBox="0 0 24 24">
    <path fill="currentColor" d="M12 .5a11.5 11.5 0 0 0-3.64 22.42c.58.1.79-.25.79-.55v-2.1c-3.2.7-3.88-1.37-3.88-1.37-.53-1.34-1.29-1.7-1.29-1.7-1.06-.72.08-.7.08-.7 1.17.08 1.78 1.2 1.78 1.2 1.04 1.79 2.74 1.27 3.4.97.11-.76.41-1.28.74-1.57-2.55-.29-5.24-1.28-5.24-5.7 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11 11 0 0 1 5.8 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.24 2.76.12 3.05.74.81 1.19 1.84 1.19 3.1 0 4.43-2.7 5.4-5.26 5.69.42.36.79 1.08.79 2.18v3.23c0 .3.21.66.8.55A11.5 11.5 0 0 0 12 .5Z"/>
  </symbol>
  <symbol id="i-radius" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="2.7" fill="currentColor"/>
    <circle cx="12" cy="12" r="6.5" fill="none" stroke="currentColor" stroke-width="1.6" opacity=".6"/>
    <circle cx="12" cy="12" r="10.3" fill="none" stroke="currentColor" stroke-width="1.3" opacity=".3"/>
  </symbol>
  <symbol id="i-lock" viewBox="0 0 24 24">
    <rect x="4.2" y="10.2" width="15.6" height="11" rx="2.6" fill="none" stroke="currentColor" stroke-width="1.7"/>
    <path fill="none" stroke="currentColor" stroke-width="1.7" d="M8 10.2V7.6a4 4 0 0 1 8 0v2.6"/>
    <circle cx="12" cy="15.6" r="1.5" fill="currentColor"/>
  </symbol>
  <symbol id="i-key" viewBox="0 0 24 24">
    <circle cx="8" cy="8.4" r="4.6" fill="none" stroke="currentColor" stroke-width="1.7"/>
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
          d="m11.4 11.6 8.4 8.4M17 17.2l2-2M14.4 14.6l2-2"/>
  </symbol>
  <symbol id="i-pulse" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
          d="M2 13h4.2l2.4-7 3.6 14 2.6-8.4 1.7 3.4H22"/>
  </symbol>
  <symbol id="i-shield" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"
          d="M12 2.6 20 5.6v6.1c0 4.6-3.2 8.3-8 9.7-4.8-1.4-8-5.1-8-9.7V5.6Z"/>
    <path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m8.4 12.1 2.5 2.5 4.7-4.9"/>
  </symbol>
  <symbol id="i-graph" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.5" d="M6.6 7.6 12 5.2m5.4 2.4L12 5.2M6.6 8.4l3.1 7.2m4.6 0 3.1-7.2"/>
    <circle cx="12" cy="4" r="2.3" fill="currentColor"/>
    <circle cx="5.4" cy="8.6" r="2.1" fill="currentColor" opacity=".72"/>
    <circle cx="18.6" cy="8.6" r="2.1" fill="currentColor" opacity=".72"/>
    <circle cx="8.4" cy="18.4" r="2.1" fill="currentColor" opacity=".46"/>
    <circle cx="15.6" cy="18.4" r="2.1" fill="currentColor" opacity=".46"/>
  </symbol>
  <symbol id="i-trend" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3 17.5 9.5 11l4 4L21 7.5"/>
    <path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M15.5 7.5H21v5.5"/>
  </symbol>
  <symbol id="i-clock" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.7"/>
    <path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M12 7v5.3l3.4 2"/>
  </symbol>
  <symbol id="i-check" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="9.4" fill="none" stroke="currentColor" stroke-width="1.5" opacity=".5"/>
    <path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m8 12.3 2.7 2.7L16.2 9"/>
  </symbol>
  <symbol id="i-search" viewBox="0 0 24 24">
    <circle cx="10.8" cy="10.8" r="6.6" fill="none" stroke="currentColor" stroke-width="1.8"/>
    <path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="m15.8 15.8 4.2 4.2"/>
  </symbol>
  <symbol id="i-doc" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M6 2.8h8l4.2 4.2v14.2H6Z"/>
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M14 2.8V7h4.2"/>
    <path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" d="M9 12h6M9 16h6"/>
  </symbol>
  <symbol id="i-book" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M3.4 4.6h6.2A2.4 2.4 0 0 1 12 7v13a2 2 0 0 0-2-1.6H3.4Z"/>
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M20.6 4.6h-6.2A2.4 2.4 0 0 0 12 7v13a2 2 0 0 1 2-1.6h6.6Z"/>
  </symbol>
  <symbol id="i-bolt" viewBox="0 0 24 24"><path fill="currentColor" d="M13.4 2 5 13.6h5.3L9.6 22 19 10.2h-5.6Z"/></symbol>
  <symbol id="i-alert" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M12 3.2 22 20.4H2Z"/>
    <path fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" d="M12 9.8v4.4"/>
    <circle cx="12" cy="17.2" r="1.1" fill="currentColor"/>
  </symbol>
  <symbol id="i-at" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="1.6"/>
    <path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" d="M16 8v5.4a2.6 2.6 0 0 0 5.2 0V12A9.2 9.2 0 1 0 15 20.7"/>
  </symbol>
  <symbol id="i-cube" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" d="M12 2.8 20.5 7v10L12 21.2 3.5 17V7Z"/>
    <path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" d="M3.5 7 12 11.4 20.5 7M12 11.4v9.8"/>
  </symbol>
  <symbol id="i-db" viewBox="0 0 24 24">
    <ellipse cx="12" cy="6" rx="7.6" ry="3.1" fill="none" stroke="currentColor" stroke-width="1.6"/>
    <path fill="none" stroke="currentColor" stroke-width="1.6" d="M4.4 6v12c0 1.7 3.4 3.1 7.6 3.1s7.6-1.4 7.6-3.1V6M4.4 12c0 1.7 3.4 3.1 7.6 3.1s7.6-1.4 7.6-3.1"/>
  </symbol>
  <symbol id="i-yarn" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="9.2" fill="none" stroke="currentColor" stroke-width="1.7"/>
    <path fill="currentColor" d="M7.4 12.6c2.8-.8 4.3-2.4 4.7-4.8 1 1.7 1.1 3.3.5 5 1.7-.5 3.1-.2 4.2 1-2 .6-3.7 1.7-5 3.4-1.7-1.7-3.2-3.2-4.4-4.6Z"/>
  </symbol>
  <symbol id="i-pnpm" viewBox="0 0 24 24">
    <g fill="currentColor">
      <rect x="2" y="2" width="6" height="6" rx="1.2"/><rect x="9" y="2" width="6" height="6" rx="1.2"/><rect x="16" y="2" width="6" height="6" rx="1.2"/>
      <rect x="2" y="9" width="6" height="6" rx="1.2" opacity=".5"/><rect x="9" y="9" width="6" height="6" rx="1.2"/><rect x="16" y="9" width="6" height="6" rx="1.2" opacity=".5"/>
      <rect x="9" y="16" width="6" height="6" rx="1.2" opacity=".3"/><rect x="16" y="16" width="6" height="6" rx="1.2" opacity=".5"/>
    </g>
  </symbol>
  <symbol id="i-osv" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M12 2.6 20.4 7.3v9.4L12 21.4 3.6 16.7V7.3Z"/>
    <circle cx="12" cy="12" r="3.4" fill="currentColor"/>
  </symbol>
  <symbol id="i-copy" viewBox="0 0 24 24">
    <rect x="8.6" y="8.6" width="12" height="12" rx="2.4" fill="none" stroke="currentColor" stroke-width="1.7"/>
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M15.4 5.4H5.8a2.4 2.4 0 0 0-2.4 2.4v9.6"/>
  </symbol>
  <symbol id="i-plus" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M12 5v14M5 12h14"/>
  </symbol>
  <symbol id="i-trash" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
          d="M4 7h16M9.5 7V4.8h5V7M6.4 7l1 12.2a2 2 0 0 0 2 1.8h5.2a2 2 0 0 0 2-1.8L17.6 7"/>
  </symbol>
  <symbol id="i-out" viewBox="0 0 24 24">
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
          d="M14 4h6v6M20 4 11 13M18 14v5a1.8 1.8 0 0 1-1.8 1.8H5A1.8 1.8 0 0 1 3.2 19V7.8A1.8 1.8 0 0 1 5 6h5"/>
  </symbol>
  <symbol id="i-user" viewBox="0 0 24 24">
    <circle cx="12" cy="8.4" r="4" fill="none" stroke="currentColor" stroke-width="1.7"/>
    <path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M4.6 20.4a7.6 7.6 0 0 1 14.8 0"/>
  </symbol>
</defs></svg>`;

  /* -------------------------------------------------------------- session */

  let session = { account: null, provider: 'local', loaded: false };

  async function loadSession() {
    try {
      const r = await api('/api/auth/me');
      session = { account: r.account, provider: r.provider, usage: r.usage, loaded: true };
    } catch {
      session = { account: null, provider: 'local', loaded: true };
    }
    paintHeader();
    return session;
  }

  const NAV = [
    { href: '/check', label: 'Run a check' },
    { href: '/developers', label: 'API' },
    { href: '/#analytics', label: 'Analytics' },
    { href: '/#pricing', label: 'Pricing' },
  ];

  function headerHTML(active) {
    const nav = NAV.map(n =>
      `<a href="${n.href}"${active === n.href ? ' class="on"' : ''}>${n.label}</a>`).join('');
    return `
<header class="hdr">
  <div class="wrap">
    <a class="logo" href="/">
      <span class="mark"><svg viewBox="0 0 32 32"><use href="#i-logo"/></svg></span>
      <span class="word">Blast Radius</span>
    </a>
    <nav class="hnav">${nav}</nav>
    <div class="hcta">
      <div class="hstat" id="livestats" aria-live="polite">
        <span class="pulse" id="pulse" aria-hidden="true"></span>
        <span id="statline">connecting…</span>
      </div>
      <span id="sessionslot"></span>
    </div>
  </div>
</header>`;
  }

  function paintHeader() {
    const slot = q('#sessionslot');
    if (!slot) return;
    if (session.account) {
      slot.innerHTML =
        `<a class="btn" href="/dashboard"><svg viewBox="0 0 24 24"><use href="#i-user"/></svg> Dashboard</a>
         <a class="btn btn-primary" href="/check">Run a check</a>`;
    } else {
      slot.innerHTML =
        `<a class="btn" href="/signin">Sign in</a>
         <a class="btn btn-primary" href="/check">Run a check</a>`;
    }
  }

  function footerHTML() {
    return `
<footer class="ftr">
  <div class="wrap">
    <a class="logo" href="/">
      <span class="mark"><svg viewBox="0 0 32 32"><use href="#i-logo"/></svg></span>
      <span class="word">Blast Radius</span>
    </a>
    <nav class="fnav">
      <a href="/">Home</a>
      <a href="/check">Run a check</a>
      <a href="/developers">API</a>
      <a href="/dashboard">Dashboard</a>
      <a href="/#analytics">Analytics</a>
      <a href="/#pricing">Pricing</a>
      <a href="/api/docs">OpenAPI</a>
    </nav>
    <div class="fsoc">
      <a href="${REPO}" target="_blank" rel="noopener" aria-label="GitHub" title="GitHub">
        <svg viewBox="0 0 24 24"><use href="#i-github"/></svg></a>
      <a href="https://github.com/hydra-db/hydradb" target="_blank" rel="noopener" aria-label="HydraDB" title="Built on HydraDB">
        <svg viewBox="0 0 24 24"><use href="#i-cube"/></svg></a>
      <a href="/api/health" aria-label="Health" title="Live health endpoint">
        <svg viewBox="0 0 24 24"><use href="#i-pulse"/></svg></a>
    </div>
    <p class="fnote">
      © 2025 Blast Radius · MIT licensed · built on
      <a href="https://github.com/hydra-db/hydradb">HydraDB</a> ·
      package data from the <a href="https://registry.npmjs.org">npm registry</a> ·
      advisories from <a href="https://osv.dev">osv.dev</a> ·
      <a href="${REPO}">github.com/Rohit-ATS/blast-radius</a>
    </p>
  </div>
</footer>`;
  }

  /* ------------------------------------------------------- header counter */

  function liveCounter() {
    const line = q('#statline'), dot = q('#pulse');
    if (!line) return;
    const paint = async () => {
      try {
        const s = await api('/api/stats');
        line.innerHTML = `${fmt(s.packages)} <span class="dim">packages</span> · ${fmt(s.edges)} <span class="dim">edges</span>`;
        dot.className = 'pulse live';
      } catch {
        line.textContent = 'graph unreachable';
        dot.className = 'pulse dead';
      }
    };
    paint();
    setInterval(paint, 20000);
  }

  function stickyHeader() {
    const bar = q('.hdr');
    if (!bar) return;
    const paint = () => bar.classList.toggle('stuck', window.scrollY > 12);
    paint();
    addEventListener('scroll', paint, { passive: true });
  }

  /* --------------------------------------------------------------- toasts */

  function toast(message, kind = 'ok') {
    let host = q('.toasts');
    if (!host) {
      host = document.createElement('div');
      host.className = 'toasts';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `<svg viewBox="0 0 24 24"><use href="#i-${kind === 'bad' ? 'alert' : 'check'}"/></svg><span>${esc(message)}</span>`;
    host.appendChild(el);
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, 3200);
  }

  /* --------------------------------------------------------------- copying */

  async function copy(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // clipboard is blocked in some contexts; fall back to a selection
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch { /* nothing else to try */ }
      ta.remove();
    }
    if (btn) {
      const was = btn.innerHTML;
      btn.classList.add('done');
      btn.innerHTML = `<svg viewBox="0 0 24 24"><use href="#i-check"/></svg> Copied`;
      setTimeout(() => { btn.innerHTML = was; btn.classList.remove('done'); }, 1500);
    }
    toast('Copied to clipboard');
  }

  function wireCopy(root = document) {
    qq('[data-copy]', root).forEach(btn => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', () => {
        const src = btn.dataset.copy;
        const text = src.startsWith('#')
          ? (q(src)?.textContent ?? '')
          : src;
        copy(text, btn);
      });
    });
  }

  /* ----------------------------------------------------------------- boot */

  function mount({ active = '' } = {}) {
    document.body.insertAdjacentHTML('afterbegin', SPRITE + headerHTML(active));
    document.body.insertAdjacentHTML('beforeend', footerHTML());
    paintHeader();
    stickyHeader();
    liveCounter();
    wireCopy();
    return loadSession();
  }

  async function requireSession(redirect = '/signin') {
    const s = session.loaded ? session : await loadSession();
    if (!s.account) {
      location.href = `${redirect}?next=${encodeURIComponent(location.pathname)}`;
      return null;
    }
    return s.account;
  }

  return { q, qq, fmt, esc, api, ago, when, mount, toast, copy, wireCopy,
           loadSession, requireSession, paintHeader, liveCounter, stickyHeader, REPO,
           get session() { return session; } };
})();
