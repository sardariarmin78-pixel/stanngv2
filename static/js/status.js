/* ===========================================================
   StanNG — public per-user status page.
   Extracted from an inline <script> so the panel can ship a
   strict script-src CSP. The uid arrives via a data attribute
   rather than being templated into JS source.
   =========================================================== */
(() => {
  const root = document.getElementById('statusContent');
  if (!root) return;
  const uid = root.dataset.uid || '';
  let lastData = null;

  const esc = STANNG.escapeHtml;

  function reasonKey(reason) {
    switch (reason) {
      case 'expired': return 'expired';
      case 'quota': return 'status_quota_over';
      case 'requests': return 'status_requests_over';
      default: return 'status_disabled';
    }
  }

  function render() {
    if (!lastData) return;
    if (lastData.error) {
      root.innerHTML = `<div class="text-center muted">${esc(STANNG.t('status_not_found'))}</div>`;
      return;
    }
    const d = lastData;
    const pct = d.quota_bytes > 0 ? Math.min(100, (d.used_bytes / d.quota_bytes) * 100) : 0;
    const quotaTxt = d.quota_gb > 0
      ? `${d.used_gb} GB / ${d.quota_gb} GB`
      : `${d.used_gb} GB / ${STANNG.t('unlimited')}`;
    // d.name is admin-supplied; escape it rather than interpolating raw.
    const statusPill = d.enabled
      ? `<span class="pill pill-on"><span class="pill-dot"></span>${esc(STANNG.t('active'))}</span>`
      : `<span class="pill pill-off"><span class="pill-dot"></span>${esc(STANNG.t(reasonKey(d.reason)))}</span>`;
    const expiry = d.expire_at
      ? new Date(d.expire_at * 1000).toLocaleDateString(STANNG.getLang() === 'fa' ? 'fa-IR' : 'en-US')
      : STANNG.t('unlimited');

    root.innerHTML = `
      <div class="text-center mb-16" style="font-size:17px; font-weight:700;">${esc(d.name)}</div>
      <div class="text-center mb-16">${statusPill}</div>
      <div class="field">
        <label>${esc(STANNG.t('status_usage'))}</label>
        <div class="bar progress-gold" style="height:10px;"><span style="width:${pct}%"></span></div>
        <div class="small muted mt-8 text-center">${esc(quotaTxt)}</div>
      </div>
      <div class="grid" style="grid-template-columns:1fr 1fr; gap:12px; margin-top:16px;">
        <div class="win stat-card" style="padding:12px;">
          <div class="label small">${esc(STANNG.t('status_devices'))}</div>
          <div class="value" style="font-size:18px;">${esc(d.active_connections)}${d.max_connections ? ' / ' + esc(d.max_connections) : ''}</div>
        </div>
        <div class="win stat-card" style="padding:12px;">
          <div class="label small">${esc(STANNG.t('status_expiry'))}</div>
          <div class="value" style="font-size:14px;">${esc(expiry)}</div>
        </div>
      </div>
    `;
  }

  STANNG.initLangThemeToggles(render);

  async function load() {
    try {
      const res = await fetch(`/api/status/${encodeURIComponent(uid)}`, { cache: 'no-store' });
      if (!res.ok) { lastData = { error: true }; render(); return; }
      lastData = await res.json();
      render();
    } catch (e) {
      lastData = { error: true };
      render();
    }
  }

  load();
  // Keep the page live while it is on screen; pause when backgrounded so a
  // forgotten tab doesn't poll the panel forever.
  setInterval(() => { if (!document.hidden) load(); }, 30000);
})();
