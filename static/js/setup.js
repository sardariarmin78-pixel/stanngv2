/* ===========================================================
   StanNG — first-run admin setup controller.
   Extracted from an inline <script> so the panel can ship a
   strict script-src CSP.
   =========================================================== */
(() => {
  STANNG.initLangThemeToggles();
  STANNG.initPasswordToggles();

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
      STANNG.shake(form);
      STANNG.playSfx('error');
      return;
    }
    STANNG.setLoading(btn, true);
    try {
      await STANNG.api('/api/setup', { method: 'POST', body: { username, password } });
      STANNG.toast(STANNG.t('setup_done'), 'success');
      setTimeout(() => { window.location.href = '/dashboard'; }, 700);
    } catch (err) {
      STANNG.setLoading(btn, false);
      STANNG.shake(form);
      let msg = err.detail || 'error';
      if (msg === 'invalid-username') msg = STANNG.t('setup_username_hint');
      else if (msg === 'weak-password') msg = STANNG.t('setup_password_hint');
      STANNG.toast(msg, 'error');
    }
  });
})();
