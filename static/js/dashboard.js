/* ===========================================================
   Dashboard controller.

   Views: dashboard, users, plans, traffic, security,
   notifications, settings. All markup is built through
   PEYK.escapeHtml — user- and API-supplied strings are never
   interpolated raw into innerHTML.
   =========================================================== */
(() => {
  let inbounds = [];
  let plans = [];
  let endpoints = [];
  let resellers = [];
  let isOwner = true;
  let sharingThreshold = 0;
  let vouchers = [];
  let currency = 'تومان';
  let lastHourly = [];
  let filterText = '';
  let filterStatus = '';
  const selected = new Set();

  const $ = (id) => document.getElementById(id);
  const esc = PEYK.escapeHtml;
  const DAY = 86400;

  /* Server error codes are kebab-case ("reseller-user-limit"); the ones worth
     showing a human have an rs_err_* entry. t() echoes unknown keys straight
     back, so the dictionary is probed directly rather than trusted to miss. */
  function apiErrorText(detail) {
    const key = 'rs_err_' + detail;
    if (window.I18N && window.I18N[key]) return PEYK.t(key);
    return detail || 'error';
  }

  // ---------------- session guard + settings hydrate ----------------
  PEYK.api('/api/me').then(me => {
    if (!me.logged_in) { window.location.href = '/login'; return; }
    $('appVersion').textContent = me.app_version || '';
    $('otaCurrent').textContent = me.app_version || '-';
    applyRole(me);
    if (!isOwner) return;   // everything below this line is owner-only

    const s = me.settings || {};
    const set = (id, v) => { const el = $(id); if (el) el.value = v; };
    const check = (id, v) => { const el = $(id); if (el) el.checked = v; };

    set('settingPublicDomain', s.public_domain || '');
    set('settingOtaRepo', s.ota_repo || '');
    check('settingKeepAlive', s.keep_alive !== false);
    set('settingFingerprint', s.default_fingerprint || 'chrome');
    set('settingAlpn', s.default_alpn || 'http/1.1');
    set('settingSniOverride', s.sni_override || '');
    check('settingFragmentEnabled', s.fragment_enabled !== false);
    set('settingFragmentPackets', s.fragment_packets || 'tlshello');
    set('settingFragmentLength', s.fragment_length || '10-30');
    set('settingFragmentInterval', s.fragment_interval || '10-20');
    set('settingPanelName', s.panel_name || '');
    set('settingTelegram', s.telegram_contact || '');
    check('settingAllowPrivate', !!s.allow_private_destinations);
    check('settingHealthEnabled', !!s.health_check_enabled);
    set('settingHealthInterval', s.health_interval_minutes != null ? s.health_interval_minutes : 15);
    set('settingHealthThreshold', s.health_fail_threshold != null ? s.health_fail_threshold : 3);
    check('settingHealthAutoDisable', !!s.health_auto_disable);
    check('settingSharingEnabled', !!s.sharing_detect_enabled);
    set('settingSharingWindow', s.sharing_window_hours != null ? s.sharing_window_hours : 24);
    set('settingSharingThreshold', s.sharing_threshold != null ? s.sharing_threshold : 4);
    check('settingSharingAutoDisable', !!s.sharing_auto_disable);
    set('settingIdleTimeout', s.idle_timeout_seconds != null ? s.idle_timeout_seconds : 600);
    set('settingHistoryDays', s.history_days != null ? s.history_days : 30);
    set('settingBotToken', s.telegram_bot_token || '');
    set('settingChatId', s.telegram_chat_id || '');
    check('settingNotifyQuota', s.notify_quota_enabled !== false);
    set('settingNotifyPercent', s.notify_quota_percent != null ? s.notify_quota_percent : 80);
    check('settingNotifyExpiry', s.notify_expiry_enabled !== false);
    check('settingRenewEnabled', !!s.userbot_renew_enabled);
    check('settingVoucherRedeem', !!s.voucher_redeem_enabled);
    check('settingNotifyCustomer', !!s.notify_customer_enabled);
    check('settingShopEnabled', !!s.shop_enabled);
    set('settingShopInstructions', s.shop_instructions || '');
    check('settingTrialSelfserve', !!s.trial_selfserve_enabled);
    set('settingRenewOptions', (s.userbot_renew_options || [30, 60, 90]).join(','));
    check('settingAutoBackup', !!s.auto_backup_enabled);
    check('settingCleanup', !!s.cleanup_enabled);
    set('settingCleanupDisable', s.cleanup_disable_days != null ? s.cleanup_disable_days : 3);
    set('settingCleanupDelete', s.cleanup_delete_days != null ? s.cleanup_delete_days : 30);
    check('settingTrialEnabled', s.trial_enabled !== false);
    set('settingTrialGb', s.trial_gb != null ? s.trial_gb : 1);
    set('settingTrialDays', s.trial_days != null ? s.trial_days : 1);
    set('settingTrialPrefix', s.trial_prefix || 'trial');
    loadFragmentProfiles(s.fragment_profile);
    check('settingUserbotEnabled', !!s.userbot_enabled);
    set('settingUserbotToken', s.userbot_token || '');
    set('settingBackupHours', s.auto_backup_hours != null ? s.auto_backup_hours : 6);
    set('settingNotifyDays', s.notify_expiry_days != null ? s.notify_expiry_days : 3);
    applyFragmentFieldState();
    loadResellers();
    loadEndpoints();
  }).catch(() => { window.location.href = '/login'; });

  /* The server refuses these endpoints for a reseller regardless; hiding them
     is so the panel does not offer buttons that can only fail. */
  function applyRole(me) {
    isOwner = me.role !== 'reseller';
    document.querySelectorAll('[data-owner-only]').forEach(el => {
      if (!isOwner) el.style.display = 'none';
    });
    document.querySelectorAll('[data-reseller-only]').forEach(el => {
      el.style.display = isOwner ? 'none' : '';
    });
    if (!isOwner) {
      document.body.classList.add('is-reseller');
      renderMyQuota(me.quota, me.usage);
      const badge = $('roleBadge');
      if (badge) { badge.textContent = PEYK.t('rs_role_badge'); badge.style.display = ''; }
      // The username half of that form is owner-only, so the heading would lie.
      const secTitle = $('secFormTitle');
      if (secTitle) {
        secTitle.setAttribute('data-i18n', 'rs_change_pass');
        secTitle.textContent = PEYK.t('rs_change_pass');
      }
    }
  }

  function renderMyQuota(quota, usage) {
    if (!quota || !usage) return;
    const unlimited = PEYK.t('unlimited');
    const setBar = (id, used, cap) => {
      const pct = cap ? Math.min(100, (used / cap) * 100) : 0;
      const bar = $(id);
      if (bar) bar.style.width = pct + '%';
      return pct;
    };
    $('myQuotaUsersText').textContent = quota.max_users
      ? `${usage.users} / ${quota.max_users}` : `${usage.users} / ${unlimited}`;
    setBar('myQuotaUsersBar', usage.users, quota.max_users);

    const usedGb = (usage.traffic_gb || 0).toFixed(2);
    $('myQuotaTrafficText').textContent = quota.max_traffic_gb
      ? `${usedGb} / ${quota.max_traffic_gb} GB` : `${usedGb} GB / ${unlimited}`;
    setBar('myQuotaTrafficBar', usage.traffic_gb, quota.max_traffic_gb);
  }

  $('settingSound').checked = PEYK.isSoundEnabled();

  // ---------------- navigation ----------------
  const views = document.querySelectorAll('.view');
  const navItems = document.querySelectorAll('.nav-item[data-view]');
  const viewTitle = $('viewTitle');
  const titleKeys = {
    dashboard: 'nav_dashboard', inbounds: 'nav_inbounds', plans: 'nav_plans',
    resellers: 'nav_resellers', endpoints: 'nav_endpoints', traffic: 'nav_traffic',
    sales: 'nav_sales',
    security: 'nav_security', notifications: 'nav_notifications', settings: 'nav_settings',
  };

  function showView(name) {
    views.forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
    navItems.forEach(n => n.classList.toggle('active', n.dataset.view === name));
    viewTitle.setAttribute('data-i18n', titleKeys[name]);
    viewTitle.textContent = PEYK.t(titleKeys[name]);
    if (name === 'inbounds' || name === 'traffic') loadInbounds();
    if (name === 'plans') loadPlans();
    if (name === 'resellers') loadResellers();
    if (name === 'sales') { loadSales(); loadVouchers(); }
    if (name === 'endpoints') loadEndpoints();
    if (name === 'security' && isOwner) { loadTwofaStatus(); loadLoginLog(); }
    if (name === 'settings') { loadBackupStatus(); loadCleanupStatus(); }
    if (name === 'notifications') loadRenewRequests(false);
    if (name === 'notifications') loadUserbotStatus();
    closeSidebarMobile();
    PEYK.playSfx('open', 0.25);
  }
  navItems.forEach(item => item.addEventListener('click', () => showView(item.dataset.view)));

  // ---------------- mobile drawer ----------------
  const sidebar = $('sidebar');
  const backdrop = $('sidebarBackdrop');
  $('menuToggle').addEventListener('click', () => {
    const opening = !sidebar.classList.contains('open');
    sidebar.classList.toggle('open', opening);
    backdrop.classList.toggle('open', opening);
  });
  backdrop.addEventListener('click', closeSidebarMobile);
  function closeSidebarMobile() {
    sidebar.classList.remove('open');
    backdrop.classList.remove('open');
  }

  // ---------------- lang / theme / sound ----------------
  document.querySelectorAll('.lang-toggle button').forEach(b => {
    b.addEventListener('click', () => {
      PEYK.setLang(b.dataset.lang);
      viewTitle.textContent = PEYK.t(viewTitle.getAttribute('data-i18n'));
      renderInbounds(); renderTrafficTable(); renderPlans(); renderResellers(); renderVouchers();
      PEYK.playSfx('toggle', 0.25);
    });
  });
  $('themeToggle').addEventListener('click', () => {
    PEYK.setTheme(PEYK.getTheme() === 'dark' ? 'light' : 'dark');
    PEYK.playSfx('toggle', 0.25);
    renderTrafficChart($('trafficChart'), lastHourly);
  });
  $('soundToggle').addEventListener('click', () => {
    const next = !PEYK.isSoundEnabled();
    PEYK.setSoundEnabled(next);
    $('settingSound').checked = next;
    if (next) PEYK.playSfx('click');
  });

  $('logoutBtn').addEventListener('click', async () => {
    try { await PEYK.api('/api/logout', { method: 'POST' }); } catch (e) { /* leave anyway */ }
    window.location.href = '/login';
  });

  // ---------------- modals ----------------
  function openModal(id) { $(id).classList.add('open'); PEYK.playSfx('open', 0.3); }
  function closeModal(id) { $(id).classList.remove('open'); PEYK.playSfx('close', 0.3); }
  document.querySelectorAll('[data-close-modal]').forEach(b => {
    b.addEventListener('click', () => closeModal(b.dataset.closeModal));
  });
  document.querySelectorAll('.modal-overlay').forEach(ov => {
    ov.addEventListener('click', (e) => { if (e.target === ov) closeModal(ov.id); });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const open = document.querySelector('.modal-overlay.open');
    if (open) closeModal(open.id);
  });

  // ---------------- stats polling ----------------
  let statsTimer = null;
  async function refreshStats() {
    try {
      const s = await PEYK.api('/stats');
      $('statCpu').textContent = s.cpu_percent.toFixed(1) + '%';
      $('barCpu').style.width = Math.min(100, s.cpu_percent) + '%';
      $('statMem').textContent = s.mem_percent.toFixed(1) + '%';
      $('barMem').style.width = Math.min(100, s.mem_percent) + '%';
      $('statUptime').textContent = PEYK.fmtDuration(s.uptime_seconds);
      const loc = s.location || {};
      $('statLocation').textContent = `${loc.flag || ''} ${loc.city || '?'}`;
      $('statTotalTraffic').textContent = PEYK.fmtBytes((s.total_up || 0) + (s.total_down || 0));
      $('statUp').textContent = PEYK.fmtBytes(s.total_up || 0);
      $('statDown').textContent = PEYK.fmtBytes(s.total_down || 0);
      PEYK.countUp($('statActiveConn'), s.active_connections || 0);
      PEYK.countUp($('statInboundCount'), s.inbounds_count || 0);
      $('navInboundCount').textContent = s.inbounds_count || 0;
      $('trafficUp').textContent = PEYK.fmtBytes(s.total_up || 0);
      $('trafficDown').textContent = PEYK.fmtBytes(s.total_down || 0);
      lastHourly = s.hourly || [];
      if (isOwner) renderTrafficChart($('trafficChart'), lastHourly);
      else renderMyQuota(s.quota, s.usage);
    } catch (e) {
      if (e.status === 401) window.location.href = '/login';
    }
  }
  function startStats() { if (!statsTimer) { refreshStats(); statsTimer = setInterval(refreshStats, 8000); } }
  function stopStats() { if (statsTimer) { clearInterval(statsTimer); statsTimer = null; } }
  document.addEventListener('visibilitychange', () => document.hidden ? stopStats() : startStats());
  startStats();

  let resizeRaf = null;
  window.addEventListener('resize', () => {
    if (resizeRaf) cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(() => renderTrafficChart($('trafficChart'), lastHourly));
  });

  // ---------------- OTA ----------------
  let otaLatest = null;
  $('otaCheckBtn').addEventListener('click', async () => {
    const btn = $('otaCheckBtn'), el = $('otaResult');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/ota/check');
      if (r.no_releases) {
        // Distinct from being current: the repo has never cut a release, so
        // there is nothing to compare against and nothing to install.
        el.innerHTML = `<span style="color:var(--warn)">${esc(PEYK.t('ota_no_releases'))}</span>`;
        otaLatest = null;
        $('otaUpdateBtn').style.display = 'none';
        $('otaUpdateHint').style.display = 'none';
      } else if (r.update_available) {
        el.innerHTML = `<span style="color:var(--accent)">${esc(PEYK.t('dash_ota_available'))} <b>${esc(r.latest)}</b></span> — <a href="${esc(r.url)}" target="_blank" rel="noopener" style="color:var(--info); text-decoration:underline;">GitHub</a>`;
        otaLatest = r.latest;
        $('otaUpdateBtn').style.display = '';
        $('otaUpdateHint').style.display = '';
      } else {
        el.innerHTML = `<span style="color:var(--ok)">${esc(PEYK.t('dash_ota_uptodate'))}</span>`;
        otaLatest = null;
        $('otaUpdateBtn').style.display = 'none';
        $('otaUpdateHint').style.display = 'none';
      }
    } catch (e) {
      let msg = e.detail || 'error';
      if (msg === 'no-repo-configured') msg = PEYK.t('ota_no_repo');
      PEYK.toast(msg, 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  $('otaUpdateBtn').addEventListener('click', async () => {
    if (!confirm(PEYK.t('dash_ota_update_confirm').replace('{version}', otaLatest || ''))) return;
    const btn = $('otaUpdateBtn');
    PEYK.setLoading(btn, true);
    $('otaCheckBtn').disabled = true;
    try {
      const r = await PEYK.api('/api/ota/update', { method: 'POST' });
      if (r.ok) {
        $('otaResult').innerHTML = `<span style="color:var(--accent)">${esc(PEYK.t('dash_ota_updating'))}</span>`;
        waitForRestart();
      } else {
        PEYK.toast(PEYK.t('dash_ota_uptodate'), 'success');
        PEYK.setLoading(btn, false);
        $('otaCheckBtn').disabled = false;
      }
    } catch (e) {
      PEYK.toast(e.detail || 'error', 'error');
      PEYK.setLoading(btn, false);
      $('otaCheckBtn').disabled = false;
    }
  });

  function waitForRestart() {
    let n = 0;
    const poll = setInterval(async () => {
      n++;
      try {
        const res = await fetch('/health', { cache: 'no-store' });
        if (res.ok) {
          clearInterval(poll);
          PEYK.toast(PEYK.t('dash_ota_done'), 'success', 3000);
          setTimeout(() => window.location.reload(), 1200);
          return;
        }
      } catch (e) { /* restarting */ }
      if (n > 60) { clearInterval(poll); PEYK.toast(PEYK.t('dash_ota_timeout'), 'error', 8000); }
    }, 3000);
  }

  $('quickAddBtn').addEventListener('click', () => { showView('inbounds'); openInboundModal(); });
  $('quickBulkBtn').addEventListener('click', () => { showView('inbounds'); openBulkModal(); });

  // ---------------- users ----------------
  let inboundsLoaded = false;

  async function loadInbounds() {
    if (!inboundsLoaded) PEYK.skeletonRows($('inboundsTableBody'), 6, 7);
    try {
      const r = await PEYK.api('/api/inbounds');
      inbounds = r.inbounds || [];
      sharingThreshold = r.sharing_threshold || 0;
      // Drop selections for rows that no longer exist.
      const live = new Set(inbounds.map(i => i.uid));
      [...selected].forEach(uid => { if (!live.has(uid)) selected.delete(uid); });
      animateRows = !inboundsLoaded;
      inboundsLoaded = true;
      renderInbounds();
      animateRows = false;
      renderTrafficTable();
      $('navInboundCount').textContent = inbounds.length;
      const soon = inbounds.filter(i => i.status.days_left != null
        && i.status.days_left <= 7 && !i.status.expired).length;
      PEYK.countUp($('statExpiring'), soon);
    } catch (e) {
      if (e.status === 401) { window.location.href = '/login'; return; }
      PEYK.toast(e.detail || 'error', 'error');
    }
  }

  function statusLabel(st) {
    if (st.live_enabled) return { key: 'active', cls: 'pill-on' };
    if (st.expired) return { key: 'expired', cls: 'pill-off' };
    if (st.quota_exceeded) return { key: 'status_quota_over', cls: 'pill-off' };
    if (st.request_exceeded) return { key: 'status_requests_over', cls: 'pill-off' };
    if (st.disabled) return { key: 'inb_disabled_manual', cls: 'pill-muted' };
    return { key: 'inactive', cls: 'pill-off' };
  }

  /* Shown only once detection is on, so the column does not carry a
     permanently empty slot for panels that never enable it. */
  function sharingBadge(ib) {
    if (!sharingThreshold || !ib.sharing) return '';
    const n = ib.sharing.networks || 0;
    // Judged on the count, not on sharing_flagged: the flag is only set by the
    // sweep, and the row should not read "fine" for the 15 minutes until it runs.
    if (n >= sharingThreshold) {
      return ` <span class="pill pill-off" title="${esc(PEYK.t('shr_flag_title'))}">`
        + `<span class="pill-dot"></span>${esc(PEYK.t('shr_flag'))} ${n}</span>`;
    }
    // A near miss is worth seeing before it trips.
    if (n >= sharingThreshold - 1) {
      return ` <span class="pill pill-warn" title="${esc(PEYK.t('shr_near_title'))}">${n}</span>`;
    }
    return '';
  }

  function usageClass(pct) {
    if (pct >= 90) return 'progress-danger';
    if (pct >= 70) return 'progress-warn';
    return '';
  }

  function visibleInbounds() {
    const needle = filterText.toLowerCase();
    return inbounds.filter(ib => {
      if (needle && !(ib.name || '').toLowerCase().includes(needle)
          && !(ib.note || '').toLowerCase().includes(needle)) return false;
      const st = ib.status;
      if (filterStatus === 'active') return st.live_enabled;
      if (filterStatus === 'inactive') return !st.live_enabled;
      if (filterStatus === 'expiring') return st.days_left != null && st.days_left <= 7 && !st.expired;
      return true;
    });
  }

  /* Only the first paint after a load is staggered. Re-rendering on a filter
     keystroke or a language switch should feel instant, not choreographed. */
  let animateRows = false;

  function renderInbounds() {
    const tbody = $('inboundsTableBody');
    const rows = visibleInbounds();
    $('inboundsEmpty').style.display = rows.length ? 'none' : 'block';
    tbody.innerHTML = '';

    const frag = document.createDocumentFragment();
    rows.forEach(ib => {
      const st = ib.status;
      const label = statusLabel(st);
      const pct = st.quota_bytes > 0 ? Math.min(100, (st.used / st.quota_bytes) * 100)
                                     : (st.used > 0 ? 6 : 0);
      const quotaTxt = ib.quota_gb > 0
        ? `${PEYK.fmtBytes(st.used)} / ${ib.quota_gb} GB`
        : `${PEYK.fmtBytes(st.used)} / ${PEYK.t('unlimited')}`;
      const expireTxt = ib.expire_at
        ? `${st.days_left} ${PEYK.t('inb_days_left')}`
        : PEYK.t('inb_no_expire');

      const tr = document.createElement('tr');
      tr.className = (selected.has(ib.uid) ? 'is-selected ' : '')
        + (animateRows ? 'row-enter' : '');
      tr.innerHTML = `
        <td class="col-check" data-label=""><input type="checkbox" class="checkbox row-check" data-uid="${esc(ib.uid)}" ${selected.has(ib.uid) ? 'checked' : ''} aria-label="select"></td>
        <td data-label="${esc(PEYK.t('inb_name'))}"><b>${esc(ib.name)}</b>${sharingBadge(ib)}${ib.note ? `<div class="small muted">${esc(ib.note)}</div>` : ''}</td>
        <td data-label="${esc(PEYK.t('inb_status'))}"><span class="pill ${label.cls}"><span class="pill-dot"></span>${esc(PEYK.t(label.key))}</span></td>
        <td data-label="${esc(PEYK.t('inb_usage'))}" style="min-width:150px;">
          <div class="small mono">${esc(quotaTxt)}</div>
          <div class="bar ${usageClass(pct)}" style="margin-top:5px;"><span style="width:${pct}%"></span></div>
        </td>
        <td data-label="${esc(PEYK.t('inb_expire'))}" class="mono small">${esc(expireTxt)}</td>
        <td data-label="${esc(PEYK.t('inb_max_conn'))}" class="mono small">${esc(st.active_connections)}${ib.max_connections ? ' / ' + esc(ib.max_connections) : ''}</td>
        <td data-label="${esc(PEYK.t('inb_actions'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-action="links" data-uid="${esc(ib.uid)}" title="${esc(PEYK.t('inb_links'))}"><svg><use href="#icon-qr"/></svg></button>
            <button class="icon-btn btn-sm" data-action="edit" data-uid="${esc(ib.uid)}" title="${esc(PEYK.t('edit'))}"><svg><use href="#icon-edit"/></svg></button>
            <span class="row-menu">
              <button class="icon-btn btn-sm" data-menu-toggle title="${esc(PEYK.t('more'))}" aria-label="${esc(PEYK.t('more'))}" aria-haspopup="true" aria-expanded="false"><svg><use href="#icon-dots"/></svg></button>
              <span class="row-menu-pop" role="menu">
                <button role="menuitem" data-action="renew" data-uid="${esc(ib.uid)}"><svg><use href="#icon-clock"/></svg>${esc(PEYK.t('inb_renew'))}</button>
                <button role="menuitem" data-action="history" data-uid="${esc(ib.uid)}"><svg><use href="#icon-chart"/></svg>${esc(PEYK.t('traffic_history'))}</button>
                ${sharingThreshold ? `<button role="menuitem" data-action="networks" data-uid="${esc(ib.uid)}"><svg><use href="#icon-globe"/></svg>${esc(PEYK.t('shr_modal_title'))}</button>` : ''}
                <button role="menuitem" data-action="reset" data-uid="${esc(ib.uid)}"><svg><use href="#icon-refresh"/></svg>${esc(PEYK.t('inb_reset_usage'))}</button>
                <button role="menuitem" data-action="regen" data-uid="${esc(ib.uid)}"><svg><use href="#icon-key"/></svg>${esc(PEYK.t('inb_regenerate'))}</button>
                <hr>
                <button role="menuitem" class="danger" data-action="delete" data-uid="${esc(ib.uid)}"><svg><use href="#icon-trash"/></svg>${esc(PEYK.t('delete'))}</button>
              </span>
            </span>
          </div>
        </td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
    syncSelectionUI();
  }

  /* One menu open at a time, dismissed by choosing something, clicking away,
     or Escape — all three, because any one alone leaves it stuck open. */
  function closeRowMenus() {
    document.querySelectorAll('.row-menu.open').forEach(m => {
      m.classList.remove('open');
      const t = m.querySelector('[data-menu-toggle]');
      if (t) t.setAttribute('aria-expanded', 'false');
    });
  }
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.row-menu')) closeRowMenus();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeRowMenus();
  });

  // Delegated: one listener regardless of row count.
  $('inboundsTableBody').addEventListener('click', (e) => {
    const toggle = e.target.closest('[data-menu-toggle]');
    if (toggle) {
      const menu = toggle.closest('.row-menu');
      const wasOpen = menu.classList.contains('open');
      closeRowMenus();
      if (!wasOpen) {
        // Flip upward when there is no room below, so the last rows of a long
        // table are still usable.
        const room = window.innerHeight - toggle.getBoundingClientRect().bottom;
        menu.classList.toggle('flip-up', room < 250);
        menu.classList.add('open');
        toggle.setAttribute('aria-expanded', 'true');
      }
      return;
    }
    const btn = e.target.closest('button[data-action]');
    if (btn) { closeRowMenus(); handleAction(btn.dataset.action, btn.dataset.uid); return; }
  });
  $('inboundsTableBody').addEventListener('change', (e) => {
    const box = e.target.closest('.row-check');
    if (!box) return;
    box.checked ? selected.add(box.dataset.uid) : selected.delete(box.dataset.uid);
    box.closest('tr').classList.toggle('is-selected', box.checked);
    syncSelectionUI();
  });

  $('selectAll').addEventListener('change', (e) => {
    const rows = visibleInbounds();
    if (e.target.checked) rows.forEach(ib => selected.add(ib.uid));
    else rows.forEach(ib => selected.delete(ib.uid));
    renderInbounds();
  });

  function syncSelectionUI() {
    const rows = visibleInbounds();
    const n = rows.filter(ib => selected.has(ib.uid)).length;
    $('bulkCount').textContent = n;
    $('bulkBar').classList.toggle('show', n > 0);
    const all = $('selectAll');
    all.checked = n > 0 && n === rows.length;
    all.indeterminate = n > 0 && n < rows.length;
  }

  $('inboundSearch').addEventListener('input', (e) => { filterText = e.target.value; renderInbounds(); });
  $('inboundFilterStatus').addEventListener('change', (e) => { filterStatus = e.target.value; renderInbounds(); });

  // ---------------- bulk actions ----------------
  document.querySelectorAll('[data-bulk]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const action = btn.dataset.bulk;
      const uids = [...selected];
      if (!uids.length) return;
      const confirmKeys = { delete: 'bulk_delete_confirm', disable: 'bulk_disable_confirm' };
      if (confirmKeys[action] && !confirm(PEYK.t(confirmKeys[action]).replace('{n}', uids.length))) return;

      const body = { action, uids };
      if (action === 'renew') {
        const days = parseInt(prompt(PEYK.t('inb_renew_days'), '30'), 10);
        if (!days || days < 1) return;
        body.days = days;
      }
      PEYK.setLoading(btn, true);
      try {
        const r = await PEYK.api('/api/inbounds/bulk-action', { method: 'POST', body });
        PEYK.toast(PEYK.t('bulk_done').replace('{n}', r.affected), 'success');
        selected.clear();
        loadInbounds();
      } catch (e) {
        PEYK.toast(e.detail || 'error', 'error');
      } finally { PEYK.setLoading(btn, false); }
    });
  });

  // ---------------- user modal ----------------
  function fillPlanSelect(sel) {
    const el = $(sel);
    const keep = el.value;
    el.innerHTML = `<option value="">${esc(PEYK.t('inb_no_plan'))}</option>`;
    plans.forEach(p => {
      const o = document.createElement('option');
      o.value = p.id;
      o.textContent = `${p.name} — ${p.quota_gb || '∞'}GB / ${p.days || '∞'}d`;
      el.appendChild(o);
    });
    el.value = keep;
  }

  function openInboundModal(ib = null) {
    $('inboundModalTitle').textContent = ib ? PEYK.t('edit') : PEYK.t('inb_add');
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
    $('fPlan').value = '';
    $('planPickerField').style.display = plans.length ? '' : 'none';
    fillPlanSelect('fPlan');
    openModal('inboundModal');
  }

  $('fPlan').addEventListener('change', (e) => {
    const p = plans.find(x => x.id === e.target.value);
    if (!p) return;
    $('fQuota').value = p.quota_gb || '';
    $('fExpire').value = p.days || '';
    $('fMaxConn').value = p.max_connections || '';
    $('fMaxReq').value = p.max_requests || '';
  });

  $('addInboundBtn').addEventListener('click', () => openInboundModal());

  const num = (id) => { const v = $(id).value.trim(); return v === '' ? 0 : Number(v); };

  $('inboundSaveBtn').addEventListener('click', async () => {
    const uid = $('inboundUid').value;
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
    if (Object.values(payload).some(v => typeof v === 'number' && !isFinite(v))) {
      PEYK.toast(PEYK.t('inb_invalid_number'), 'error'); return;
    }
    const btn = $('inboundSaveBtn');
    PEYK.setLoading(btn, true);
    try {
      if (uid) {
        await PEYK.api(`/api/inbounds/${uid}`, { method: 'PATCH', body: payload });
        PEYK.toast(PEYK.t('inb_updated'), 'success');
      } else {
        await PEYK.api('/api/inbounds', { method: 'POST', body: payload });
        PEYK.toast(PEYK.t('inb_created'), 'success');
      }
      closeModal('inboundModal');
      loadInbounds();
    } catch (e) {
      PEYK.toast(apiErrorText(e.detail), 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  // ---------------- bulk create ----------------
  function openBulkModal() {
    fillPlanSelect('bulkPlan');
    $('bulkPlan').value = '';
    updateBulkPreview();
    openModal('bulkModal');
  }
  $('bulkCreateBtn').addEventListener('click', openBulkModal);

  function updateBulkPreview() {
    const count = Math.max(1, parseInt($('bulkCountInput').value, 10) || 1);
    const start = Math.max(1, parseInt($('bulkStart').value, 10) || 1);
    const prefix = $('bulkPrefix').value.trim() || 'user';
    const width = String(start + count - 1).length;
    const first = `${prefix}-${String(start).padStart(width, '0')}`;
    const last = `${prefix}-${String(start + count - 1).padStart(width, '0')}`;
    $('bulkPreview').textContent = count > 1 ? `${first} … ${last}` : first;
  }
  ['bulkCountInput', 'bulkStart', 'bulkPrefix'].forEach(id =>
    $(id).addEventListener('input', updateBulkPreview));

  $('bulkPlan').addEventListener('change', (e) => {
    const p = plans.find(x => x.id === e.target.value);
    if (!p) return;
    $('bulkQuota').value = p.quota_gb || '';
    $('bulkExpire').value = p.days || '';
    $('bulkMaxConn').value = p.max_connections || '';
  });

  $('bulkSaveBtn').addEventListener('click', async () => {
    const body = {
      count: parseInt($('bulkCountInput').value, 10) || 0,
      prefix: $('bulkPrefix').value.trim() || 'user',
      start_index: parseInt($('bulkStart').value, 10) || 1,
    };
    const planId = $('bulkPlan').value;
    if (planId) body.plan_id = planId;
    else {
      body.quota_gb = num('bulkQuota');
      body.expire_days = num('bulkExpire');
      body.max_connections = num('bulkMaxConn');
    }
    const btn = $('bulkSaveBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/inbounds/bulk', { method: 'POST', body });
      PEYK.toast(PEYK.t('bulk_created').replace('{n}', r.created), 'success');
      closeModal('bulkModal');
      loadInbounds();
    } catch (e) {
      PEYK.toast(apiErrorText(e.detail), 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  // ---------------- plans ----------------
  async function loadPlans() {
    try {
      const r = await PEYK.api('/api/plans');
      plans = r.plans || [];
      renderPlans();
    } catch (e) { /* non-fatal */ }
  }

  function renderPlans() {
    const tbody = $('plansTableBody');
    if (!tbody) return;
    $('plansEmpty').style.display = plans.length ? 'none' : 'block';
    tbody.innerHTML = '';
    const frag = document.createDocumentFragment();
    plans.forEach(p => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td data-label="${esc(PEYK.t('plans_name'))}"><b>${esc(p.name)}</b></td>
        <td data-label="${esc(PEYK.t('inb_quota'))}" class="mono small">${p.quota_gb ? esc(p.quota_gb) + ' GB' : esc(PEYK.t('unlimited'))}</td>
        <td data-label="${esc(PEYK.t('inb_expire'))}" class="mono small">${p.days ? esc(p.days) + ' ' + esc(PEYK.t('inb_days_left')) : esc(PEYK.t('unlimited'))}</td>
        <td data-label="${esc(PEYK.t('inb_max_conn'))}" class="mono small">${p.max_connections || esc(PEYK.t('unlimited'))}</td>
        <td data-label="${esc(PEYK.t('plans_price'))}" class="mono small">${p.price ? fmtMoney(p.price) : '—'}</td>
        <td data-label="${esc(PEYK.t('inb_actions'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-plan-action="use" data-id="${esc(p.id)}" title="${esc(PEYK.t('inb_bulk_create'))}"><svg><use href="#icon-layers"/></svg></button>
            <button class="icon-btn btn-sm" data-plan-action="edit" data-id="${esc(p.id)}" title="${esc(PEYK.t('edit'))}"><svg><use href="#icon-edit"/></svg></button>
            <button class="icon-btn btn-sm" data-plan-action="delete" data-id="${esc(p.id)}" title="${esc(PEYK.t('delete'))}" style="color:var(--err)"><svg><use href="#icon-trash"/></svg></button>
          </div>
        </td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }

  $('plansTableBody').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-plan-action]');
    if (!btn) return;
    const plan = plans.find(p => p.id === btn.dataset.id);
    if (!plan) return;
    if (btn.dataset.planAction === 'edit') return openPlanModal(plan);
    if (btn.dataset.planAction === 'use') {
      showView('inbounds');
      openBulkModal();
      $('bulkPlan').value = plan.id;
      $('bulkPlan').dispatchEvent(new Event('change'));
      return;
    }
    if (!confirm(PEYK.t('plans_delete_confirm'))) return;
    try {
      await PEYK.api(`/api/plans/${plan.id}`, { method: 'DELETE' });
      PEYK.toast(PEYK.t('plans_deleted'), 'success');
      loadPlans();
    } catch (err) { PEYK.toast(err.detail || 'error', 'error'); }
  });

  function openPlanModal(plan = null) {
    $('planModalTitle').textContent = plan ? PEYK.t('edit') : PEYK.t('plans_add');
    $('planId').value = plan ? plan.id : '';
    $('planName').value = plan ? plan.name : '';
    $('planQuota').value = plan ? (plan.quota_gb || '') : '';
    $('planDays').value = plan ? (plan.days || '') : '';
    $('planMaxConn').value = plan ? (plan.max_connections || '') : '';
    $('planMaxReq').value = plan ? (plan.max_requests || '') : '';
    $('planPrice').value = plan ? (plan.price || '') : '';
    openModal('planModal');
  }
  $('addPlanBtn').addEventListener('click', () => openPlanModal());

  $('planSaveBtn').addEventListener('click', async () => {
    const id = $('planId').value;
    const body = {
      name: $('planName').value.trim(),
      quota_gb: num('planQuota'),
      days: num('planDays'),
      max_connections: num('planMaxConn'),
      max_requests: num('planMaxReq'),
      price: num('planPrice'),
    };
    if (!body.name) { PEYK.toast(PEYK.t('plans_name_required'), 'error'); return; }
    const btn = $('planSaveBtn');
    PEYK.setLoading(btn, true);
    try {
      if (id) await PEYK.api(`/api/plans/${id}`, { method: 'PATCH', body });
      else await PEYK.api('/api/plans', { method: 'POST', body });
      PEYK.toast(PEYK.t('settings_saved'), 'success');
      closeModal('planModal');
      loadPlans();
    } catch (e) {
      PEYK.toast(e.detail || 'error', 'error');
    } finally { PEYK.setLoading(btn, false); }
  });


  // ---------------- import from another panel ----------------
  /* Two steps on purpose. A migration is the one action an admin cannot undo
     by hand -- nobody unpicks a thousand wrong rows -- so nothing is written
     until they have seen what the file actually contains. */
  let importPayload = null;

  $('importPickBtn').addEventListener('click', () => $('importFile').click());

  $('importFile').addEventListener('change', async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    $('importPreview').style.display = 'none';
    importPayload = null;
    if (!file) return;

    let parsed;
    try {
      parsed = JSON.parse(await file.text());
    } catch (err) {
      PEYK.toast(PEYK.t('rs_err_invalid-json'), 'error');
      return;
    }

    const btn = $('importPickBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/import/preview', { method: 'POST', body: { data: parsed } });
      importPayload = parsed;
      renderImportPreview(r);
    } catch (err) {
      PEYK.toast(apiErrorText(err.detail), 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  function renderImportPreview(r) {
    $('importSource').textContent = r.source;
    $('importCount').textContent = r.importable;

    const skipped = $('importSkipped');
    skipped.style.display = r.skipped.length ? '' : 'none';
    skipped.textContent = PEYK.t('im_skipped').replace('{n}', r.skipped.length);

    const lapsed = $('importLapsed');
    lapsed.style.display = r.lapsed ? '' : 'none';
    lapsed.textContent = PEYK.t('im_lapsed').replace('{n}', r.lapsed);

    const over = $('importOverQuota');
    over.style.display = r.over_quota ? '' : 'none';
    over.textContent = PEYK.t('im_over_quota').replace('{n}', r.over_quota);

    const tbody = $('importSampleBody');
    tbody.innerHTML = '';
    const frag = document.createDocumentFragment();
    r.sample.forEach(row => {
      const tr = document.createElement('tr');
      const state = row.lapsed
        ? `<span class="pill pill-muted">${esc(PEYK.t(row.lapsed === 'quota' ? 'status_quota_over' : 'expired'))}</span>`
        : `<span class="pill pill-on"><span class="pill-dot"></span>${esc(PEYK.t('active'))}</span>`;
      tr.innerHTML = `
        <td><b>${esc(row.name)}</b></td>
        <td class="mono small">${row.quota_gb ? esc(row.quota_gb) + ' GB' : esc(PEYK.t('unlimited'))}</td>
        <td style="text-align:end;">${state}</td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
    $('importPreview').style.display = '';
    $('importConfirmBtn').disabled = !r.importable || !!r.over_quota;
  }

  $('importConfirmBtn').addEventListener('click', async () => {
    if (!importPayload) return;
    if (!confirm(PEYK.t('im_confirm_ask').replace('{n}', $('importCount').textContent))) return;
    const btn = $('importConfirmBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/import', { method: 'POST', body: { data: importPayload } });
      PEYK.toast(PEYK.t('im_done').replace('{n}', r.imported), 'success', 5000);
      $('importPreview').style.display = 'none';
      importPayload = null;
      loadInbounds();
    } catch (e) {
      PEYK.toast(apiErrorText(e.detail), 'error');
    } finally { PEYK.setLoading(btn, false); }
  });


  // ---------------- sales + vouchers ----------------
  /* Grouped thousands, no decimals: these are Toman figures, and a fractional
     Toman is not a thing anyone writes. */
  function fmtMoney(n) {
    return (Number(n) || 0).toLocaleString(PEYK.getLang() === 'fa' ? 'fa-IR' : 'en-US');
  }

  async function loadSales() {
    const days = $('salesPeriod').value || 30;
    try {
      const r = await PEYK.api(`/api/sales?days=${days}`);
      currency = r.currency || 'تومان';
      $('salesTotal').textContent = `${fmtMoney(r.total)} ${currency}`;
      $('salesCount').textContent = r.count;
      renderSalesTable('salesBySellerBody', r.by_seller,
        row => row.seller || PEYK.t('sl_me'));
      renderSalesTable('salesByPlanBody', r.by_plan, row => row.plan);
    } catch (e) { /* non-fatal */ }
  }

  function renderSalesTable(bodyId, rows, label) {
    const tbody = $(bodyId);
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="3" class="small muted" style="text-align:center; padding:20px;">${esc(PEYK.t('sl_nothing'))}</td></tr>`;
      return;
    }
    const frag = document.createDocumentFragment();
    rows.forEach(row => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td data-label="${esc(PEYK.t('sl_seller'))}"><b>${esc(label(row))}</b></td>
        <td data-label="${esc(PEYK.t('sl_count'))}" class="mono small">${esc(row.count)}</td>
        <td data-label="${esc(PEYK.t('sl_total'))}" class="mono small" style="text-align:end;">${esc(fmtMoney(row.total))}</td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }

  $('salesPeriod').addEventListener('change', loadSales);

  async function loadVouchers() {
    try {
      const r = await PEYK.api('/api/vouchers');
      vouchers = r.vouchers || [];
      renderVouchers();
    } catch (e) { /* non-fatal */ }
  }

  function renderVouchers() {
    const tbody = $('vouchersTableBody');
    if (!tbody) return;
    const unused = vouchers.filter(v => !v.used_at);
    $('voucherUnused').textContent = unused.length;
    $('voucherTotal').textContent = vouchers.length;
    $('vouchersEmpty').style.display = vouchers.length ? 'none' : 'block';
    tbody.innerHTML = '';
    const frag = document.createDocumentFragment();
    vouchers.forEach(v => {
      const used = !!v.used_at;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td data-label="${esc(PEYK.t('sl_code'))}"><b class="mono">${esc(v.code)}</b></td>
        <td data-label="${esc(PEYK.t('plans_name'))}" class="small">${esc(v.plan_name || '-')}${v.price ? ` <span class="muted mono">${esc(fmtMoney(v.price))}</span>` : ''}</td>
        <td data-label="${esc(PEYK.t('sl_state'))}">
          <span class="pill ${used ? 'pill-muted' : 'pill-on'}"><span class="pill-dot"></span>${esc(PEYK.t(used ? 'sl_used' : 'sl_unused'))}</span>
        </td>
        <td data-label="${esc(PEYK.t('sl_used_by'))}" class="small muted">${used ? esc(v.used_name || '-') : '—'}</td>
        <td data-label="${esc(PEYK.t('inb_actions'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-vc-action="copy" data-code="${esc(v.code)}" title="${esc(PEYK.t('copy'))}"><svg><use href="#icon-copy"/></svg></button>
            ${used ? '' : `<button class="icon-btn btn-sm" data-vc-action="delete" data-code="${esc(v.code)}" title="${esc(PEYK.t('delete'))}" style="color:var(--err)"><svg><use href="#icon-trash"/></svg></button>`}
          </div>
        </td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }

  $('vouchersTableBody').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-vc-action]');
    if (!btn) return;
    const code = btn.dataset.code;
    if (btn.dataset.vcAction === 'copy') {
      await copyText(code);
      return;
    }
    if (!confirm(PEYK.t('sl_delete_confirm'))) return;
    try {
      await PEYK.api(`/api/vouchers/${code}`, { method: 'DELETE' });
      PEYK.toast(PEYK.t('sl_deleted'), 'success');
      loadVouchers();
    } catch (err) { PEYK.toast(err.detail || 'error', 'error'); }
  });

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      PEYK.toast(PEYK.t('copied'), 'success');
    } catch (e) {
      PEYK.toast(PEYK.t('copy_failed'), 'error');
    }
  }

  $('copyUnusedBtn').addEventListener('click', () => {
    const unused = vouchers.filter(v => !v.used_at).map(v => v.code);
    if (!unused.length) { PEYK.toast(PEYK.t('sl_nothing_to_copy'), 'error'); return; }
    copyText(unused.join('\n'));
  });

  $('addVoucherBtn').addEventListener('click', () => {
    fillPlanSelect('voucherPlan');
    $('voucherCount').value = 10;
    $('voucherNote').value = '';
    $('voucherResult').style.display = 'none';
    openModal('voucherModal');
  });

  $('voucherSaveBtn').addEventListener('click', async () => {
    const planId = $('voucherPlan').value;
    if (!planId) { PEYK.toast(PEYK.t('sl_pick_plan'), 'error'); return; }
    const btn = $('voucherSaveBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/vouchers', {
        method: 'POST',
        body: {
          plan_id: planId,
          count: parseInt($('voucherCount').value, 10) || 1,
          note: $('voucherNote').value.trim(),
        },
      });
      const codes = r.vouchers.map(v => v.code).join('\n');
      $('voucherCodes').value = codes;
      $('voucherResult').style.display = '';
      PEYK.toast(PEYK.t('sl_minted').replace('{n}', r.vouchers.length), 'success');
      loadVouchers();
      loadSales();
    } catch (e) {
      PEYK.toast(apiErrorText(e.detail), 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  $('copyNewCodesBtn').addEventListener('click', () => copyText($('voucherCodes').value));


  // ---------------- anti-sharing ----------------
  $('saveSharingBtn').addEventListener('click', () => {
    saveSettings('saveSharingBtn', {
      sharing_detect_enabled: $('settingSharingEnabled').checked,
      sharing_window_hours: parseInt($('settingSharingWindow').value, 10) || 24,
      sharing_threshold: parseInt($('settingSharingThreshold').value, 10) || 4,
      sharing_auto_disable: $('settingSharingAutoDisable').checked,
    });
  });

  $('sharingCheckBtn').addEventListener('click', async () => {
    const btn = $('sharingCheckBtn'), el = $('sharingResult');
    PEYK.setLoading(btn, true);
    el.textContent = '';
    try {
      const r = await PEYK.api('/api/sharing/check', { method: 'POST' });
      if (!r.flagged.length) {
        el.innerHTML = `<span class="pill pill-on"><span class="pill-dot"></span>${esc(PEYK.t('shr_none'))}</span>`;
      } else {
        const names = r.flagged.map(f => `${esc(f.name)} (${f.networks})`).join('، ');
        el.innerHTML = `<span class="pill pill-off"><span class="pill-dot"></span>${esc(PEYK.t('shr_found').replace('{n}', r.flagged.length))}</span> <span class="muted">${names}</span>`;
      }
      loadInbounds();
    } catch (e) {
      el.textContent = e.detail === 'sharing-disabled' ? PEYK.t('shr_disabled') : (e.detail || 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  // ---------------- where a subscription was used ----------------
  let networksModalUid = null;

  async function showNetworks(uid) {
    networksModalUid = uid;
    const tbody = $('networksTableBody');
    tbody.innerHTML = '';
    $('networksSummary').textContent = PEYK.t('loading');
    openModal('networksModal');
    try {
      const r = await PEYK.api(`/api/inbounds/${uid}/networks`);
      if (!r.recent.length) {
        $('networksSummary').textContent = PEYK.t('shr_never_used');
        return;
      }
      $('networksSummary').innerHTML = PEYK.t('shr_summary')
        .replace('{nets}', `<b>${r.networks}</b>`)
        .replace('{ips}', `<b>${r.ips}</b>`)
        .replace('{hours}', `<b>${r.window_hours}</b>`)
        .replace('{limit}', `<b>${r.threshold}</b>`);
      const frag = document.createDocumentFragment();
      r.recent.forEach(e => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td data-label="${esc(PEYK.t('shr_ip'))}" class="mono small">${esc(e.ip)}</td>
          <td data-label="${esc(PEYK.t('shr_network'))}" class="mono small muted">${esc(e.network)}</td>
          <td data-label="${esc(PEYK.t('shr_last'))}" class="small muted" style="text-align:end;">${esc(PEYK.fmtAgo(e.last))}</td>`;
        frag.appendChild(tr);
      });
      tbody.appendChild(frag);
    } catch (e) {
      $('networksSummary').textContent = e.detail || 'error';
    }
  }

  $('clearFlagBtn').addEventListener('click', async () => {
    if (!networksModalUid) return;
    if (!confirm(PEYK.t('shr_clear_confirm'))) return;
    const btn = $('clearFlagBtn');
    PEYK.setLoading(btn, true);
    try {
      await PEYK.api(`/api/inbounds/${networksModalUid}/clear-flag`, { method: 'POST' });
      PEYK.toast(PEYK.t('shr_cleared'), 'success');
      closeModal('networksModal');
      loadInbounds();
    } catch (e) {
      PEYK.toast(e.detail || 'error', 'error');
    } finally { PEYK.setLoading(btn, false); }
  });


  // ---------------- resellers ----------------
  async function loadResellers() {
    if (!isOwner) return;
    try {
      const r = await PEYK.api('/api/resellers');
      resellers = r.resellers || [];
      renderResellers();
      $('navResellerCount').textContent = resellers.length;
    } catch (e) { /* non-fatal */ }
  }

  function renderResellers() {
    const tbody = $('resellersTableBody');
    if (!tbody) return;
    $('resellersEmpty').style.display = resellers.length ? 'none' : 'block';
    tbody.innerHTML = '';
    const frag = document.createDocumentFragment();
    const unlimited = PEYK.t('unlimited');
    resellers.forEach(r => {
      const on = r.enabled !== false;
      const users = r.max_users ? `${r.users} / ${r.max_users}` : `${r.users} / ${unlimited}`;
      const traffic = r.max_traffic_gb
        ? `${r.used_gb} / ${r.max_traffic_gb} GB`
        : `${r.used_gb} GB`;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td data-label="${esc(PEYK.t('rs_username'))}"><b class="mono">${esc(r.username)}</b></td>
        <td data-label="${esc(PEYK.t('rs_status'))}">
          <span class="pill ${on ? 'pill-on' : 'pill-off'}"><span class="pill-dot"></span>${esc(PEYK.t(on ? 'active' : 'inactive'))}</span>
        </td>
        <td data-label="${esc(PEYK.t('rs_users'))}" class="mono small">${esc(users)}</td>
        <td data-label="${esc(PEYK.t('rs_traffic'))}" class="mono small">${esc(traffic)}</td>
        <td data-label="${esc(PEYK.t('rs_note'))}" class="small muted">${esc(r.note || '-')}</td>
        <td data-label="${esc(PEYK.t('inb_actions'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-rs-action="toggle" data-id="${esc(r.id)}" title="${esc(PEYK.t(on ? 'bulk_disable' : 'bulk_enable'))}"><svg><use href="#icon-power"/></svg></button>
            <button class="icon-btn btn-sm" data-rs-action="edit" data-id="${esc(r.id)}" title="${esc(PEYK.t('edit'))}"><svg><use href="#icon-edit"/></svg></button>
            <button class="icon-btn btn-sm" data-rs-action="delete" data-id="${esc(r.id)}" title="${esc(PEYK.t('delete'))}" style="color:var(--err)"><svg><use href="#icon-trash"/></svg></button>
          </div>
        </td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }

  $('resellersTableBody').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-rs-action]');
    if (!btn) return;
    const rs = resellers.find(r => r.id === btn.dataset.id);
    if (!rs) return;
    const action = btn.dataset.rsAction;

    if (action === 'edit') return openResellerModal(rs);

    if (action === 'toggle') {
      try {
        await PEYK.api(`/api/resellers/${rs.id}`, {
          method: 'PATCH', body: { enabled: rs.enabled === false },
        });
        PEYK.toast(PEYK.t('settings_saved'), 'success');
        loadResellers();
      } catch (err) { PEYK.toast(err.detail || 'error', 'error'); }
      return;
    }

    // Deleting the account is the reversible half; deleting the customers it
    // sold to is not, so that is a second, explicit answer.
    if (!confirm(PEYK.t('rs_delete_confirm').replace('{name}', rs.username))) return;
    let dropUsers = false;
    if (rs.users > 0) {
      dropUsers = confirm(PEYK.t('rs_delete_users_confirm').replace('{n}', rs.users));
    }
    try {
      await PEYK.api(`/api/resellers/${rs.id}?delete_users=${dropUsers ? 1 : 0}`, { method: 'DELETE' });
      PEYK.toast(PEYK.t('rs_deleted'), 'success');
      loadResellers();
      loadInbounds();
    } catch (err) { PEYK.toast(err.detail || 'error', 'error'); }
  });

  function openResellerModal(rs = null) {
    $('resellerModalTitle').textContent = rs ? PEYK.t('rs_edit') : PEYK.t('rs_add');
    $('resellerId').value = rs ? rs.id : '';
    $('resellerUsername').value = rs ? rs.username : '';
    $('resellerUsername').disabled = !!rs;
    $('resellerPassword').value = '';
    $('resellerPasswordLabel').textContent = PEYK.t(rs ? 'rs_password_change' : 'rs_password');
    $('resellerMaxUsers').value = rs ? (rs.max_users || '') : '';
    $('resellerMaxTraffic').value = rs ? (rs.max_traffic_gb || '') : '';
    $('resellerNote').value = rs ? (rs.note || '') : '';
    $('resellerEnabled').checked = rs ? rs.enabled !== false : true;
    openModal('resellerModal');
  }
  $('addResellerBtn').addEventListener('click', () => openResellerModal());

  $('resellerSaveBtn').addEventListener('click', async () => {
    const id = $('resellerId').value;
    const password = $('resellerPassword').value;
    const body = {
      max_users: num('resellerMaxUsers'),
      max_traffic_gb: num('resellerMaxTraffic'),
      note: $('resellerNote').value.trim(),
      enabled: $('resellerEnabled').checked,
    };
    if (!id) {
      body.username = $('resellerUsername').value.trim();
      if (!/^[a-zA-Z0-9_]{3,32}$/.test(body.username)) {
        PEYK.toast(PEYK.t('rs_bad_username'), 'error'); return;
      }
    }
    if (password) body.password = password;
    if (!id && password.length < 8) { PEYK.toast(PEYK.t('rs_weak_password'), 'error'); return; }

    const btn = $('resellerSaveBtn');
    PEYK.setLoading(btn, true);
    try {
      if (id) await PEYK.api(`/api/resellers/${id}`, { method: 'PATCH', body });
      else await PEYK.api('/api/resellers', { method: 'POST', body });
      PEYK.toast(PEYK.t('settings_saved'), 'success');
      closeModal('resellerModal');
      loadResellers();
    } catch (e) {
      PEYK.toast(apiErrorText(e.detail), 'error');
    } finally { PEYK.setLoading(btn, false); }
  });


  // ---------------- link rotation ----------------
  let linksModalUid = null;

  $('rotateLinkBtn').addEventListener('click', async () => {
    if (!linksModalUid) return;
    if (!confirm(PEYK.t('rot_confirm'))) return;
    const btn = $('rotateLinkBtn');
    PEYK.setLoading(btn, true);
    try {
      await PEYK.api(`/api/inbounds/${linksModalUid}/rotate-link`, { method: 'POST' });
      PEYK.toast(PEYK.t('rot_done'), 'success', 5000);
      await loadInbounds();
      showLinks(linksModalUid);
    } catch (e) {
      PEYK.toast(e.detail || 'error', 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  $('saveHealthBtn').addEventListener('click', () => {
    saveSettings('saveHealthBtn', {
      health_check_enabled: $('settingHealthEnabled').checked,
      health_interval_minutes: parseInt($('settingHealthInterval').value, 10) || 15,
      health_fail_threshold: parseInt($('settingHealthThreshold').value, 10) || 3,
      health_auto_disable: $('settingHealthAutoDisable').checked,
    });
  });

  // ---------------- endpoints ----------------
  async function loadEndpoints() {
    try {
      const r = await PEYK.api('/api/endpoints');
      endpoints = r.endpoints || [];
      renderEndpoints();
      $('navEndpointCount').textContent = endpoints.filter(e => e.enabled !== false).length;
    } catch (e) { /* non-fatal */ }
  }

  function healthCell(ep) {
    const h = ep.health || {};
    if (h.ok === null || h.ok === undefined) {
      return `<span class="pill pill-muted">${esc(PEYK.t('ep_untested'))}</span>`;
    }
    if (!h.ok) return `<span class="pill pill-off"><span class="pill-dot"></span>${esc(PEYK.t('ep_down'))}</span>`;
    const ms = h.latency_ms;
    const cls = ms == null ? 'pill-on' : (ms < 300 ? 'pill-on' : ms < 900 ? 'pill-warn' : 'pill-off');
    return `<span class="pill ${cls}"><span class="pill-dot"></span>${esc(ms != null ? ms + ' ms' : PEYK.t('ep_up'))}</span>`;
  }

  function renderEndpoints() {
    const tbody = $('endpointsTableBody');
    if (!tbody) return;
    $('endpointsEmpty').style.display = endpoints.length ? 'none' : 'block';
    tbody.innerHTML = '';
    const frag = document.createDocumentFragment();
    [...endpoints]
      .sort((a, b) => (a.sort || 0) - (b.sort || 0) || (a.name || '').localeCompare(b.name || ''))
      .forEach(ep => {
        const tr = document.createElement('tr');
        const on = ep.enabled !== false;
        tr.innerHTML = `
          <td data-label="${esc(PEYK.t('ep_name'))}"><b>${esc(ep.name || ep.address)}</b></td>
          <td data-label="${esc(PEYK.t('ep_address'))}" class="mono small">${esc(ep.address)}:${esc(ep.port || 443)}</td>
          <td data-label="${esc(PEYK.t('ep_host'))}" class="mono small">${esc(ep.host || '—')}${ep.sni ? ' / ' + esc(ep.sni) : ''}</td>
          <td data-label="${esc(PEYK.t('ep_health'))}">${healthCell(ep)}</td>
          <td data-label="${esc(PEYK.t('inb_status'))}"><span class="pill ${on ? 'pill-on' : 'pill-muted'}"><span class="pill-dot"></span>${esc(PEYK.t(on ? 'active' : 'inactive'))}</span></td>
          <td data-label="${esc(PEYK.t('inb_actions'))}">
            <div class="row-actions">
              <button class="icon-btn btn-sm" data-ep-action="test" data-id="${esc(ep.id)}" title="${esc(PEYK.t('ep_test'))}"><svg><use href="#icon-refresh"/></svg></button>
              <button class="icon-btn btn-sm" data-ep-action="edit" data-id="${esc(ep.id)}" title="${esc(PEYK.t('edit'))}"><svg><use href="#icon-edit"/></svg></button>
              <button class="icon-btn btn-sm" data-ep-action="delete" data-id="${esc(ep.id)}" title="${esc(PEYK.t('delete'))}" style="color:var(--err)"><svg><use href="#icon-trash"/></svg></button>
            </div>
          </td>`;
        frag.appendChild(tr);
      });
    tbody.appendChild(frag);
  }

  function openEndpointModal(ep = null) {
    $('endpointModalTitle').textContent = ep ? PEYK.t('edit') : PEYK.t('ep_add');
    $('epId').value = ep ? ep.id : '';
    $('epName').value = ep ? (ep.name || '') : '';
    $('epAddress').value = ep ? ep.address : '';
    $('epPort').value = ep ? (ep.port || 443) : 443;
    $('epHost').value = ep ? (ep.host || '') : '';
    $('epSni').value = ep ? (ep.sni || '') : '';
    $('epFp').value = ep ? (ep.fp || '') : '';
    $('epSort').value = ep ? (ep.sort || 0) : endpoints.length;
    $('epEnabled').checked = ep ? ep.enabled !== false : true;
    openModal('endpointModal');
  }
  $('addEndpointBtn').addEventListener('click', () => openEndpointModal());

  $('epSaveBtn').addEventListener('click', async () => {
    const id = $('epId').value;
    const body = {
      name: $('epName').value.trim(),
      address: $('epAddress').value.trim(),
      port: parseInt($('epPort').value, 10) || 443,
      host: $('epHost').value.trim(),
      sni: $('epSni').value.trim(),
      fp: $('epFp').value,
      sort: parseInt($('epSort').value, 10) || 0,
      enabled: $('epEnabled').checked,
    };
    if (!body.address) { PEYK.toast(PEYK.t('ep_address_required'), 'error'); return; }
    const btn = $('epSaveBtn');
    PEYK.setLoading(btn, true);
    try {
      if (id) await PEYK.api(`/api/endpoints/${id}`, { method: 'PATCH', body });
      else await PEYK.api('/api/endpoints', { method: 'POST', body });
      PEYK.toast(PEYK.t('settings_saved'), 'success');
      closeModal('endpointModal');
      loadEndpoints();
    } catch (e) {
      const map = { 'invalid-address': 'ep_bad_address', 'endpoint-limit-reached': 'ep_limit' };
      PEYK.toast(map[e.detail] ? PEYK.t(map[e.detail]) : (e.detail || 'error'), 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  async function testEndpoint(id, silent) {
    try {
      const r = await PEYK.api(`/api/endpoints/${id}/test`, { method: 'POST' });
      const ep = endpoints.find(e => e.id === id);
      if (ep) ep.health = r.health;
      renderEndpoints();
      if (!silent) {
        PEYK.toast(
          r.ok ? `${PEYK.t('ep_up')} — ${r.latency_ms} ms` : `${PEYK.t('ep_down')} — ${r.detail}`,
          r.ok ? 'success' : 'error');
      }
      return r.ok;
    } catch (e) {
      if (!silent) PEYK.toast(e.detail || 'error', 'error');
      return false;
    }
  }

  $('endpointsTableBody').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-ep-action]');
    if (!btn) return;
    const ep = endpoints.find(x => x.id === btn.dataset.id);
    if (!ep) return;
    const action = btn.dataset.epAction;
    if (action === 'edit') return openEndpointModal(ep);
    if (action === 'test') {
      PEYK.setLoading(btn, true);
      await testEndpoint(ep.id, false);
      PEYK.setLoading(btn, false);
      return;
    }
    if (!confirm(PEYK.t('ep_delete_confirm'))) return;
    try {
      await PEYK.api(`/api/endpoints/${ep.id}`, { method: 'DELETE' });
      PEYK.toast(PEYK.t('ep_deleted'), 'success');
      loadEndpoints();
    } catch (err) { PEYK.toast(err.detail || 'error', 'error'); }
  });

  $('testAllEndpointsBtn').addEventListener('click', async () => {
    const btn = $('testAllEndpointsBtn');
    PEYK.setLoading(btn, true);
    // Sequential on purpose: a burst of parallel probes from one box skews
    // the latency numbers it is trying to measure.
    let up = 0;
    for (const ep of endpoints) if (await testEndpoint(ep.id, true)) up++;
    PEYK.setLoading(btn, false);
    PEYK.toast(PEYK.t('ep_test_result').replace('{up}', up).replace('{total}', endpoints.length),
                 up === endpoints.length ? 'success' : 'info');
    loadEndpoints();
  });

  // ---------------- traffic table ----------------
  function renderTrafficTable() {
    const tbody = $('trafficTableBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    const frag = document.createDocumentFragment();
    inbounds.forEach(ib => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td data-label="${esc(PEYK.t('inb_name'))}"><b>${esc(ib.name)}</b></td>
        <td data-label="${esc(PEYK.t('dash_upload'))}" class="mono small">${esc(PEYK.fmtBytes(ib.used_up || 0))}</td>
        <td data-label="${esc(PEYK.t('dash_download'))}" class="mono small">${esc(PEYK.fmtBytes(ib.used_down || 0))}</td>
        <td data-label="${esc(PEYK.t('inb_usage'))}" class="mono small">${esc(PEYK.fmtBytes((ib.used_up || 0) + (ib.used_down || 0)))}</td>
        <td data-label="${esc(PEYK.t('traffic_history'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-action="history" data-uid="${esc(ib.uid)}" title="${esc(PEYK.t('traffic_history'))}"><svg><use href="#icon-chart"/></svg></button>
          </div>
        </td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }
  $('trafficTableBody').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action="history"]');
    if (btn) showHistory(btn.dataset.uid);
  });

  // ---------------- row actions ----------------
  async function handleAction(action, uid) {
    if (action === 'networks') return showNetworks(uid);
    const ib = inbounds.find(x => x.uid === uid);
    if (!ib) return;
    if (action === 'edit') return openInboundModal(ib);
    if (action === 'renew') return openRenewModal(ib);
    if (action === 'links') return showLinks(uid);
    if (action === 'history') return showHistory(uid);

    const jobs = {
      reset: { path: `/api/inbounds/${uid}/reset-usage`, method: 'POST', ok: 'inb_reset_done' },
      regen: { path: `/api/inbounds/${uid}/regenerate`, method: 'POST', ok: 'inb_regenerated', confirm: 'inb_regenerate_confirm' },
      delete: { path: `/api/inbounds/${uid}`, method: 'DELETE', ok: 'inb_deleted', confirm: 'inb_delete_confirm' },
    };
    const job = jobs[action];
    if (!job) return;
    if (job.confirm && !confirm(PEYK.t(job.confirm))) return;
    try {
      await PEYK.api(job.path, { method: job.method });
      PEYK.toast(PEYK.t(job.ok), 'success');
      loadInbounds();
    } catch (e) { PEYK.toast(e.detail || 'error', 'error'); }
  }

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
    if (!uid || !days || days < 1) { PEYK.toast(PEYK.t('inb_invalid_number'), 'error'); return; }
    const btn = $('renewSaveBtn');
    PEYK.setLoading(btn, true);
    try {
      await PEYK.api(`/api/inbounds/${uid}/renew`, {
        method: 'POST', body: { days, reset_usage: $('renewResetUsage').checked },
      });
      PEYK.toast(PEYK.t('inb_renewed'), 'success');
      closeModal('renewModal');
      loadInbounds();
    } catch (e) {
      PEYK.toast(e.detail || 'error', 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  // ---------------- links ----------------
  async function showLinks(uid) {
    linksModalUid = uid;
    try {
      const r = await PEYK.api(`/api/inbounds/${uid}/links`);
      const configs = (r.links && r.links.configs) || [];
      $('linkTls').textContent = r.links.tls || '';
      const multi = $('multiConfigBlock');
      if (configs.length > 1) {
        multi.style.display = '';
        $('multiConfigList').innerHTML = configs.map(c =>
          `<div class="link-row" style="margin-bottom:6px;"><code>${esc(c.link)}</code></div>`).join('');
        $('multiConfigCount').textContent = configs.length;
      } else {
        multi.style.display = 'none';
      }
      $('linkSub').textContent = r.sub_url || '';
      $('linkStatus').textContent = r.status_url || '';
      $('linkSubJson').textContent = r.sub_json_url || '';
      $('linkSubClash').textContent = r.sub_url ? `${r.sub_url}?format=clash` : '';
      $('linkSubSingbox').textContent = r.sub_url ? `${r.sub_url}?format=singbox` : '';
      $('qrImg').src = `/api/inbounds/${uid}/qr?t=${Date.now()}`;
      const ib = inbounds.find(x => x.uid === uid);
      const ips = (ib && ib.active_ips) || [];
      $('activeIpsBlock').style.display = ips.length ? '' : 'none';
      $('activeIpsList').textContent = ips.join(', ');
      openModal('linksModal');
    } catch (e) { PEYK.toast(e.detail || 'error', 'error'); }
  }

  // ---------------- per-user history ----------------
  async function showHistory(uid) {
    try {
      const r = await PEYK.api(`/api/inbounds/${uid}/history`);
      $('historyName').textContent = r.name || '';
      const series = (r.history || []).map(h => ({
        t: Date.parse(h.d + 'T00:00:00Z') / 1000, up: h.up, down: h.down,
      }));
      const totalUp = series.reduce((a, b) => a + b.up, 0);
      const totalDown = series.reduce((a, b) => a + b.down, 0);
      const active = series.filter(s => s.up + s.down > 0).length;
      $('historySummary').textContent =
        `${PEYK.t('dash_upload')}: ${PEYK.fmtBytes(totalUp)} · ` +
        `${PEYK.t('dash_download')}: ${PEYK.fmtBytes(totalDown)} · ` +
        `${PEYK.t('history_active_days').replace('{n}', active)}`;
      openModal('historyModal');
      // Canvas needs a laid-out box before it can size itself.
      requestAnimationFrame(() => renderTrafficChart($('historyChart'), series, { unit: 'day' }));
    } catch (e) { PEYK.toast(e.detail || 'error', 'error'); }
  }

  // ---------------- copy ----------------
  async function copyText(text) {
    if (!text) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      PEYK.toast(PEYK.t('copied'), 'success', 1500);
      PEYK.playSfx('click', 0.35);
    } catch (e) { PEYK.toast('error', 'error'); }
  }
  document.querySelectorAll('[data-copy]').forEach(btn => {
    btn.addEventListener('click', () => {
      const el = $(btn.dataset.copy);
      if (el) copyText(el.textContent);
    });
  });

  // ---------------- security: password ----------------
  $('securityForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const np = $('newPassword').value;
    if (np && np !== $('newPassword2').value) {
      PEYK.toast(PEYK.t('setup_mismatch'), 'error');
      PEYK.shake($('securityForm'));
      return;
    }
    const btn = $('securityBtn');
    PEYK.setLoading(btn, true);
    try {
      await PEYK.api('/api/change-password', {
        method: 'POST',
        body: {
          old_password: $('oldPassword').value,
          new_username: $('newUsername').value.trim(),
          new_password: np,
        },
      });
      PEYK.toast(PEYK.t('sec_updated'), 'success');
      $('securityForm').reset();
    } catch (e2) {
      let msg = e2.detail;
      if (msg === 'wrong-old-password') msg = PEYK.t('sec_wrong_old');
      else if (msg === 'weak-password') msg = PEYK.t('setup_password_hint');
      else if (msg === 'invalid-username') msg = PEYK.t('setup_username_hint');
      else if (msg === 'nothing-to-change') msg = PEYK.t('sec_nothing_to_change');
      PEYK.toast(msg || 'error', 'error');
      PEYK.shake($('securityForm'));
    } finally { PEYK.setLoading(btn, false); }
  });

  // ---------------- security: 2FA ----------------
  async function loadTwofaStatus() {
    try {
      const r = await PEYK.api('/api/2fa/status');
      const on = !!r.enabled;
      const badge = $('twofaBadge');
      badge.textContent = PEYK.t(on ? 'twofa_on' : 'twofa_off');
      badge.className = 'pill ' + (on ? 'pill-on' : 'pill-muted');
      $('twofaOffBlock').style.display = on ? 'none' : '';
      $('twofaOnBlock').style.display = on ? '' : 'none';
      $('twofaRecoveryLeft').textContent = r.recovery_remaining || 0;
    } catch (e) { /* non-fatal */ }
  }

  $('twofaSetupBtn').addEventListener('click', async () => {
    const btn = $('twofaSetupBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/2fa/setup', { method: 'POST' });
      $('twofaSecret').textContent = r.secret;
      $('twofaQr').src = `/api/2fa/qr?t=${Date.now()}`;
      $('twofaStep1').style.display = '';
      $('twofaStep2').style.display = 'none';
      $('twofaConfirmBtn').style.display = '';
      $('twofaCode').value = '';
      openModal('twofaModal');
    } catch (e) {
      PEYK.toast(e.detail || 'error', 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  $('twofaConfirmBtn').addEventListener('click', async () => {
    const code = $('twofaCode').value.trim();
    if (!/^\d{6}$/.test(code)) { PEYK.toast(PEYK.t('twofa_invalid'), 'error'); return; }
    const btn = $('twofaConfirmBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/2fa/enable', { method: 'POST', body: { code } });
      showRecoveryCodes(r.recovery_codes || []);
      PEYK.toast(PEYK.t('twofa_enabled'), 'success');
      loadTwofaStatus();
    } catch (e) {
      PEYK.toast(e.detail === 'invalid-code' ? PEYK.t('twofa_invalid') : (e.detail || 'error'), 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  function showRecoveryCodes(codes) {
    const box = $('twofaRecoveryCodes');
    box.innerHTML = '';
    codes.forEach(c => {
      const el = document.createElement('span');
      el.textContent = c;
      box.appendChild(el);
    });
    $('twofaStep1').style.display = 'none';
    $('twofaStep2').style.display = '';
    $('twofaConfirmBtn').style.display = 'none';
    openModal('twofaModal');
  }

  $('twofaCopyCodes').addEventListener('click', () => {
    const codes = [...$('twofaRecoveryCodes').querySelectorAll('span')].map(s => s.textContent);
    copyText(codes.join('\n'));
  });

  // A single password-confirm modal serves every action that needs re-auth.
  let pendingConfirm = null;
  function askPassword(titleKey, fn) {
    pendingConfirm = fn;
    $('confirmPassTitle').textContent = PEYK.t(titleKey);
    $('confirmPassInput').value = '';
    openModal('confirmPassModal');
    setTimeout(() => $('confirmPassInput').focus(), 60);
  }
  $('confirmPassBtn').addEventListener('click', async () => {
    const password = $('confirmPassInput').value;
    if (!password || !pendingConfirm) return;
    const btn = $('confirmPassBtn');
    PEYK.setLoading(btn, true);
    try {
      await pendingConfirm(password);
      closeModal('confirmPassModal');
    } catch (e) {
      PEYK.toast(e.detail === 'wrong-password' ? PEYK.t('sec_wrong_old') : (e.detail || 'error'), 'error');
      PEYK.shake($('confirmPassInput'));
    } finally { PEYK.setLoading(btn, false); }
  });
  $('confirmPassInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('confirmPassBtn').click(); }
  });

  $('twofaDisableBtn').addEventListener('click', () => {
    askPassword('twofa_disable', async (password) => {
      await PEYK.api('/api/2fa/disable', { method: 'POST', body: { password } });
      PEYK.toast(PEYK.t('twofa_disabled'), 'success');
      loadTwofaStatus();
    });
  });

  $('twofaRegenBtn').addEventListener('click', () => {
    askPassword('twofa_regen', async (password) => {
      const r = await PEYK.api('/api/2fa/recovery-codes', { method: 'POST', body: { password } });
      showRecoveryCodes(r.recovery_codes || []);
      loadTwofaStatus();
    });
  });

  // ---------------- security: login log ----------------
  async function loadLoginLog() {
    try {
      const r = await PEYK.api('/api/login-log');
      const tbody = $('loginLogBody');
      const rows = r.entries || [];
      $('loginLogEmpty').style.display = rows.length ? 'none' : 'block';
      tbody.innerHTML = '';
      const frag = document.createDocumentFragment();
      rows.forEach(e => {
        const tr = document.createElement('tr');
        const when = new Date(e.ts * 1000).toLocaleString(
          PEYK.getLang() === 'fa' ? 'fa-IR' : 'en-US');
        const pill = e.ok
          ? `<span class="pill pill-on"><span class="pill-dot"></span>${esc(PEYK.t('loginlog_ok'))}</span>`
          : `<span class="pill pill-off"><span class="pill-dot"></span>${esc(PEYK.t('loginlog_fail'))}</span>`;
        tr.innerHTML = `
          <td data-label="${esc(PEYK.t('loginlog_time'))}" class="small">${esc(when)}</td>
          <td data-label="${esc(PEYK.t('loginlog_ip'))}" class="mono small">${esc(e.ip)}</td>
          <td data-label="${esc(PEYK.t('loginlog_method'))}" class="mono small">${esc(e.method)}</td>
          <td data-label="${esc(PEYK.t('loginlog_result'))}">${pill}</td>`;
        frag.appendChild(tr);
      });
      tbody.appendChild(frag);
    } catch (e) { /* non-fatal */ }
  }
  $('loginLogRefresh').addEventListener('click', loadLoginLog);

  // ---------------- settings ----------------
  async function saveSettings(btnId, payload, after) {
    const btn = $(btnId);
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/settings', { method: 'POST', body: payload });
      PEYK.toast(PEYK.t('settings_saved'), 'success');
      if (after) after(r);
    } catch (e) {
      let msg = e.detail || 'error';
      if (msg === 'invalid-bot-token') msg = PEYK.t('notify_bad_token');
      if (msg === 'invalid-ota-repo') msg = PEYK.t('ota_bad_repo');
      PEYK.toast(msg, 'error');
    } finally { PEYK.setLoading(btn, false); }
  }

  $('saveSettingsBtn').addEventListener('click', () => {
    PEYK.setSoundEnabled($('settingSound').checked);
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
    }, () => setTimeout(() => window.location.reload(), 600));
  });

  $('saveRelayBtn').addEventListener('click', () => {
    saveSettings('saveRelayBtn', {
      allow_private_destinations: $('settingAllowPrivate').checked,
      idle_timeout_seconds: parseInt($('settingIdleTimeout').value, 10) || 0,
      history_days: parseInt($('settingHistoryDays').value, 10) || 30,
    });
  });

  $('saveNotifyBtn').addEventListener('click', () => {
    saveSettings('saveNotifyBtn', {
      telegram_bot_token: $('settingBotToken').value.trim(),
      telegram_chat_id: $('settingChatId').value.trim(),
    });
  });

  $('saveNotifyRulesBtn').addEventListener('click', () => {
    saveSettings('saveNotifyRulesBtn', {
      notify_quota_enabled: $('settingNotifyQuota').checked,
      notify_quota_percent: parseInt($('settingNotifyPercent').value, 10) || 80,
      notify_expiry_enabled: $('settingNotifyExpiry').checked,
      notify_expiry_days: parseInt($('settingNotifyDays').value, 10) || 3,
    });
  });

  $('testNotifyBtn').addEventListener('click', async () => {
    const btn = $('testNotifyBtn');
    PEYK.setLoading(btn, true);
    try {
      await PEYK.api('/api/notify/test', { method: 'POST' });
      PEYK.toast(PEYK.t('notify_test_sent'), 'success');
    } catch (e) {
      const msg = e.detail === 'not-configured' ? PEYK.t('notify_not_configured') : (e.detail || 'error');
      PEYK.toast(msg, 'error', 7000);
    } finally { PEYK.setLoading(btn, false); }
  });

  function applyFragmentFieldState() {
    const on = $('settingFragmentEnabled').checked;
    const f = $('fragmentFields');
    f.style.opacity = on ? '1' : '.45';
    f.style.pointerEvents = on ? 'auto' : 'none';
  }
  $('settingFragmentEnabled').addEventListener('change', applyFragmentFieldState);

  // ---------------- user bot ----------------
  async function loadUserbotStatus() {
    try {
      const r = await PEYK.api('/api/userbot');
      const badge = $('ubBadge');
      const on = r.enabled && r.configured;
      badge.textContent = PEYK.t(on ? 'ub_on' : 'ub_off');
      badge.className = 'pill ' + (on ? 'pill-on' : 'pill-muted');
      $('ubBound').textContent =
        PEYK.t('ub_bound_users').replace('{n}', r.bound_users || 0);
      // The bot hands out subscription links, which need a public domain.
      $('ubDomainWarning').style.display =
        (r.configured && !r.public_domain_set) ? '' : 'none';
    } catch (e) { /* non-fatal */ }
  }

  $('saveUserbotBtn').addEventListener('click', () => {
    saveSettings('saveUserbotBtn', {
      userbot_token: $('settingUserbotToken').value.trim(),
      userbot_enabled: $('settingUserbotEnabled').checked,
    }, loadUserbotStatus);
  });

  $('testUserbotBtn').addEventListener('click', async () => {
    const btn = $('testUserbotBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/userbot/test', { method: 'POST' });
      PEYK.toast(PEYK.t('ub_connected').replace('{name}', '@' + (r.username || '')), 'success', 6000);
    } catch (e) {
      const msg = e.detail === 'not-configured' ? PEYK.t('ub_no_token') : (e.detail || 'error');
      PEYK.toast(msg, 'error', 7000);
    } finally { PEYK.setLoading(btn, false); }
  });

  // ---------------- fragment profiles ----------------
  async function loadFragmentProfiles(current) {
    try {
      const r = await PEYK.api('/api/fragment-profiles');
      const sel = $('settingFragmentProfile');
      sel.innerHTML = '';
      const lang = PEYK.getLang();
      r.profiles.forEach(p => {
        const o = document.createElement('option');
        o.value = p.id;
        o.textContent = lang === 'fa' ? p.label_fa : p.label_en;
        o.dataset.note = lang === 'fa' ? p.note_fa : p.note_en;
        sel.appendChild(o);
      });
      sel.value = current || r.current;
      applyFragmentProfileState();
    } catch (e) { /* non-fatal */ }
  }

  function applyFragmentProfileState() {
    const sel = $('settingFragmentProfile');
    const opt = sel.options[sel.selectedIndex];
    $('fragmentProfileNote').textContent = opt ? (opt.dataset.note || '') : '';
    // The three raw fields only mean anything on the custom profile.
    const custom = sel.value === 'custom';
    const fields = $('fragmentFields');
    fields.style.opacity = custom ? '1' : '.45';
    fields.style.pointerEvents = custom ? 'auto' : 'none';
  }
  $('settingFragmentProfile').addEventListener('change', applyFragmentProfileState);

  // ---------------- trial accounts ----------------
  $('trialBtn').addEventListener('click', async () => {
    const btn = $('trialBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/inbounds/trial', { method: 'POST' });
      PEYK.toast(PEYK.t('trial_created').replace('{name}', r.inbound.name), 'success');
      await loadInbounds();
      showLinks(r.inbound.uid);
    } catch (e) {
      const msg = e.detail === 'trial-disabled' ? PEYK.t('trial_disabled') : apiErrorText(e.detail);
      PEYK.toast(msg, 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  $('saveTrialBtn').addEventListener('click', () => {
    saveSettings('saveTrialBtn', {
      trial_enabled: $('settingTrialEnabled').checked,
      trial_gb: Number($('settingTrialGb').value) || 0,
      trial_days: parseInt($('settingTrialDays').value, 10) || 1,
      trial_prefix: $('settingTrialPrefix').value.trim() || 'trial',
    });
  });

  // ---------------- retention ----------------
  async function loadCleanupStatus() {
    try {
      const r = await PEYK.api('/api/cleanup');
      const badge = $('cleanupBadge');
      badge.textContent = PEYK.t(r.enabled ? 'cleanup_on' : 'cleanup_off');
      badge.className = 'pill ' + (r.enabled ? 'pill-on' : 'pill-muted');
      $('cleanupLast').textContent = r.last
        ? `${PEYK.t('cleanup_last')}: ` +
          new Date(r.last.ts * 1000).toLocaleString(PEYK.getLang() === 'fa' ? 'fa-IR' : 'en-US')
        : PEYK.t('cleanup_never_run');
      renderCleanupPreview(r);
    } catch (e) { /* non-fatal */ }
  }

  function renderCleanupPreview(r) {
    const box = $('cleanupPreview');
    const dis = r.would_disable || [];
    const del = r.would_delete || [];
    if (!dis.length && !del.length) { box.style.display = 'none'; return; }
    box.style.display = '';
    const names = list => list.slice(0, 6).map(u => esc(u.name || u.uid)).join('، ')
      + (list.length > 6 ? ` … +${list.length - 6}` : '');
    const parts = [];
    if (dis.length) parts.push(`<b>${dis.length}</b> ${esc(PEYK.t('cleanup_would_disable'))}: ${names(dis)}`);
    if (del.length) parts.push(`<b>${del.length}</b> ${esc(PEYK.t('cleanup_would_delete'))}: ${names(del)}`);
    box.innerHTML = parts.join('<br>');
  }

  $('saveCleanupBtn').addEventListener('click', () => {
    saveSettings('saveCleanupBtn', {
      cleanup_enabled: $('settingCleanup').checked,
      cleanup_disable_days: parseInt($('settingCleanupDisable').value, 10) || 0,
      cleanup_delete_days: parseInt($('settingCleanupDelete').value, 10) || 0,
    }, loadCleanupStatus);
  });

  $('cleanupPreviewBtn').addEventListener('click', async () => {
    const btn = $('cleanupPreviewBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/cleanup');
      renderCleanupPreview(r);
      const n = (r.would_disable || []).length + (r.would_delete || []).length;
      if (!n) PEYK.toast(PEYK.t('cleanup_nothing'), 'success');
    } catch (e) { PEYK.toast(e.detail || 'error', 'error'); }
    finally { PEYK.setLoading(btn, false); }
  });

  $('cleanupRunBtn').addEventListener('click', async () => {
    if (!confirm(PEYK.t('cleanup_run_confirm'))) return;
    const btn = $('cleanupRunBtn');
    PEYK.setLoading(btn, true);
    try {
      const r = await PEYK.api('/api/cleanup', { method: 'POST' });
      PEYK.toast(PEYK.t('cleanup_done')
        .replace('{disabled}', r.disabled).replace('{deleted}', r.deleted), 'success');
      loadCleanupStatus();
      loadInbounds();
    } catch (e) { PEYK.toast(e.detail || 'error', 'error'); }
    finally { PEYK.setLoading(btn, false); }
  });

  // ---------------- renewal requests ----------------
  async function loadRenewRequests(show) {
    try {
      const r = await PEYK.api('/api/userbot/requests');
      const badge = $('rnBadge');
      const on = $('settingRenewEnabled').checked;
      badge.textContent = PEYK.t(on ? 'rn_on' : 'rn_off');
      badge.className = 'pill ' + (on ? 'pill-on' : 'pill-muted');
      $('rnPending').textContent = r.pending
        ? PEYK.t('rn_pending').replace('{n}', r.pending)
        : PEYK.t('rn_none');

      if (!show) return;
      const rows = r.requests || [];
      $('rnTableWrap').style.display = rows.length ? '' : 'none';
      const tbody = $('rnTableBody');
      tbody.innerHTML = '';
      const pill = st => st === 'approved'
        ? `<span class="pill pill-on">${esc(PEYK.t('rn_approved'))}</span>`
        : st === 'rejected'
          ? `<span class="pill pill-off">${esc(PEYK.t('rn_rejected'))}</span>`
          : `<span class="pill pill-warn">${esc(PEYK.t('rn_waiting'))}</span>`;
      rows.forEach(q => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td data-label="${esc(PEYK.t('inb_name'))}"><b>${esc(q.name || q.uid)}</b></td>
          <td data-label="${esc(PEYK.t('loginlog_time'))}" class="small">${esc(
            new Date((q.created_at || 0) * 1000)
              .toLocaleString(PEYK.getLang() === 'fa' ? 'fa-IR' : 'en-US'))}</td>
          <td data-label="${esc(PEYK.t('inb_status'))}">${pill(q.status)}</td>`;
        tbody.appendChild(tr);
      });
      if (!rows.length) PEYK.toast(PEYK.t('rn_none'), 'info', 2000);
    } catch (e) { /* non-fatal */ }
  }

  $('saveRenewBtn').addEventListener('click', () => {
    const days = $('settingRenewOptions').value
      .split(',').map(x => parseInt(x.trim(), 10)).filter(n => n > 0);
    if (!days.length) { PEYK.toast(PEYK.t('rn_options_bad'), 'error'); return; }
    saveSettings('saveRenewBtn', {
      userbot_renew_enabled: $('settingRenewEnabled').checked,
      voucher_redeem_enabled: $('settingVoucherRedeem').checked,
      notify_customer_enabled: $('settingNotifyCustomer').checked,
      shop_enabled: $('settingShopEnabled').checked,
      shop_instructions: $('settingShopInstructions').value.trim(),
      trial_selfserve_enabled: $('settingTrialSelfserve').checked,
      userbot_renew_options: days,
    }, () => loadRenewRequests(false));
  });

  $('rnRefreshBtn').addEventListener('click', () => loadRenewRequests(true));

  // ---------------- telegram backup ----------------
  async function loadBackupStatus() {
    try {
      const r = await PEYK.api('/api/backup/telegram');
      const badge = $('tgbkBadge');
      const on = !!r.auto_enabled && r.configured;
      badge.textContent = PEYK.t(on ? 'tgbk_on' : 'tgbk_off');
      badge.className = 'pill ' + (on ? 'pill-on' : 'pill-muted');

      // Only warn when it actually bites: ephemeral disk, nothing protecting it.
      $('ephemeralWarning').style.display =
        (r.storage_is_ephemeral && !(r.configured && r.auto_enabled)) ? '' : 'none';

      const last = r.last;
      $('tgbkLast').textContent = last
        ? PEYK.t(last.ok ? 'tgbk_last_ok' : 'tgbk_last_fail') + ': ' +
          new Date(last.ts * 1000).toLocaleString(PEYK.getLang() === 'fa' ? 'fa-IR' : 'en-US') +
          (last.ok ? '' : ' - ' + last.detail)
        : PEYK.t('tgbk_never');

      const boot = $('tgbkBootstrap');
      const needsBootstrap = r.storage_is_ephemeral && !r.bootstrap_configured;
      boot.style.display = needsBootstrap ? '' : 'none';
      if (needsBootstrap) boot.textContent = PEYK.t('tgbk_bootstrap_hint');
    } catch (e) { /* non-fatal */ }
  }

  $('saveBackupBtn').addEventListener('click', () => {
    saveSettings('saveBackupBtn', {
      auto_backup_enabled: $('settingAutoBackup').checked,
      auto_backup_hours: parseInt($('settingBackupHours').value, 10) || 6,
    }, loadBackupStatus);
  });

  $('backupNowBtn').addEventListener('click', async () => {
    const btn = $('backupNowBtn');
    PEYK.setLoading(btn, true);
    try {
      await PEYK.api('/api/backup/telegram', { method: 'POST' });
      PEYK.toast(PEYK.t('tgbk_sent'), 'success');
      loadBackupStatus();
    } catch (e) {
      const msg = e.detail === 'not-configured' ? PEYK.t('notify_not_configured') : (e.detail || 'error');
      PEYK.toast(msg, 'error', 7000);
    } finally { PEYK.setLoading(btn, false); }
  });

  $('tgRestoreBtn').addEventListener('click', async () => {
    if (!confirm(PEYK.t('tgbk_restore_confirm'))) return;
    const btn = $('tgRestoreBtn');
    PEYK.setLoading(btn, true);
    try {
      await PEYK.api('/api/backup/telegram/restore', { method: 'POST' });
      PEYK.toast(PEYK.t('settings_restored'), 'success', 4000);
      setTimeout(function () { window.location.href = '/login'; }, 1400);
    } catch (e) {
      const map = { 'no-backup-found': 'tgbk_none_found', 'not-a-peyk-backup': 'tgbk_bad_file' };
      PEYK.toast(map[e.detail] ? PEYK.t(map[e.detail]) : (e.detail || 'error'), 'error', 7000);
      PEYK.setLoading(btn, false);
    }
  });

  // ---------------- backup / restore ----------------
  $('backupBtn').addEventListener('click', async () => {
    const btn = $('backupBtn');
    PEYK.setLoading(btn, true);
    try {
      const res = await fetch('/api/backup', { credentials: 'same-origin' });
      if (!res.ok) throw new Error('backup-failed');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const cd = res.headers.get('Content-Disposition') || '';
      a.download = (cd.match(/filename="([^"]+)"/) || [])[1] || 'backup.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      PEYK.toast(e.message || 'error', 'error');
    } finally { PEYK.setLoading(btn, false); }
  });

  $('restoreBtn').addEventListener('click', () => $('restoreFile').click());
  $('restoreFile').addEventListener('change', async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (!confirm(PEYK.t('settings_restore_confirm'))) return;
    try {
      const parsed = JSON.parse(await file.text());
      await PEYK.api('/api/restore', { method: 'POST', body: { db: parsed } });
      PEYK.toast(PEYK.t('settings_restored'), 'success', 4000);
      setTimeout(() => { window.location.href = '/login'; }, 1400);
    } catch (err) {
      PEYK.toast(err.detail || err.message || 'invalid-backup', 'error');
    }
  });

  $('logoutAllBtn').addEventListener('click', async () => {
    if (!confirm(PEYK.t('settings_logout_all_confirm'))) return;
    try { await PEYK.api('/api/logout-all', { method: 'POST' }); } catch (e) { /* leaving anyway */ }
    window.location.href = '/login';
  });

  // ---------------- boot ----------------
  // Endpoints and the settings hydrate are owner-only, so they are kicked off
  // from the /api/me handler once the role is known.
  loadInbounds();
  loadPlans();
})();
