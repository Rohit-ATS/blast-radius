/* The dedicated check page.
 *
 * app.js already owns the query form, the results grid, the map, the drop
 * zones, the explorer and the ticker — this file only mounts the shared shell
 * and adds what belongs to this page: the "monitor this package" action and a
 * deep-linkable `?pkg=&ver=` so a check can be shared as a URL.
 */

'use strict';

(async () => {
  const { q, api, toast } = window.BR;
  await window.BR.mount({ active: '/check' });

  const pkg = q('#pkg'), ver = q('#ver');

  /* ------------------------------------------------------------ deep link */
  /* A check is a thing you send someone during an incident, so it has to
     survive being pasted into Slack. */

  const params = new URLSearchParams(location.search);
  const wanted = { pkg: params.get('pkg') || '', ver: params.get('ver') || '' };
  if (wanted.pkg) {
    pkg.value = wanted.pkg;
    ver.value = wanted.ver;
    // app.js binds submit on this form; firing it runs the whole console.
    setTimeout(() => q('#queryform').requestSubmit(), 350);
  }

  // Keep the address bar in step with whatever was last checked.
  q('#queryform').addEventListener('submit', () => {
    const url = new URL(location.href);
    url.searchParams.set('pkg', pkg.value.trim());
    if (ver.value.trim()) url.searchParams.set('ver', ver.value.trim());
    else url.searchParams.delete('ver');
    history.replaceState(null, '', url);
  });

  /* -------------------------------------------------------- monitor action */

  const btn = q('#watchbtn');
  const label = btn.querySelector('span');

  btn.addEventListener('click', async () => {
    const name = pkg.value.trim();
    if (!name) { toast('Run a check first', 'bad'); return; }

    if (!window.BR.session.account) {
      location.href = `/signin?next=${encodeURIComponent(location.pathname + location.search)}`;
      return;
    }

    btn.disabled = true;
    const was = label.textContent;
    label.textContent = 'Adding…';
    try {
      await api('/api/monitors', {
        method: 'POST',
        body: JSON.stringify({ package: name, label: ver.value.trim() }),
      });
      label.textContent = 'Monitoring';
      btn.classList.remove('btn-primary');
      toast(`${name} added to the 24-hour watch`);
      setTimeout(() => {
        label.textContent = was;
        btn.classList.add('btn-primary');
        btn.disabled = false;
      }, 2600);
    } catch (err) {
      toast(err.message, 'bad');
      label.textContent = was;
      btn.disabled = false;
    }
  });

  // The call to action only makes sense once there is something to monitor.
  const results = q('#results');
  new MutationObserver(() => {
    q('#watchcta').hidden = results.hasAttribute('hidden');
  }).observe(results, { attributes: true, attributeFilter: ['hidden'] });
  q('#watchcta').hidden = results.hasAttribute('hidden');
})();
