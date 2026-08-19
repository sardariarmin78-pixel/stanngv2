/* ===========================================================
   Dashboard controller.

   Views: dashboard, users, plans, traffic, security,
   notifications, settings. All markup is built through
   STANNG.escapeHtml — user- and API-supplied strings are never
   interpolated raw into innerHTML.
   =========================================================== */
(() => {
  let inbounds = [];
  let plans = [];
  let endpoints = [];
  let lastHourly = [];
  let filterText = '';
  let filterStatus = '';
  const selected = new Set();

  const $ = (id) => document.getElementById(id);
  const esc = STANNG.escapeHtml;
  const DAY = 86400;

  // ---------------- session guard + settings hydrate ----------------
  STANNG.api('/api/me').then(me => {
    if (!me.logged_in) { window.location.href = '/login'; return; }
    $('appVersion').textContent = me.app_version || '';
    $('otaCurrent').textContent = me.app_version || '-';
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
    set('settingIdleTimeout', s.idle_timeout_seconds != null ? s.idle_timeout_seconds : 600);
    set('settingHistoryDays', s.history_days != null ? s.history_days : 30);
    set('settingBotToken', s.telegram_bot_token || '');
    set('settingChatId', s.telegram_chat_id || '');
    check('settingNotifyQuota', s.notify_quota_enabled !== false);
    set('settingNotifyPercent', s.notify_quota_percent != null ? s.notify_quota_percent : 80);
    check('settingNotifyExpiry', s.notify_expiry_enabled !== false);
    set('settingNotifyDays', s.notify_expiry_days != null ? s.notify_expiry_days : 3);
    applyFragmentFieldState();
  }).catch(() => { window.location.href = '/login'; });

  $('settingSound').checked = STANNG.isSoundEnabled();

  // ---------------- navigation ----------------
  const views = document.querySelectorAll('.view');
  const navItems = document.querySelectorAll('.nav-item[data-view]');
  const viewTitle = $('viewTitle');
  const titleKeys = {
    dashboard: 'nav_dashboard', inbounds: 'nav_inbounds', plans: 'nav_plans',
    endpoints: 'nav_endpoints', traffic: 'nav_traffic', security: 'nav_security',
    notifications: 'nav_notifications', settings: 'nav_settings',
  };

  function showView(name) {
    views.forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
    navItems.forEach(n => n.classList.toggle('active', n.dataset.view === name));
    viewTitle.setAttribute('data-i18n', titleKeys[name]);
    viewTitle.textContent = STANNG.t(titleKeys[name]);
    if (name === 'inbounds' || name === 'traffic') loadInbounds();
    if (name === 'plans') loadPlans();
    if (name === 'endpoints') loadEndpoints();
    if (name === 'security') { loadTwofaStatus(); loadLoginLog(); }
    closeSidebarMobile();
    STANNG.playSfx('open', 0.25);
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
      STANNG.setLang(b.dataset.lang);
      viewTitle.textContent = STANNG.t(viewTitle.getAttribute('data-i18n'));
      renderInbounds(); renderTrafficTable(); renderPlans();
      STANNG.playSfx('toggle', 0.25);
    });
  });
  $('themeToggle').addEventListener('click', () => {
    STANNG.setTheme(STANNG.getTheme() === 'dark' ? 'light' : 'dark');
    STANNG.playSfx('toggle', 0.25);
    renderTrafficChart($('trafficChart'), lastHourly);
  });
  $('soundToggle').addEventListener('click', () => {
    const next = !STANNG.isSoundEnabled();
    STANNG.setSoundEnabled(next);
    $('settingSound').checked = next;
    if (next) STANNG.playSfx('click');
  });

  $('logoutBtn').addEventListener('click', async () => {
    try { await STANNG.api('/api/logout', { method: 'POST' }); } catch (e) { /* leave anyway */ }
    window.location.href = '/login';
  });

  // ---------------- modals ----------------
  function openModal(id) { $(id).classList.add('open'); STANNG.playSfx('open', 0.3); }
  function closeModal(id) { $(id).classList.remove('open'); STANNG.playSfx('close', 0.3); }
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
    STANNG.setLoading(btn, true);
    try {
      const r = await STANNG.api('/api/ota/check');
      if (r.update_available) {
        el.innerHTML = `<span style="color:var(--accent)">${esc(STANNG.t('dash_ota_available'))} <b>${esc(r.latest)}</b></span> — <a href="${esc(r.url)}" target="_blank" rel="noopener" style="color:var(--info); text-decoration:underline;">GitHub</a>`;
        otaLatest = r.latest;
        $('otaUpdateBtn').style.display = '';
        $('otaUpdateHint').style.display = '';
      } else {
        el.innerHTML = `<span style="color:var(--ok)">${esc(STANNG.t('dash_ota_uptodate'))}</span>`;
        otaLatest = null;
        $('otaUpdateBtn').style.display = 'none';
        $('otaUpdateHint').style.display = 'none';
      }
    } catch (e) {
      let msg = e.detail || 'error';
      if (msg === 'no-repo-configured') msg = STANNG.t('ota_no_repo');
      STANNG.toast(msg, 'error');
    } finally { STANNG.setLoading(btn, false); }
  });

  $('otaUpdateBtn').addEventListener('click', async () => {
    if (!confirm(STANNG.t('dash_ota_update_confirm').replace('{version}', otaLatest || ''))) return;
    const btn = $('otaUpdateBtn');
    STANNG.setLoading(btn, true);
    $('otaCheckBtn').disabled = true;
    try {
      const r = await STANNG.api('/api/ota/update', { method: 'POST' });
      if (r.ok) {
        $('otaResult').innerHTML = `<span style="color:var(--accent)">${esc(STANNG.t('dash_ota_updating'))}</span>`;
        waitForRestart();
      } else {
        STANNG.toast(STANNG.t('dash_ota_uptodate'), 'success');
        STANNG.setLoading(btn, false);
        $('otaCheckBtn').disabled = false;
      }
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
      STANNG.setLoading(btn, false);
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
          STANNG.toast(STANNG.t('dash_ota_done'), 'success', 3000);
          setTimeout(() => window.location.reload(), 1200);
          return;
        }
      } catch (e) { /* restarting */ }
      if (n > 60) { clearInterval(poll); STANNG.toast(STANNG.t('dash_ota_timeout'), 'error', 8000); }
    }, 3000);
  }

  $('quickAddBtn').addEventListener('click', () => { showView('inbounds'); openInboundModal(); });
  $('quickBulkBtn').addEventListener('click', () => { showView('inbounds'); openBulkModal(); });

  // ---------------- users ----------------
  async function loadInbounds() {
    try {
      const r = await STANNG.api('/api/inbounds');
      inbounds = r.inbounds || [];
      // Drop selections for rows that no longer exist.
      const live = new Set(inbounds.map(i => i.uid));
      [...selected].forEach(uid => { if (!live.has(uid)) selected.delete(uid); });
      renderInbounds();
      renderTrafficTable();
      $('navInboundCount').textContent = inbounds.length;
      const soon = inbounds.filter(i => i.status.days_left != null
        && i.status.days_left <= 7 && !i.status.expired).length;
      $('statExpiring').textContent = soon;
    } catch (e) {
      if (e.status === 401) { window.location.href = '/login'; return; }
      STANNG.toast(e.detail || 'error', 'error');
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
        ? `${STANNG.fmtBytes(st.used)} / ${ib.quota_gb} GB`
        : `${STANNG.fmtBytes(st.used)} / ${STANNG.t('unlimited')}`;
      const expireTxt = ib.expire_at
        ? `${st.days_left} ${STANNG.t('inb_days_left')}`
        : STANNG.t('inb_no_expire');

      const tr = document.createElement('tr');
      tr.className = selected.has(ib.uid) ? 'is-selected' : '';
      tr.innerHTML = `
        <td class="col-check" data-label=""><input type="checkbox" class="checkbox row-check" data-uid="${esc(ib.uid)}" ${selected.has(ib.uid) ? 'checked' : ''} aria-label="select"></td>
        <td data-label="${esc(STANNG.t('inb_name'))}"><b>${esc(ib.name)}</b>${ib.note ? `<div class="small muted">${esc(ib.note)}</div>` : ''}</td>
        <td data-label="${esc(STANNG.t('inb_status'))}"><span class="pill ${label.cls}"><span class="pill-dot"></span>${esc(STANNG.t(label.key))}</span></td>
        <td data-label="${esc(STANNG.t('inb_usage'))}" style="min-width:150px;">
          <div class="small mono">${esc(quotaTxt)}</div>
          <div class="bar ${usageClass(pct)}" style="margin-top:5px;"><span style="width:${pct}%"></span></div>
        </td>
        <td data-label="${esc(STANNG.t('inb_expire'))}" class="mono small">${esc(expireTxt)}</td>
        <td data-label="${esc(STANNG.t('inb_max_conn'))}" class="mono small">${esc(st.active_connections)}${ib.max_connections ? ' / ' + esc(ib.max_connections) : ''}</td>
        <td data-label="${esc(STANNG.t('inb_actions'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-action="links" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('inb_links'))}"><svg><use href="#icon-qr"/></svg></button>
            <button class="icon-btn btn-sm" data-action="history" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('traffic_history'))}"><svg><use href="#icon-chart"/></svg></button>
            <button class="icon-btn btn-sm" data-action="edit" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('edit'))}"><svg><use href="#icon-edit"/></svg></button>
            <button class="icon-btn btn-sm" data-action="renew" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('inb_renew'))}"><svg><use href="#icon-clock"/></svg></button>
            <button class="icon-btn btn-sm" data-action="reset" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('inb_reset_usage'))}"><svg><use href="#icon-refresh"/></svg></button>
            <button class="icon-btn btn-sm" data-action="regen" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('inb_regenerate'))}"><svg><use href="#icon-key"/></svg></button>
            <button class="icon-btn btn-sm" data-action="delete" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('delete'))}" style="color:var(--err)"><svg><use href="#icon-trash"/></svg></button>
          </div>
        </td>`;
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
    syncSelectionUI();
  }

  // Delegated: one listener regardless of row count.
  $('inboundsTableBody').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (btn) { handleAction(btn.dataset.action, btn.dataset.uid); return; }
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
      if (confirmKeys[action] && !confirm(STANNG.t(confirmKeys[action]).replace('{n}', uids.length))) return;

      const body = { action, uids };
      if (action === 'renew') {
        const days = parseInt(prompt(STANNG.t('inb_renew_days'), '30'), 10);
        if (!days || days < 1) return;
        body.days = days;
      }
      STANNG.setLoading(btn, true);
      try {
        const r = await STANNG.api('/api/inbounds/bulk-action', { method: 'POST', body });
        STANNG.toast(STANNG.t('bulk_done').replace('{n}', r.affected), 'success');
        selected.clear();
        loadInbounds();
      } catch (e) {
        STANNG.toast(e.detail || 'error', 'error');
      } finally { STANNG.setLoading(btn, false); }
    });
  });

  // ---------------- user modal ----------------
  function fillPlanSelect(sel) {
    const el = $(sel);
    const keep = el.value;
    el.innerHTML = `<option value="">${esc(STANNG.t('inb_no_plan'))}</option>`;
    plans.forEach(p => {
      const o = document.createElement('option');
      o.value = p.id;
      o.textContent = `${p.name} — ${p.quota_gb || '∞'}GB / ${p.days || '∞'}d`;
      el.appendChild(o);
    });
    el.value = keep;
  }

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
      STANNG.toast(STANNG.t('inb_invalid_number'), 'error'); return;
    }
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
    } finally { STANNG.setLoading(btn, false); }
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
    STANNG.setLoading(btn, true);
    try {
      const r = await STANNG.api('/api/inbounds/bulk', { method: 'POST', body });
      STANNG.toast(STANNG.t('bulk_created').replace('{n}', r.created), 'success');
      closeModal('bulkModal');
      loadInbounds();
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    } finally { STANNG.setLoading(btn, false); }
  });

  // ---------------- plans ----------------
  async function loadPlans() {
    try {
      const r = await STANNG.api('/api/plans');
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
        <td data-label="${esc(STANNG.t('plans_name'))}"><b>${esc(p.name)}</b></td>
        <td data-label="${esc(STANNG.t('inb_quota'))}" class="mono small">${p.quota_gb ? esc(p.quota_gb) + ' GB' : esc(STANNG.t('unlimited'))}</td>
        <td data-label="${esc(STANNG.t('inb_expire'))}" class="mono small">${p.days ? esc(p.days) + ' ' + esc(STANNG.t('inb_days_left')) : esc(STANNG.t('unlimited'))}</td>
        <td data-label="${esc(STANNG.t('inb_max_conn'))}" class="mono small">${p.max_connections || esc(STANNG.t('unlimited'))}</td>
        <td data-label="${esc(STANNG.t('inb_actions'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-plan-action="use" data-id="${esc(p.id)}" title="${esc(STANNG.t('inb_bulk_create'))}"><svg><use href="#icon-layers"/></svg></button>
            <button class="icon-btn btn-sm" data-plan-action="edit" data-id="${esc(p.id)}" title="${esc(STANNG.t('edit'))}"><svg><use href="#icon-edit"/></svg></button>
            <button class="icon-btn btn-sm" data-plan-action="delete" data-id="${esc(p.id)}" title="${esc(STANNG.t('delete'))}" style="color:var(--err)"><svg><use href="#icon-trash"/></svg></button>
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
    if (!confirm(STANNG.t('plans_delete_confirm'))) return;
    try {
      await STANNG.api(`/api/plans/${plan.id}`, { method: 'DELETE' });
      STANNG.toast(STANNG.t('plans_deleted'), 'success');
      loadPlans();
    } catch (err) { STANNG.toast(err.detail || 'error', 'error'); }
  });

  function openPlanModal(plan = null) {
    $('planModalTitle').textContent = plan ? STANNG.t('edit') : STANNG.t('plans_add');
    $('planId').value = plan ? plan.id : '';
    $('planName').value = plan ? plan.name : '';
    $('planQuota').value = plan ? (plan.quota_gb || '') : '';
    $('planDays').value = plan ? (plan.days || '') : '';
    $('planMaxConn').value = plan ? (plan.max_connections || '') : '';
    $('planMaxReq').value = plan ? (plan.max_requests || '') : '';
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
    };
    if (!body.name) { STANNG.toast(STANNG.t('plans_name_required'), 'error'); return; }
    const btn = $('planSaveBtn');
    STANNG.setLoading(btn, true);
    try {
      if (id) await STANNG.api(`/api/plans/${id}`, { method: 'PATCH', body });
      else await STANNG.api('/api/plans', { method: 'POST', body });
      STANNG.toast(STANNG.t('settings_saved'), 'success');
      closeModal('planModal');
      loadPlans();
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    } finally { STANNG.setLoading(btn, false); }
  });


  // ---------------- endpoints ----------------
  async function loadEndpoints() {
    try {
      const r = await STANNG.api('/api/endpoints');
      endpoints = r.endpoints || [];
      renderEndpoints();
      $('navEndpointCount').textContent = endpoints.filter(e => e.enabled !== false).length;
    } catch (e) { /* non-fatal */ }
  }

  function healthCell(ep) {
    const h = ep.health || {};
    if (h.ok === null || h.ok === undefined) {
      return `<span class="pill pill-muted">${esc(STANNG.t('ep_untested'))}</span>`;
    }
    if (!h.ok) return `<span class="pill pill-off"><span class="pill-dot"></span>${esc(STANNG.t('ep_down'))}</span>`;
    const ms = h.latency_ms;
    const cls = ms == null ? 'pill-on' : (ms < 300 ? 'pill-on' : ms < 900 ? 'pill-warn' : 'pill-off');
    return `<span class="pill ${cls}"><span class="pill-dot"></span>${esc(ms != null ? ms + ' ms' : STANNG.t('ep_up'))}</span>`;
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
          <td data-label="${esc(STANNG.t('ep_name'))}"><b>${esc(ep.name || ep.address)}</b></td>
          <td data-label="${esc(STANNG.t('ep_address'))}" class="mono small">${esc(ep.address)}:${esc(ep.port || 443)}</td>
          <td data-label="${esc(STANNG.t('ep_host'))}" class="mono small">${esc(ep.host || '—')}${ep.sni ? ' / ' + esc(ep.sni) : ''}</td>
          <td data-label="${esc(STANNG.t('ep_health'))}">${healthCell(ep)}</td>
          <td data-label="${esc(STANNG.t('inb_status'))}"><span class="pill ${on ? 'pill-on' : 'pill-muted'}"><span class="pill-dot"></span>${esc(STANNG.t(on ? 'active' : 'inactive'))}</span></td>
          <td data-label="${esc(STANNG.t('inb_actions'))}">
            <div class="row-actions">
              <button class="icon-btn btn-sm" data-ep-action="test" data-id="${esc(ep.id)}" title="${esc(STANNG.t('ep_test'))}"><svg><use href="#icon-refresh"/></svg></button>
              <button class="icon-btn btn-sm" data-ep-action="edit" data-id="${esc(ep.id)}" title="${esc(STANNG.t('edit'))}"><svg><use href="#icon-edit"/></svg></button>
              <button class="icon-btn btn-sm" data-ep-action="delete" data-id="${esc(ep.id)}" title="${esc(STANNG.t('delete'))}" style="color:var(--err)"><svg><use href="#icon-trash"/></svg></button>
            </div>
          </td>`;
        frag.appendChild(tr);
      });
    tbody.appendChild(frag);
  }

  function openEndpointModal(ep = null) {
    $('endpointModalTitle').textContent = ep ? STANNG.t('edit') : STANNG.t('ep_add');
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
    if (!body.address) { STANNG.toast(STANNG.t('ep_address_required'), 'error'); return; }
    const btn = $('epSaveBtn');
    STANNG.setLoading(btn, true);
    try {
      if (id) await STANNG.api(`/api/endpoints/${id}`, { method: 'PATCH', body });
      else await STANNG.api('/api/endpoints', { method: 'POST', body });
      STANNG.toast(STANNG.t('settings_saved'), 'success');
      closeModal('endpointModal');
      loadEndpoints();
    } catch (e) {
      const map = { 'invalid-address': 'ep_bad_address', 'endpoint-limit-reached': 'ep_limit' };
      STANNG.toast(map[e.detail] ? STANNG.t(map[e.detail]) : (e.detail || 'error'), 'error');
    } finally { STANNG.setLoading(btn, false); }
  });

  async function testEndpoint(id, silent) {
    try {
      const r = await STANNG.api(`/api/endpoints/${id}/test`, { method: 'POST' });
      const ep = endpoints.find(e => e.id === id);
      if (ep) ep.health = r.health;
      renderEndpoints();
      if (!silent) {
        STANNG.toast(
          r.ok ? `${STANNG.t('ep_up')} — ${r.latency_ms} ms` : `${STANNG.t('ep_down')} — ${r.detail}`,
          r.ok ? 'success' : 'error');
      }
      return r.ok;
    } catch (e) {
      if (!silent) STANNG.toast(e.detail || 'error', 'error');
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
      STANNG.setLoading(btn, true);
      await testEndpoint(ep.id, false);
      STANNG.setLoading(btn, false);
      return;
    }
    if (!confirm(STANNG.t('ep_delete_confirm'))) return;
    try {
      await STANNG.api(`/api/endpoints/${ep.id}`, { method: 'DELETE' });
      STANNG.toast(STANNG.t('ep_deleted'), 'success');
      loadEndpoints();
    } catch (err) { STANNG.toast(err.detail || 'error', 'error'); }
  });

  $('testAllEndpointsBtn').addEventListener('click', async () => {
    const btn = $('testAllEndpointsBtn');
    STANNG.setLoading(btn, true);
    // Sequential on purpose: a burst of parallel probes from one box skews
    // the latency numbers it is trying to measure.
    let up = 0;
    for (const ep of endpoints) if (await testEndpoint(ep.id, true)) up++;
    STANNG.setLoading(btn, false);
    STANNG.toast(STANNG.t('ep_test_result').replace('{up}', up).replace('{total}', endpoints.length),
                 up === endpoints.length ? 'success' : 'info');
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
        <td data-label="${esc(STANNG.t('inb_name'))}"><b>${esc(ib.name)}</b></td>
        <td data-label="${esc(STANNG.t('dash_upload'))}" class="mono small">${esc(STANNG.fmtBytes(ib.used_up || 0))}</td>
        <td data-label="${esc(STANNG.t('dash_download'))}" class="mono small">${esc(STANNG.fmtBytes(ib.used_down || 0))}</td>
        <td data-label="${esc(STANNG.t('inb_usage'))}" class="mono small">${esc(STANNG.fmtBytes((ib.used_up || 0) + (ib.used_down || 0)))}</td>
        <td data-label="${esc(STANNG.t('traffic_history'))}">
          <div class="row-actions">
            <button class="icon-btn btn-sm" data-action="history" data-uid="${esc(ib.uid)}" title="${esc(STANNG.t('traffic_history'))}"><svg><use href="#icon-chart"/></svg></button>
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
    if (job.confirm && !confirm(STANNG.t(job.confirm))) return;
    try {
      await STANNG.api(job.path, { method: job.method });
      STANNG.toast(STANNG.t(job.ok), 'success');
      loadInbounds();
    } catch (e) { STANNG.toast(e.detail || 'error', 'error'); }
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
    if (!uid || !days || days < 1) { STANNG.toast(STANNG.t('inb_invalid_number'), 'error'); return; }
    const btn = $('renewSaveBtn');
    STANNG.setLoading(btn, true);
    try {
      await STANNG.api(`/api/inbounds/${uid}/renew`, {
        method: 'POST', body: { days, reset_usage: $('renewResetUsage').checked },
      });
      STANNG.toast(STANNG.t('inb_renewed'), 'success');
      closeModal('renewModal');
      loadInbounds();
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    } finally { STANNG.setLoading(btn, false); }
  });

  // ---------------- links ----------------
  async function showLinks(uid) {
    try {
      const r = await STANNG.api(`/api/inbounds/${uid}/links`);
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
    } catch (e) { STANNG.toast(e.detail || 'error', 'error'); }
  }

  // ---------------- per-user history ----------------
  async function showHistory(uid) {
    try {
      const r = await STANNG.api(`/api/inbounds/${uid}/history`);
      $('historyName').textContent = r.name || '';
      const series = (r.history || []).map(h => ({
        t: Date.parse(h.d + 'T00:00:00Z') / 1000, up: h.up, down: h.down,
      }));
      const totalUp = series.reduce((a, b) => a + b.up, 0);
      const totalDown = series.reduce((a, b) => a + b.down, 0);
      const active = series.filter(s => s.up + s.down > 0).length;
      $('historySummary').textContent =
        `${STANNG.t('dash_upload')}: ${STANNG.fmtBytes(totalUp)} · ` +
        `${STANNG.t('dash_download')}: ${STANNG.fmtBytes(totalDown)} · ` +
        `${STANNG.t('history_active_days').replace('{n}', active)}`;
      openModal('historyModal');
      // Canvas needs a laid-out box before it can size itself.
      requestAnimationFrame(() => renderTrafficChart($('historyChart'), series, { unit: 'day' }));
    } catch (e) { STANNG.toast(e.detail || 'error', 'error'); }
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
      STANNG.toast(STANNG.t('copied'), 'success', 1500);
      STANNG.playSfx('click', 0.35);
    } catch (e) { STANNG.toast('error', 'error'); }
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
      STANNG.toast(STANNG.t('setup_mismatch'), 'error');
      STANNG.shake($('securityForm'));
      return;
    }
    const btn = $('securityBtn');
    STANNG.setLoading(btn, true);
    try {
      await STANNG.api('/api/change-password', {
        method: 'POST',
        body: {
          old_password: $('oldPassword').value,
          new_username: $('newUsername').value.trim(),
          new_password: np,
        },
      });
      STANNG.toast(STANNG.t('sec_updated'), 'success');
      $('securityForm').reset();
    } catch (e2) {
      let msg = e2.detail;
      if (msg === 'wrong-old-password') msg = STANNG.t('sec_wrong_old');
      else if (msg === 'weak-password') msg = STANNG.t('setup_password_hint');
      else if (msg === 'invalid-username') msg = STANNG.t('setup_username_hint');
      else if (msg === 'nothing-to-change') msg = STANNG.t('sec_nothing_to_change');
      STANNG.toast(msg || 'error', 'error');
      STANNG.shake($('securityForm'));
    } finally { STANNG.setLoading(btn, false); }
  });

  // ---------------- security: 2FA ----------------
  async function loadTwofaStatus() {
    try {
      const r = await STANNG.api('/api/2fa/status');
      const on = !!r.enabled;
      const badge = $('twofaBadge');
      badge.textContent = STANNG.t(on ? 'twofa_on' : 'twofa_off');
      badge.className = 'pill ' + (on ? 'pill-on' : 'pill-muted');
      $('twofaOffBlock').style.display = on ? 'none' : '';
      $('twofaOnBlock').style.display = on ? '' : 'none';
      $('twofaRecoveryLeft').textContent = r.recovery_remaining || 0;
    } catch (e) { /* non-fatal */ }
  }

  $('twofaSetupBtn').addEventListener('click', async () => {
    const btn = $('twofaSetupBtn');
    STANNG.setLoading(btn, true);
    try {
      const r = await STANNG.api('/api/2fa/setup', { method: 'POST' });
      $('twofaSecret').textContent = r.secret;
      $('twofaQr').src = `/api/2fa/qr?t=${Date.now()}`;
      $('twofaStep1').style.display = '';
      $('twofaStep2').style.display = 'none';
      $('twofaConfirmBtn').style.display = '';
      $('twofaCode').value = '';
      openModal('twofaModal');
    } catch (e) {
      STANNG.toast(e.detail || 'error', 'error');
    } finally { STANNG.setLoading(btn, false); }
  });

  $('twofaConfirmBtn').addEventListener('click', async () => {
    const code = $('twofaCode').value.trim();
    if (!/^\d{6}$/.test(code)) { STANNG.toast(STANNG.t('twofa_invalid'), 'error'); return; }
    const btn = $('twofaConfirmBtn');
    STANNG.setLoading(btn, true);
    try {
      const r = await STANNG.api('/api/2fa/enable', { method: 'POST', body: { code } });
      showRecoveryCodes(r.recovery_codes || []);
      STANNG.toast(STANNG.t('twofa_enabled'), 'success');
      loadTwofaStatus();
    } catch (e) {
      STANNG.toast(e.detail === 'invalid-code' ? STANNG.t('twofa_invalid') : (e.detail || 'error'), 'error');
    } finally { STANNG.setLoading(btn, false); }
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
    $('confirmPassTitle').textContent = STANNG.t(titleKey);
    $('confirmPassInput').value = '';
    openModal('confirmPassModal');
    setTimeout(() => $('confirmPassInput').focus(), 60);
  }
  $('confirmPassBtn').addEventListener('click', async () => {
    const password = $('confirmPassInput').value;
    if (!password || !pendingConfirm) return;
    const btn = $('confirmPassBtn');
    STANNG.setLoading(btn, true);
    try {
      await pendingConfirm(password);
      closeModal('confirmPassModal');
    } catch (e) {
      STANNG.toast(e.detail === 'wrong-password' ? STANNG.t('sec_wrong_old') : (e.detail || 'error'), 'error');
      STANNG.shake($('confirmPassInput'));
    } finally { STANNG.setLoading(btn, false); }
  });
  $('confirmPassInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('confirmPassBtn').click(); }
  });

  $('twofaDisableBtn').addEventListener('click', () => {
    askPassword('twofa_disable', async (password) => {
      await STANNG.api('/api/2fa/disable', { method: 'POST', body: { password } });
      STANNG.toast(STANNG.t('twofa_disabled'), 'success');
      loadTwofaStatus();
    });
  });

  $('twofaRegenBtn').addEventListener('click', () => {
    askPassword('twofa_regen', async (password) => {
      const r = await STANNG.api('/api/2fa/recovery-codes', { method: 'POST', body: { password } });
      showRecoveryCodes(r.recovery_codes || []);
      loadTwofaStatus();
    });
  });

  // ---------------- security: login log ----------------
  async function loadLoginLog() {
    try {
      const r = await STANNG.api('/api/login-log');
      const tbody = $('loginLogBody');
      const rows = r.entries || [];
      $('loginLogEmpty').style.display = rows.length ? 'none' : 'block';
      tbody.innerHTML = '';
      const frag = document.createDocumentFragment();
      rows.forEach(e => {
        const tr = document.createElement('tr');
        const when = new Date(e.ts * 1000).toLocaleString(
          STANNG.getLang() === 'fa' ? 'fa-IR' : 'en-US');
        const pill = e.ok
          ? `<span class="pill pill-on"><span class="pill-dot"></span>${esc(STANNG.t('loginlog_ok'))}</span>`
          : `<span class="pill pill-off"><span class="pill-dot"></span>${esc(STANNG.t('loginlog_fail'))}</span>`;
        tr.innerHTML = `
          <td data-label="${esc(STANNG.t('loginlog_time'))}" class="small">${esc(when)}</td>
          <td data-label="${esc(STANNG.t('loginlog_ip'))}" class="mono small">${esc(e.ip)}</td>
          <td data-label="${esc(STANNG.t('loginlog_method'))}" class="mono small">${esc(e.method)}</td>
          <td data-label="${esc(STANNG.t('loginlog_result'))}">${pill}</td>`;
        frag.appendChild(tr);
      });
      tbody.appendChild(frag);
    } catch (e) { /* non-fatal */ }
  }
  $('loginLogRefresh').addEventListener('click', loadLoginLog);

  // ---------------- settings ----------------
  async function saveSettings(btnId, payload, after) {
    const btn = $(btnId);
    STANNG.setLoading(btn, true);
    try {
      const r = await STANNG.api('/api/settings', { method: 'POST', body: payload });
      STANNG.toast(STANNG.t('settings_saved'), 'success');
      if (after) after(r);
    } catch (e) {
      let msg = e.detail || 'error';
      if (msg === 'invalid-bot-token') msg = STANNG.t('notify_bad_token');
      if (msg === 'invalid-ota-repo') msg = STANNG.t('ota_bad_repo');
      STANNG.toast(msg, 'error');
    } finally { STANNG.setLoading(btn, false); }
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
    STANNG.setLoading(btn, true);
    try {
      await STANNG.api('/api/notify/test', { method: 'POST' });
      STANNG.toast(STANNG.t('notify_test_sent'), 'success');
    } catch (e) {
      const msg = e.detail === 'not-configured' ? STANNG.t('notify_not_configured') : (e.detail || 'error');
      STANNG.toast(msg, 'error', 7000);
    } finally { STANNG.setLoading(btn, false); }
  });

  function applyFragmentFieldState() {
    const on = $('settingFragmentEnabled').checked;
    const f = $('fragmentFields');
    f.style.opacity = on ? '1' : '.45';
    f.style.pointerEvents = on ? 'auto' : 'none';
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
      const cd = res.headers.get('Content-Disposition') || '';
      a.download = (cd.match(/filename="([^"]+)"/) || [])[1] || 'backup.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      STANNG.toast(e.message || 'error', 'error');
    } finally { STANNG.setLoading(btn, false); }
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
      setTimeout(() => { window.location.href = '/login'; }, 1400);
    } catch (err) {
      STANNG.toast(err.detail || err.message || 'invalid-backup', 'error');
    }
  });

  $('logoutAllBtn').addEventListener('click', async () => {
    if (!confirm(STANNG.t('settings_logout_all_confirm'))) return;
    try { await STANNG.api('/api/logout-all', { method: 'POST' }); } catch (e) { /* leaving anyway */ }
    window.location.href = '/login';
  });

  // ---------------- boot ----------------
  loadInbounds();
  loadPlans();
  loadEndpoints();
})();
