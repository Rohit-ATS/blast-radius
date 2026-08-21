/* Sign in / create account.
 *
 * One form, two modes. The server owns credential handling; this only collects
 * and reports. On success it goes wherever `?next=` pointed, defaulting to the
 * dashboard — which is where a new account's first API key is waiting.
 */

'use strict';

(async () => {
  const { q, api, toast } = window.BR;
  await window.BR.mount({ active: '' });

  // Already signed in? There is nothing to do on this page.
  if (window.BR.session.account) {
    location.replace(next());
    return;
  }

  let mode = new URLSearchParams(location.search).get('mode') === 'signup'
    ? 'signup' : 'signin';

  const els = {
    title: q('#authtitle'), lede: q('#authlede'), swap: q('#authswap'),
    name: q('#namefield'), submit: q('#submit'), err: q('#autherr'),
    pw: q('#password'), hint: q('#pwhint'), meta: q('#authmeta'),
  };

  function next() {
    const to = new URLSearchParams(location.search).get('next');
    return to && to.startsWith('/') ? to : '/dashboard';
  }

  function paint() {
    const up = mode === 'signup';
    els.title.textContent = up ? 'Create your account' : 'Sign in';
    els.lede.textContent = up
      ? 'One account, unlimited API keys, unlimited calls. No card, no quota, no upsell.'
      : 'Your account holds your API keys, your monitors and the alerts they raise.';
    els.name.hidden = !up;
    els.submit.textContent = up ? 'Create account' : 'Sign in';
    els.pw.setAttribute('autocomplete', up ? 'new-password' : 'current-password');
    els.hint.textContent = up
      ? 'At least 8 characters. Stored as PBKDF2-HMAC-SHA256 with a per-account salt.'
      : 'Stored as PBKDF2-HMAC-SHA256 with a per-account salt.';
    els.swap.innerHTML = up
      ? `Already have an account? <button type="button" id="swap">Sign in</button>`
      : `New here? <button type="button" id="swap">Create an account</button>`;
    q('#swap').addEventListener('click', () => {
      mode = up ? 'signin' : 'signup';
      els.err.hidden = true;
      paint();
    });
    els.err.hidden = true;
  }

  paint();

  // Say which credential store is actually in play, rather than implying one.
  api('/api/auth/me').then(r => {
    els.meta.innerHTML = r.provider === 'supabase'
      ? 'Authentication is handled by Supabase. Your keys, monitors and alerts live in this instance.'
      : 'Credentials are held by this instance (PBKDF2). Set <code>SUPABASE_URL</code> and '
        + '<code>SUPABASE_ANON_KEY</code> to move authentication to Supabase — everything else is unchanged.';
  }).catch(() => {});

  /* Password reset only exists when an email provider does. Rather than
     showing a control that quietly does nothing, this asks the server what is
     configured and hides itself when the answer is "nothing". */
  api('/api/auth/me').then(r => {
    q('#forgotrow').hidden = r.provider !== 'supabase';
  }).catch(() => { q('#forgotrow').hidden = true; });

  q('#forgot').addEventListener('click', async () => {
    const email = q('#email').value.trim();
    if (!email) { els.err.textContent = 'Enter your email address first.'; els.err.hidden = false; return; }
    try {
      await api('/api/auth/reset', { method: 'POST', body: JSON.stringify({ email }) });
      toast('Check your inbox for the reset link');
      els.err.hidden = true;
    } catch (err) {
      els.err.textContent = err.message;
      els.err.hidden = false;
    }
  });

  q('#authform').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = els.submit;
    const was = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = `<span class="spin"></span>`;
    els.err.hidden = true;

    try {
      const payload = {
        email: q('#email').value.trim(),
        password: q('#password').value,
        name: q('#name').value.trim(),
      };
      const out = await api(`/api/auth/${mode === 'signup' ? 'signup' : 'login'}`,
                            { method: 'POST', body: JSON.stringify(payload) });

      toast(mode === 'signup' ? 'Account created' : 'Signed in');
      location.href = next();
    } catch (err) {
      // 202/confirm_email means the account was created and Supabase has sent
      // a link — a success the user must act on, not an error.
      if (err.code === 'confirm_email') {
        els.err.style.cssText = 'background:var(--blue-bg);border-color:var(--blue-line);color:#23407f';
        mode = 'signin';
        paint();
      } else {
        els.err.style.cssText = '';
      }
      els.err.textContent = err.message;
      els.err.hidden = false;
      btn.disabled = false;
      btn.textContent = was;
    }
  });
})();
