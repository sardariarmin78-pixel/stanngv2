/* ===========================================================
   StanNG — dashboard controller (v1.5.0)
   Plain-text subscription + Subscription-Userinfo header.
   Adds: renew, enable/disable, backup/restore, active IPs,
   branding and relay-safety settings.
   =========================================================== */
(() => {
  let currentInbounds = [];
  let lastHourly = [];
  let inboundFilter = '';

  const $ = (id) => document.getElementById(id);
  const esc = STANNG.escapeHtml;

  // ---------------- guard: must be logged in ----------------
  STANNG.api('/api/me').then(me => {
    if (!me.logged_in) { window.location.href = '/login'; return; }
    $('appVersion').textContent = me.app_version || '';
    // The panel version is whatever code is running; settings.app_version was
    // removed from storage in 1.4.1 and reading it here always fell through.
    $('otaCurrent').textContent = me.app_version || '-';
    const s = me.settings || {};
    $('settingPublicDomain').value = s.public_domain || '';
    $('settingOtaRepo').value = s.ota_repo || '';
    $('settingKeepAlive').checked = s.keep_alive !== false;
    $('settingFingerprint').value = s.default_fingerprint || 'chrome';
    $('settingAlpn').value = s.default_alpn || 'http/1.1';
    $('settingSniOverride').value = s.sni_override || '';
    $('settingFragmentEnabled').checked = s.fragment_enabled !== false;
    $('settingFragmentPackets').value = s.fragment_packets || 'tlshello';
    $('settingFragmentLength').value = s.fragment_length || '10-30';
    $('settingFragmentInterval').value = s.fragment_interval || '10-20';
    $('settingPanelName').value = s.panel_name || '';
    $('settingTelegram').value = s.telegram_contact || '';
    $('settingAllowPrivate').checked = !!s.allow_private_destinations;
    $('settingIdleTimeout').value = s.idle_timeout_seconds != null ? s.idle_timeout_seconds : 600;
    applyFragmentFieldState();
  }).catch(() => { window.location.href = '/login'; });

  $('settingSound').checked = STANNG.isSoundEnabled();

  // ---------------- nav / view switching ----------------
  const views = document.querySelectorAll('.view');
  const navItems = document.querySelectorAll('.nav-item[data-view]');
  const viewTitle = $('viewTitle');
  const titleKeys = {
    dashboard: 'nav_dashboard', inbounds: 'nav_inbounds', traffic: 'nav_traffic',
    security: 'nav_security', settings: 'nav_settings',
  };

  function showView(name) {
    views.forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
    navItems.forEach(n => n.classList.toggle('active', n.dataset.view === name));
    viewTitle.setAttribute('data-i18n', titleKeys[name]);
    viewTitle.textContent = STANNG.t(titleKeys[name]);
    if (name === 'inbounds' || name === 'traffic') loadInbounds();
    closeSidebarMobile();
    STANNG.playSfx('open', 0.3);
  }
  navItems.forEach(item => item.addEventListener('click', () => showView(item.dataset.view)));

  // ---------------- mobile sidebar ----------------
  const sidebar = $('sidebar');
  const backdrop = $('sidebarBackdrop');
  $('menuToggle').addEventListener('click', () => {
    const opening = !sidebar.classList.contains('open');
    sidebar.classList.toggle('open', opening);
    backdrop.classList.toggle('open', opening);
    document.body.classList.toggle('sidebar-locked', opening);
  });
  backdrop.addEventListener('click', closeSidebarMobile);
  function closeSidebarMobile() {
    sidebar.classList.remove('open');
    backdrop.classList.remove('open');
    document.body.classList.remove('sidebar-locked');
  }

  // ---------------- lang / theme / sound ----------------
  document.querySelectorAll('.lang-toggle button').forEach(btn => {
    btn.addEventListener('click', () => {
      STANNG.setLang(btn.dataset.lang);
      viewTitle.textContent = STANNG.t(viewTitle.getAttribute('data-i18n'));
      renderInboundsTable();
      renderTrafficTable();
      STANNG.playSfx('toggle', 0.3);
    });
  });
  $('themeToggle').addEventListener('click', () => {
    STANNG.setTheme(STANNG.getTheme() === 'dark' ? 'light' : 'dark');
    STANNG.playSfx('toggle', 0.3);
    renderTrafficChart($('trafficChart'), lastHourly);
  });
  $('soundToggle').addEventListener('click', () => {
    const next = !STANNG.isSoundEnabled();
    STANNG.setSoundEnabled(next);
    $('settingSound').checked = next;
    if (next) STANNG.playSfx('click');
  });

  // ---------------- logout ----------------
  $('logoutBtn').addEventListener('click', async () => {
    try { await STANNG.api('/api/logout', { method: 'POST' }); } catch (e) { /* leave anyway */ }
    window.location.href = '/login';
  });

  // ---------------- modal helpers ----------------
  function openModal(id) { $(id).classList.add('open'); STANNG.playSfx('open', 0.4); }
  function closeModal(id) { $(id).classList.remove('open'); STANNG.playSfx('close', 0.4); }
  document.querySelectorAll('[data-close-modal]').forEach(btn => {
    btn.addEventListener('click', () => closeModal(btn.dataset.closeModal));
  });
  document.querySelectorAll('.modal-overlay').forEach(ov => {
    ov.addEventListener('click', (e) => { if (e.target === ov) closeModal(ov.id); });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const open = document.querySelector('.modal-overlay.open');
    if (open) closeModal(open.id);
  });

  // ---------------- dashboard stats polling ----------------
  let statsTimer = null;
  async function refreshStats() {
    try {
      const s = await STANNG.api('/stats');
      $('statCpu').textContent = s.cpu_percent.toFixed(1) + '%';
      $('barCpu').style.width = Math.min(100, s.cpu_percent) + '%';
      $('statMem').textContent = s.mem_percent.toFixed(1) + '%';
      $('barMem').style.width = Math.min(100, s.mem_percent) + '%';
      $('statUptime').textContent = STANNG.fmtDuration(s.uptime_seconds);
      const loc = s.location || {};
      $('statLocation').textContent = `${loc.flag || ''} ${loc.city || '?'}`;
      $('statTotalTraffic').textContent = STANNG.fmtBytes((s.total_up || 0) + (s.total_down || 0));
      $('statUp').textContent = STANNG.fmtBytes(s.total_up || 0);
      $('statDown').textContent = STANNG.fmtBytes(s.total_down || 0);
      $('statActiveConn').textContent = s.active_connections || 0;
      $('statInboundCount').textContent = s.inbounds_count || 0;
      $('navInboundCount').textContent = s.inbounds_count || 0;
      $('trafficUp').textContent = STANNG.fmtBytes(s.total_up || 0);
      $('trafficDown').textContent = STANNG.fmtBytes(s.total_down || 0);
      lastHourly = s.hourly || [];
      renderTrafficChart($('trafficChart'), lastHourly);
    } catch (e) {
      if (e.status === 401) { window.location.href = '/login'; }
    }
  }

  function startStatsPolling() {
    if (statsTimer) return;
    refreshStats();
    statsTimer = setInterval(refreshStats, 8000);
  }
  function stopStatsPolling() {
    if (!statsTimer) return;
    clearInterval(statsTimer);
    statsTimer = null;
  }
  // A backgrounded tab used to keep polling /stats forever.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopStatsPolling(); else startStatsPolling();
  });
  startStatsPolling();

  let resizeRaf = null;
  window.addEventListener('resize', () => {
    if (resizeRaf) cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(() => renderTrafficChart($('trafficChart'), lastHourly));
  });

  // ---------------- OTA ----------------
  let otaLatestKnown = null;

  $('otaCheckBtn').addEventListener('click', async () => {
    const btn = $('otaCheckBtn');
    const updateBtn = $('otaUpdateBtn');
    const hint = $('otaUpdateHint');
    const el = $('otaResult');
    STANNG.setLoading(btn, true);
    try {
      const r = await STANNG.api('/api/ota/check');
      if (r.update_available) {
        // r.latest and r.url come from the GitHub API, so escape before insert.
        el.innerHTML = `<span style="color:var(--gold-300)">${esc(STANNG.t('dash_ota_available'))} <b>${esc(r.latest)}</b></span> — <a href="${esc(r.url)}" target="_blank" rel="noopener" style="color:var(--azure); text-decoration:underline;">GitHub</a>`;
        STANNG.toast(STANNG.t('dash_ota_available') + ' ' + r.latest, 'info');
        otaLatestKnown = r.latest;
        updateBtn.style.display = '';
        hint.style.display = '';
      } else {
        el.innerHTML = `<span style="color:var(--emerald)">${esc(STANNG.t('dash_ota_uptodate'))}</span>`;
        STANNG.toast(STANNG.t('dash_ota_uptodate'), 'success');
        otaLatestKnown = null;
        updateBtn.style.display = 'none';
        hint.style.display = 'none';
      }
    } catch (e) {
      let msg = e.detail || 'error';
      if (msg === 'no-repo-configured') msg = STANNG.t('settings_ota_repo');
      STANNG.toast(msg, 'error');
    } finally {
      STANNG.setLoading(btn, false);
    }
  });

  $('otaUpdateBtn').addEventListener('click', async () => {
    const msg = STANNG.t('dash_ota_update_confirm').replace('{version}', otaLatestKnown || '');
    if (!confirm(msg)) return;

    const updateBtn = $('otaUpdateBtn');
    const checkBtn = $('otaCheckBtn');
    const el = $('otaResult');
    STANNG.setLoading(updateBtn, true);
    checkBtn.disabled = true;

    try {
      const r = await STANNG.api('/api/ota/update', { method: 'POST' });
      if (r.ok) {
        el.innerHTML = `<span style="color:var(--gold-300)">${esc(STANNG.t('dash_ota_updating'))}</span>`;
        STANNG.toast(STANNG.t('dash_ota_updating'), 'info', 8000);
        waitForRestartThenReload();
      } else {
        el.innerHTML = `<span style="color:var(--emerald)">${esc(STANNG.t('dash_ota_uptodate'))}</span>`;
        STANNG.toast(STANNG.t('dash_ota_uptodate'), 'success');
        STANNG.setLoading(updateBtn, false);
        checkBtn.disabled = false;
      }
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
      STANNG.setLoading(updateBtn, false);
      checkBtn.disabled = false;
    }
  });

  function waitForRestartThenReload() {
    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      try {
        const res = await fetch('/health', { cache: 'no-store' });
        if (res.ok) {
          clearInterval(poll);
          STANNG.toast(STANNG.t('dash_ota_done'), 'success', 3000);
          setTimeout(() => window.location.reload(), 1200);
          return;
        }
      } catch (e) { /* still restarting */ }
      if (attempts > 60) {
        clearInterval(poll);
        STANNG.toast(STANNG.t('dash_ota_timeout'), 'error', 8000);
      }
    }, 3000);
  }

  $('quickAddBtn').addEventListener('click', () => { showView('inbounds'); openInboundModal(); });

  // ---------------- inbounds ----------------
  async function loadInbounds() {
    try {
      const r = await STANNG.api('/api/inbounds');
      currentInbounds = r.inbounds || [];
      renderInboundsTable();
      renderTrafficTable();
      $('navInboundCount').textContent = currentInbounds.length;
    } catch (e) {
      if (e.status === 401) { window.location.href = '/login'; return; }
      STANNG.toast(e.detail || 'error', 'error');
    }
  }

  function statusLabel(st) {
    if (st.live_enabled) return { key: 'active', cls: 'pill-on' };
    if (st.expired) return { key: 'expired', cls: 'pill-off' };
    // Quota and request caps used to be lumped in as a generic "inactive",
    // which made it impossible to tell why a user stopped working.
    if (st.quota_exceeded) return { key: 'status_quota_over', cls: 'pill-off' };
    if (st.request_exceeded) return { key: 'status_requests_over', cls: 'pill-off' };
    if (st.disabled) return { key: 'inb_disabled_manual', cls: 'pill-off' };
    return { key: 'inactive', cls: 'pill-off' };
  }

  function renderInboundsTable() {
    const tbody = $('inboundsTableBody');
    const empty = $('inboundsEmpty');
    const needle = inboundFilter.toLowerCase();
    const rows = currentInbounds.filter(ib =>
      !needle
      || (ib.name || '').toLowerCase().includes(needle)
      || (ib.note || '').toLowerCase().includes(needle));
    tbody.innerHTML = '';
    empty.style.display = rows.length ? 'none' : 'block';

    const frag = document.createDocumentFragment();
    rows.forEach(ib => {
      const st = ib.status;
      const tr = document.createElement('tr');
      const label = statusLabel(st);
      const statusPill = `<span class="pill ${label.cls}"><span class="pill-dot"></span>${esc(STANNG.t(label.key))}</span>`;
      const quotaTxt = ib.quota_gb > 0
        ? `${STANNG.fmtBytes(st.used)} ${STANNG.t('inb_used_of')} ${ib.quota_gb} GB`
        : `${STANNG.fmtBytes(st.used)} / ${STANNG.t('unlimited')}`;
      const pct = st.quota_bytes > 0
        ? Math.min(100, (st.used / st.quota_bytes) * 100)
        : (st.used > 0 ? 8 : 0);
      const expireTxt = ib.expire_at
        ? `${st.days_left} ${STANNG.t('inb_days_left')}`
        : STANNG.t('inb_no_expire');
      tr.innerHTML = `
        <td data-label="${esc(STANNG.t('inb_name'))}"><b>${esc(ib.name)}</b><div class="small muted">${esc(ib.note || '')}</div></td>
        <td data-label="${esc(STANNG.t('inb_status'))}">${statusPill}</td>
        <td data-label="${esc(STANNG.t('inb_usage'))}" style="min-width:160px;">
          <div class="small">${esc(quotaTxt)}</div>
          <div class="bar progress-gold" style="margin-top:4px;"><span style="width:${pct}%"></span></div>
        </td>
        <td data-label="${esc(STANNG.t('inb_expire'))}">${esc(expireTxt)}</td>
        <td data-label="${esc(STANNG.t('inb_max_conn'))}">${esc(st.active_connections)}${ib.max_connections ? ' / ' + esc(ib.max_connections) : ''} <span class="small muted">${esc(STANNG.t('inb_active_devices'))}</span></td>
        <td data-label="${esc(STANNG.t('inb_actions'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-action="links" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('inb_links'))}"><svg width="15" height="15"><use href="#icon-qr"/></svg></button>
            <button class="icon-btn btn-sm" data-action="edit" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('edit'))}"><svg width="15" height="15"><use href="#icon-edit"/></svg></button>
            <button class="icon-btn btn-sm" data-action="renew" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('inb_renew'))}"><svg width="15" height="15"><use href="#icon-clock"/></svg></button>
            <button class="icon-btn btn-sm" data-action="reset" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('inb_reset_usage'))}"><svg width="15" height="15"><use href="#icon-refresh"/></svg></button>
            <button class="icon-btn btn-sm" data-action="regen" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('inb_regenerate'))}"><svg width="15" height="15"><use href="#icon-key"/></svg></button>
            <button class="icon-btn btn-sm" data-action="delete" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('delete'))}" style="color:var(--crimson)"><svg width="15" height="15"><use href="#icon-trash"/></svg></button>
          </div>
        </td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }

  // One delegated listener instead of re-binding six per row on every render.
  $('inboundsTableBody').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (btn) handleInboundAction(btn.dataset.action, btn.dataset.uid);
  });

  function renderTrafficTable() {
    const tbody = $('trafficTableBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    const frag = document.createDocumentFragment();
    currentInbounds.forEach(ib => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td data-label="${esc(STANNG.t('inb_name'))}"><b>${esc(ib.name)}</b></td>
        <td data-label="${esc(STANNG.t('dash_upload'))}">${esc(STANNG.fmtBytes(ib.used_up || 0))}</td>
        <td data-label="${esc(STANNG.t('dash_download'))}">${esc(STANNG.fmtBytes(ib.used_down || 0))}</td>
        <td data-label="${esc(STANNG.t('inb_usage'))}">${esc(STANNG.fmtBytes((ib.used_up || 0) + (ib.used_down || 0)))}</td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }

  $('inboundSearch').addEventListener('input', (e) => {
    inboundFilter = e.target.value;
    renderInboundsTable();
  });

  function openInboundModal(ib = null) {
    $('inboundModalTitle').textContent = ib ? STANNG.t('edit') : STANNG.t('inb_add');
    $('inboundUid').value = ib ? ib.uid : '';
    $('fName').value = ib ? ib.name : '';
    $('fQuota').value = ib ? (ib.quota_gb || '') : '';
    $('fExpire').value = ib ? (ib.expire_days || '') : '';
    $('fMaxConn').value = ib ? (ib.max_connections || '') : '';
    $('fMaxReq').value = ib ? (ib.max_requests || '') : '';
    $('fFingerprint').value = ib ? (ib.fp || 'chrome') : 'chrome';
    $('fStrictIp').checked = ib ? !!ib.strict_single_ip : false;
    $('fEnabled').checked = ib ? ib.enabled !== false : true;
    $('fNote').value = ib ? (ib.note || '') : '';
    openModal('inboundModal');
  }

  $('addInboundBtn').addEventListener('click', () => openInboundModal());

  $('inboundSaveBtn').addEventListener('click', async () => {
    const uid = $('inboundUid').value;
    const num = (id) => {
      const raw = $(id).value.trim();
      return raw === '' ? 0 : Number(raw);
    };
    const payload = {
      name: $('fName').value.trim() || 'User',
      quota_gb: num('fQuota'),
      expire_days: num('fExpire'),
      max_connections: num('fMaxConn'),
      max_requests: num('fMaxReq'),
      fp: $('fFingerprint').value,
      strict_single_ip: $('fStrictIp').checked,
      enabled: $('fEnabled').checked,
      note: $('fNote').value.trim(),
    };
    const bad = Object.entries(payload).find(([, v]) => typeof v === 'number' && !isFinite(v));
    if (bad) { STANNG.toast(STANNG.t('inb_invalid_number'), 'error'); return; }

    const btn = $('inboundSaveBtn');
    STANNG.setLoading(btn, true);
    try {
      if (uid) {
        await STANNG.api(`/api/inbounds/${uid}`, { method: 'PATCH', body: payload });
        STANNG.toast(STANNG.t('inb_updated'), 'success');
      } else {
        await STANNG.api('/api/inbounds', { method: 'POST', body: payload });
        STANNG.toast(STANNG.t('inb_created'), 'success');
      }
      closeModal('inboundModal');
      loadInbounds();
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    } finally {
      STANNG.setLoading(btn, false);
    }
  });

  // ---------------- renew ----------------
  function openRenewModal(ib) {
    $('renewUid').value = ib.uid;
    $('renewDays').value = ib.expire_days || 30;
    $('renewResetUsage').checked = true;
    openModal('renewModal');
  }

  $('renewSaveBtn').addEventListener('click', async () => {
    const uid = $('renewUid').value;
    const days = parseInt($('renewDays').value, 10);
    if (!uid || !days || days < 1) { STANNG.toast(STANNG.t('inb_invalid_number'), 'error'); return; }
    const btn = $('renewSaveBtn');
    STANNG.setLoading(btn, true);
    try {
      await STANNG.api(`/api/inbounds/${uid}/renew`, {
        method: 'POST',
        body: { days, reset_usage: $('renewResetUsage').checked },
      });
      STANNG.toast(STANNG.t('inb_renewed'), 'success');
      closeModal('renewModal');
      loadInbounds();
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    } finally {
      STANNG.setLoading(btn, false);
    }
  });

  async function handleInboundAction(action, uid) {
    const ib = currentInbounds.find(x => x.uid === uid);
    if (!ib) return;
    if (action === 'edit') return openInboundModal(ib);
    if (action === 'renew') return openRenewModal(ib);
    if (action === 'links') return showLinksModal(uid);

    const jobs = {
      reset: { path: `/api/inbounds/${uid}/reset-usage`, method: 'POST', ok: 'inb_reset_done' },
      regen: { path: `/api/inbounds/${uid}/regenerate`, method: 'POST', ok: 'inb_regenerated', confirm: 'inb_regenerate_confirm' },
      delete: { path: `/api/inbounds/${uid}`, method: 'DELETE', ok: 'inb_deleted', confirm: 'inb_delete_confirm' },
    };
    const job = jobs[action];
    if (!job) return;
    if (job.confirm && !confirm(STANNG.t(job.confirm))) return;
    try {
      await STANNG.api(job.path, { method: job.method });
      STANNG.toast(STANNG.t(job.ok), 'success');
      loadInbounds();
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    }
  }

  // ---------------- links modal ----------------
  async function showLinksModal(uid) {
    try {
      const r = await STANNG.api(`/api/inbounds/${uid}/links`);
      $('linkTls').textContent = r.links.tls || '';
      $('linkSub').textContent = r.sub_url || '';
      $('linkStatus').textContent = r.status_url || '';
      $('linkSubJson').textContent = r.sub_json_url || '';
      $('qrImg').src = `/api/inbounds/${uid}/qr?t=${Date.now()}`;

      const ib = currentInbounds.find(x => x.uid === uid);
      const ips = (ib && ib.active_ips) || [];
      $('activeIpsBlock').style.display = ips.length ? '' : 'none';
      $('activeIpsList').textContent = ips.join(', ');

      openModal('linksModal');
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    }
  }

  // ---------------- copy ----------------
  async function copyText(text) {
    if (!text) return;
    try {
      // clipboard.writeText needs a secure context; fall back for plain http.
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      STANNG.toast(STANNG.t('copied'), 'success', 1600);
      STANNG.playSfx('click', 0.4);
    } catch (e) {
      STANNG.toast('error', 'error');
    }
  }

  document.querySelectorAll('[data-copy]').forEach(btn => {
    btn.addEventListener('click', () => {
      const el = $(btn.dataset.copy);
      if (el) copyText(el.textContent);
    });
  });

  // ---------------- security ----------------
  $('securityForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const old_password = $('oldPassword').value;
    const new_username = $('newUsername').value.trim();
    const new_password = $('newPassword').value;
    const new_password2 = $('newPassword2').value;
    if (new_password && new_password !== new_password2) {
      STANNG.toast(STANNG.t('setup_mismatch'), 'error');
      STANNG.shake($('securityForm'));
      return;
    }
    const btn = $('securityBtn');
    STANNG.setLoading(btn, true);
    try {
      await STANNG.api('/api/change-password', {
        method: 'POST', body: { old_password, new_username, new_password },
      });
      STANNG.toast(STANNG.t('sec_updated'), 'success');
      $('securityForm').reset();
    } catch (e) {
      let msg = e.detail;
      if (msg === 'wrong-old-password') msg = STANNG.t('sec_wrong_old');
      else if (msg === 'weak-password') msg = STANNG.t('setup_password_hint');
      else if (msg === 'invalid-username') msg = STANNG.t('setup_username_hint');
      STANNG.toast(msg || 'error', 'error');
      STANNG.shake($('securityForm'));
    } finally {
      STANNG.setLoading(btn, false);
    }
  });

  // ---------------- settings ----------------
  async function saveSettings(btnId, payload, after) {
    const btn = $(btnId);
    STANNG.setLoading(btn, true);
    try {
      const r = await STANNG.api('/api/settings', { method: 'POST', body: payload });
      STANNG.toast(STANNG.t('settings_saved'), 'success');
      if (after) after(r);
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    } finally {
      STANNG.setLoading(btn, false);
    }
  }

  $('saveSettingsBtn').addEventListener('click', () => {
    STANNG.setSoundEnabled($('settingSound').checked);
    saveSettings('saveSettingsBtn', {
      public_domain: $('settingPublicDomain').value.trim(),
      ota_repo: $('settingOtaRepo').value.trim(),
      keep_alive: $('settingKeepAlive').checked,
    });
  });

  $('saveAdvancedBtn').addEventListener('click', () => {
    saveSettings('saveAdvancedBtn', {
      default_fingerprint: $('settingFingerprint').value,
      default_alpn: $('settingAlpn').value,
      sni_override: $('settingSniOverride').value.trim(),
      fragment_enabled: $('settingFragmentEnabled').checked,
      fragment_packets: $('settingFragmentPackets').value.trim() || 'tlshello',
      fragment_length: $('settingFragmentLength').value.trim() || '10-30',
      fragment_interval: $('settingFragmentInterval').value.trim() || '10-20',
    });
  });

  $('saveBrandBtn').addEventListener('click', () => {
    saveSettings('saveBrandBtn', {
      panel_name: $('settingPanelName').value.trim(),
      telegram_contact: $('settingTelegram').value.trim(),
    }, () => setTimeout(() => window.location.reload(), 700));
  });

  $('saveRelayBtn').addEventListener('click', () => {
    saveSettings('saveRelayBtn', {
      allow_private_destinations: $('settingAllowPrivate').checked,
      idle_timeout_seconds: parseInt($('settingIdleTimeout').value, 10) || 0,
    });
  });

  function applyFragmentFieldState() {
    const on = $('settingFragmentEnabled').checked;
    const fields = $('fragmentFields');
    fields.style.opacity = on ? '1' : '.45';
    fields.style.pointerEvents = on ? 'auto' : 'none';
  }
  $('settingFragmentEnabled').addEventListener('change', applyFragmentFieldState);

  // ---------------- backup / restore ----------------
  $('backupBtn').addEventListener('click', async () => {
    const btn = $('backupBtn');
    STANNG.setLoading(btn, true);
    try {
      const res = await fetch('/api/backup', { credentials: 'same-origin' });
      if (!res.ok) throw new Error('backup-failed');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = (res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/)?.[1]
        || 'stanng-backup.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      STANNG.toast(e.message || 'error', 'error');
    } finally {
      STANNG.setLoading(btn, false);
    }
  });

  $('restoreBtn').addEventListener('click', () => $('restoreFile').click());

  $('restoreFile').addEventListener('change', async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (!confirm(STANNG.t('settings_restore_confirm'))) return;
    try {
      const parsed = JSON.parse(await file.text());
      await STANNG.api('/api/restore', { method: 'POST', body: { db: parsed } });
      STANNG.toast(STANNG.t('settings_restored'), 'success', 4000);
      setTimeout(() => { window.location.href = '/login'; }, 1500);
    } catch (err) {
      STANNG.toast(err.detail || err.message || 'invalid-backup', 'error');
    }
  });

  $('logoutAllBtn').addEventListener('click', async () => {
    if (!confirm(STANNG.t('settings_logout_all_confirm'))) return;
    try {
      await STANNG.api('/api/logout-all', { method: 'POST' });
    } catch (e) { /* signing out regardless */ }
    window.location.href = '/login';
  });

  // ---------------- initial load ----------------
  loadInbounds();
})();
