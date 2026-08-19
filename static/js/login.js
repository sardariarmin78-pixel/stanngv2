/* ===========================================================
   Login controller.

   Two-step: credentials first, then a second factor only if the
   server says one is required. The 2FA field is never shown up
   front, so a panel without 2FA looks unchanged.
   =========================================================== */
(() => {
  STANNG.initLangThemeToggles();
  STANNG.initPasswordToggles();

  const form = document.getElementById('loginForm');
  const btn = document.getElementById('loginBtn');
  const step1 = document.getElementById('loginStep1');
  const step2 = document.getElementById('loginStep2');
  const codeInput = document.getElementById('twofaCode');
  if (!form) return;

  let awaitingCode = false;

  function askForCode() {
    awaitingCode = true;
    step1.style.display = 'none';
    step2.style.display = '';
    btn.querySelector('span').textContent = STANNG.t('twofa_verify');
    codeInput.value = '';
    codeInput.focus();
  }

  function backToCredentials() {
    awaitingCode = false;
    step1.style.display = '';
    step2.style.display = 'none';
    btn.querySelector('span').textContent = STANNG.t('login_btn');
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    const body = { username, password };
    if (awaitingCode) body.code = codeInput.value.trim();

    STANNG.setLoading(btn, true);
    try {
      const res = await STANNG.api('/api/login', { method: 'POST', body });
      if (res && res.twofa_required) {
        // Password accepted; the account also has TOTP enabled.
        STANNG.setLoading(btn, false);
        askForCode();
        return;
      }
      STANNG.toast(STANNG.t('login_success'), 'success');
      if (res && res.recovery_remaining === 0) {
        STANNG.toast(STANNG.t('twofa_no_recovery_left'), 'info', 7000);
      }
      setTimeout(() => { window.location.href = '/dashboard'; }, 450);
    } catch (err) {
      STANNG.setLoading(btn, false);
      STANNG.shake(form);
      const detail = err.detail || '';
      let msg = awaitingCode ? STANNG.t('twofa_invalid') : STANNG.t('login_failed');
      if (detail.startsWith('locked:')) {
        const secs = parseInt(detail.split(':')[1], 10);
        msg = STANNG.t('login_locked');
        if (!isNaN(secs)) msg += ` (${Math.ceil(secs / 60)}m)`;
        backToCredentials();
      }
      STANNG.toast(msg, 'error');
      if (awaitingCode) codeInput.select();
    }
  });
})();
