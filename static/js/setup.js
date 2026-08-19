/* ===========================================================
   Peyk — first-run admin setup controller.
   Extracted from an inline <script> so the panel can ship a
   strict script-src CSP.
   =========================================================== */
(() => {
  PEYK.initLangThemeToggles();
  PEYK.initPasswordToggles();

  fetch('/api/setup-status')
    .then(r => r.json())
    .then(d => { if (!d.needs_setup) window.location.href = '/login'; })
    .catch(() => {});

  const form = document.getElementById('setupForm');
  const btn = document.getElementById('setupBtn');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    const password2 = document.getElementById('password2').value;
    const mismatchEl = document.getElementById('mismatchError');
    mismatchEl.classList.remove('show');

    if (password !== password2) {
      mismatchEl.classList.add('show');
      PEYK.shake(form);
      PEYK.playSfx('error');
      return;
    }
    PEYK.setLoading(btn, true);
    try {
      await PEYK.api('/api/setup', { method: 'POST', body: { username, password } });
      PEYK.toast(PEYK.t('setup_done'), 'success');
      setTimeout(() => { window.location.href = '/dashboard'; }, 700);
    } catch (err) {
      PEYK.setLoading(btn, false);
      PEYK.shake(form);
      let msg = err.detail || 'error';
      if (msg === 'invalid-username') msg = PEYK.t('setup_username_hint');
      else if (msg === 'weak-password') msg = PEYK.t('setup_password_hint');
      PEYK.toast(msg, 'error');
    }
  });
})();
