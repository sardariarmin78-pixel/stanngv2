/* ===========================================================
   StanNG — login page controller.
   Extracted from an inline <script> so the panel can ship a
   strict script-src CSP.
   =========================================================== */
(() => {
  STANNG.initLangThemeToggles();
  STANNG.initPasswordToggles();

  const form = document.getElementById('loginForm');
  const btn = document.getElementById('loginBtn');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    STANNG.setLoading(btn, true);
    try {
      await STANNG.api('/api/login', { method: 'POST', body: { username, password } });
      STANNG.toast(STANNG.t('login_success'), 'success');
      setTimeout(() => { window.location.href = '/dashboard'; }, 500);
    } catch (err) {
      STANNG.setLoading(btn, false);
      STANNG.shake(form);
      const detail = err.detail || '';
      let msg = STANNG.t('login_failed');
      if (detail.startsWith('locked:')) {
        // Tell the admin how long the lockout still has to run.
        const secs = parseInt(detail.split(':')[1], 10);
        msg = STANNG.t('login_locked');
        if (!isNaN(secs)) msg += ` (${Math.ceil(secs / 60)}m)`;
      }
      STANNG.toast(msg, 'error');
    }
  });
})();
