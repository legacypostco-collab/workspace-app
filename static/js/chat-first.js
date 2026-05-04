/* Chat-First UI — gradient minimalist (07r/07s design).
 *
 * State machine:
 *   - WELCOME: hero + title + input + pills (sidebar collapsed by default for new users)
 *   - CONV: chat thread + sticky bottom input
 *
 * Sidebar logic:
 *   - First visit (no chat history): collapsed
 *   - Returning user (>0 chats): open by default on desktop
 *   - Mobile (<768px): always overlay (slide over content, never push)
 *   - State persisted in localStorage 'cf_sidebar_open'
 */
(function(){
  'use strict';

  const SB_KEY = 'cf_sidebar_open';
  const CONV_KEY = 'cf_active_conv';

  let state = {
    convId: null,
    ws: null,
    wsRetry: 0,
    streaming: false,
    currentBubble: null,
    config: null,
    convs: [],
    _lastCards: [],
    _lastActions: [],
    _intent: 'default',
  };

  // Persist active conversation id across page reloads so we don't spawn
  // a fresh "Без названия" chat every time the user refreshes.
  function setConvId(id) {
    state.convId = id || null;
    try {
      if (id) localStorage.setItem(CONV_KEY, id);
      else localStorage.removeItem(CONV_KEY);
    } catch(e){}
  }
  function getStoredConvId() {
    try { return localStorage.getItem(CONV_KEY); } catch(e) { return null; }
  }

  // ── Helpers ──────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const csrf = () => document.cookie.replace(/(?:(?:^|.*;\s*)csrftoken\s*=\s*([^;]*).*$)|^.*$/, '$1');
  const esc = s => (s == null ? '' : String(s)).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const fmtMoney = (v, c='USD') => {
    if (!v && v !== 0) return '—';
    const sym = {USD:'$', EUR:'€', RUB:'₽', CNY:'¥'}[c] || '';
    return sym + Number(v).toLocaleString('en-US', {maximumFractionDigits:0});
  };

  async function api(path, opts={}) {
    const res = await fetch(path, {
      headers: {'Content-Type':'application/json','X-CSRFToken': csrf(), ...(opts.headers||{})},
      ...opts,
    });
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json();
  }

  // ══════════════════════════════════════════════════════════
  // Sidebar toggle
  // ══════════════════════════════════════════════════════════
  function isMobile() { return window.innerWidth <= 768; }

  // ── Tiny "ding" via WebAudio (no external assets) ─────────────
  let _audioCtx = null;
  function notifBeep() {
    try {
      if (localStorage.getItem('cf_notif_sound') === '0') return; // user-muted
      _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const ctx = _audioCtx;
      // Browsers require a user gesture to start audio; if not yet allowed, bail silently.
      if (ctx.state === 'suspended') { ctx.resume().catch(() => {}); }
      const t0 = ctx.currentTime;
      // Two short tones — a friendly "di-ding".
      [
        {f: 880, start: 0,    dur: 0.09, gain: 0.08},
        {f: 1320, start: 0.09, dur: 0.13, gain: 0.07},
      ].forEach(n => {
        const osc = ctx.createOscillator();
        const g = ctx.createGain();
        osc.frequency.value = n.f;
        osc.type = 'sine';
        g.gain.setValueAtTime(0, t0 + n.start);
        g.gain.linearRampToValueAtTime(n.gain, t0 + n.start + 0.01);
        g.gain.linearRampToValueAtTime(0, t0 + n.start + n.dur);
        osc.connect(g); g.connect(ctx.destination);
        osc.start(t0 + n.start);
        osc.stop(t0 + n.start + n.dur + 0.02);
      });
    } catch (e) { /* audio unsupported — silent */ }
  }
  window.toggleNotifSound = function(on) {
    localStorage.setItem('cf_notif_sound', on ? '1' : '0');
  };

  // ── Settings panel ────────────────────────────────────────────
  function applyDarkMode(on) {
    document.body.classList.toggle('dark-mode', !!on);
    localStorage.setItem('cf_dark_mode', on ? '1' : '0');
  }
  function applyLang(lang) {
    if (!lang) return;
    document.cookie = 'django_language=' + lang + '; path=/; max-age=' + (60*60*24*365);
    localStorage.setItem('cf_lang', lang);
    // Reload to re-render server-side translations
    location.reload();
  }
  function loadSettings() {
    // Sound toggle
    const sndEl = document.getElementById('settingNotifSound');
    if (sndEl) sndEl.checked = localStorage.getItem('cf_notif_sound') !== '0';
    // Dark mode
    const darkEl = document.getElementById('settingDarkMode');
    const darkOn = localStorage.getItem('cf_dark_mode') === '1';
    if (darkEl) darkEl.checked = darkOn;
    if (darkOn) document.body.classList.add('dark-mode');
    // Lang
    const langEl = document.getElementById('settingLang');
    if (langEl) {
      const m = document.cookie.match(/django_language=([a-z]+)/);
      langEl.value = (m && m[1]) || localStorage.getItem('cf_lang') || 'ru';
    }
  }
  window.onSettingChange = function(key, val) {
    if (key === 'sound') toggleNotifSound(val);
    else if (key === 'dark') applyDarkMode(val);
    else if (key === 'lang') applyLang(val);
  };
  window.toggleSettingsPanel = function(force) {
    const panel = document.getElementById('settingsPanel');
    if (!panel) return;
    const willOpen = force === undefined ? panel.hasAttribute('hidden') : !!force;
    if (willOpen) {
      panel.removeAttribute('hidden');
      setTimeout(() => document.addEventListener('click', _settingsOutside, true), 0);
    } else {
      panel.setAttribute('hidden', '');
      document.removeEventListener('click', _settingsOutside, true);
    }
  };
  function _settingsOutside(ev) {
    const panel = document.getElementById('settingsPanel');
    if (!panel) return;
    // Не закрывать клик по самой панели или по кнопке настроек
    if (panel.contains(ev.target) || ev.target.closest('.side-settings')) return;
    panel.setAttribute('hidden', '');
    document.removeEventListener('click', _settingsOutside, true);
  }

  // ── Realtime notification toast (WS push) ─────────────────────
  function showNotifToast(payload) {
    notifBeep();
    try {
      let host = document.getElementById('notifToastHost');
      if (!host) {
        host = document.createElement('div');
        host.id = 'notifToastHost';
        host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none;';
        document.body.appendChild(host);
      }
      const t = document.createElement('div');
      const title = (payload && payload.title) || 'Уведомление';
      const body  = (payload && payload.body)  || '';
      const url   = (payload && payload.url)   || '';
      t.style.cssText = 'pointer-events:auto;background:#1d2330;color:#fff;padding:10px 14px;border-radius:10px;border:1px solid rgba(100,181,246,0.35);box-shadow:0 6px 24px rgba(0,0,0,.25);max-width:340px;font-size:13px;line-height:1.4;cursor:pointer;';
      t.innerHTML = '<div style="font-weight:600;margin-bottom:2px;">🔔 ' + esc(title) + '</div>' + (body ? '<div style="opacity:.85;">' + esc(body) + '</div>' : '');
      if (url) t.addEventListener('click', () => { try { location.href = url; } catch(e){} });
      host.appendChild(t);
      setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 320); }, 5000);
      // Bump bell badge + prepend to dropdown if user already opened it
      bumpBellBadge(+1);
      prependNotifItem(payload);
    } catch (e) { console.error('notif toast', e); }
  }

  // ── Notification bell + dropdown ──────────────────────────────
  const notif = { items: [], unread: 0, loaded: false, open: false };

  function setBellBadge(n) {
    notif.unread = Math.max(0, n|0);
    const el = document.getElementById('bellBadge');
    if (!el) return;
    if (notif.unread > 0) {
      el.textContent = notif.unread > 99 ? '99+' : String(notif.unread);
      el.style.display = '';
    } else {
      el.style.display = 'none';
    }
  }
  function bumpBellBadge(d) { setBellBadge(notif.unread + d); }

  function notifTimeAgo(iso) {
    if (!iso) return '';
    try {
      const t = new Date(iso).getTime();
      const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
      if (s < 60) return 'только что';
      if (s < 3600) return Math.floor(s/60) + ' мин';
      if (s < 86400) return Math.floor(s/3600) + ' ч';
      return Math.floor(s/86400) + ' д';
    } catch(e) { return ''; }
  }

  function renderNotifList() {
    const list = document.getElementById('notifList');
    if (!list) return;
    if (!notif.items.length) {
      list.innerHTML = '<div class="notif-empty">Нет уведомлений</div>';
      return;
    }
    list.innerHTML = notif.items.map(n =>
      '<div class="notif-item' + (n.is_read ? '' : ' unread') + '" data-id="' + n.id + '" data-url="' + esc(n.url || '') + '">' +
        '<div class="notif-row">' +
          '<span class="notif-kind ' + esc(n.kind || 'info') + '">' + esc(n.kind || 'info') + '</span>' +
          '<span class="notif-time">' + esc(notifTimeAgo(n.created_at)) + '</span>' +
        '</div>' +
        '<div class="notif-title">' + esc(n.title || '') + '</div>' +
        (n.body ? '<div class="notif-body">' + esc(n.body) + '</div>' : '') +
      '</div>'
    ).join('');
  }

  function prependNotifItem(payload) {
    if (!payload || !payload.id) return;
    // Drop existing copy by id (in case server replays)
    notif.items = (notif.items || []).filter(x => x.id !== payload.id);
    notif.items.unshift({
      id: payload.id, kind: payload.kind || 'info',
      title: payload.title || '', body: payload.body || '',
      url: payload.url || '', is_read: false,
      created_at: new Date().toISOString(),
    });
    if (notif.items.length > 50) notif.items.length = 50;
    if (notif.open) renderNotifList();
  }

  async function loadNotifications() {
    try {
      const data = await api('/api/assistant/notifications/?limit=20');
      notif.items = data.items || [];
      notif.loaded = true;
      setBellBadge(data.unread_count || 0);
      renderNotifList();
    } catch (e) { console.warn('loadNotifications failed', e); }
  }

  window.toggleNotifPanel = function() {
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    notif.open = panel.hasAttribute('hidden');
    if (notif.open) {
      panel.removeAttribute('hidden');
      if (!notif.loaded) loadNotifications(); else renderNotifList();
      // Close on outside click
      setTimeout(() => document.addEventListener('click', _notifOutside, true), 0);
    } else {
      panel.setAttribute('hidden', '');
      document.removeEventListener('click', _notifOutside, true);
    }
  };
  function _notifOutside(ev) {
    const panel = document.getElementById('notifPanel');
    const bell = document.getElementById('topBell');
    if (!panel || !bell) return;
    if (panel.contains(ev.target) || bell.contains(ev.target)) return;
    panel.setAttribute('hidden', '');
    notif.open = false;
    document.removeEventListener('click', _notifOutside, true);
  }

  async function markNotifRead(id) {
    try {
      const r = await api('/api/assistant/notifications/' + id + '/read/', {method:'POST', body: JSON.stringify({})});
      const it = notif.items.find(x => x.id === id);
      if (it) it.is_read = true;
      setBellBadge(r.unread_count || 0);
      renderNotifList();
    } catch (e) { console.warn('markNotifRead', e); }
  }

  window.markAllNotifsRead = async function() {
    try {
      await api('/api/assistant/notifications/read-all/', {method:'POST', body: JSON.stringify({})});
      notif.items.forEach(x => x.is_read = true);
      setBellBadge(0);
      renderNotifList();
    } catch (e) { console.warn('markAllNotifsRead', e); }
  };

  // Click on a notification row → mark read + navigate (if url given)
  document.addEventListener('click', (ev) => {
    const item = ev.target.closest && ev.target.closest('.notif-item');
    if (!item) return;
    const id = parseInt(item.dataset.id, 10);
    const url = item.dataset.url || '';
    if (id) markNotifRead(id);
    if (url) { try { location.href = url; } catch(e){} }
  });

  window.toggleSidebar = (force) => {
    const sb = $('sidebar');
    const open = force === undefined ? !sb.classList.contains('open') : force;
    sb.classList.toggle('open', open);
    if (!isMobile()) {
      try { localStorage.setItem(SB_KEY, open ? '1' : '0'); } catch(e){}
    }
  };

  function applyDefaultSidebar(hasHistory) {
    if (isMobile()) {
      // Mobile: always closed by default
      $('sidebar').classList.remove('open');
      return;
    }
    // Desktop: persisted preference, or open if user has history
    const saved = localStorage.getItem(SB_KEY);
    let open;
    if (saved === '1') open = true;
    else if (saved === '0') open = false;
    else open = hasHistory;  // first visit: open if history exists
    $('sidebar').classList.toggle('open', open);
  }

  // Click outside on mobile to close
  document.addEventListener('click', (e) => {
    if (!isMobile()) return;
    const sb = $('sidebar');
    if (!sb.classList.contains('open')) return;
    if (sb.contains(e.target) || e.target.closest('.top-burger')) return;
    sb.classList.remove('open');
  });

  // ══════════════════════════════════════════════════════════
  // State transitions: WELCOME ↔ CONV
  // ══════════════════════════════════════════════════════════
  function showConv() {
    $('welcomeStage').classList.add('hidden');
    $('convStage').classList.remove('hidden');
  }
  function showWelcome() {
    $('welcomeStage').classList.remove('hidden');
    $('convStage').classList.add('hidden');
    $('streamInner').innerHTML = '';
  }

  // 🏠 Home — возврат к welcome stage, сохраняем conversation
  window.goHome = () => {
    showWelcome();
    // Скрыть notif/settings панели если открыты
    const np = document.getElementById('notifPanel');
    if (np) np.setAttribute('hidden', '');
    const sp = document.getElementById('settingsPanel');
    if (sp) sp.setAttribute('hidden', '');
  };

  // ══════════════════════════════════════════════════════════
  // Card renderers
  // ══════════════════════════════════════════════════════════
  const renderers = {
    product(d) {
      return `<div class="card">
        <div class="card-row">
          <div class="card-emoji">⚙️</div>
          <div class="card-info">
            <div class="card-title">${esc(d.article || '')} — ${esc(d.name || d.title || '')}</div>
            <div class="card-sub">${esc(d.brand || '')}${d.country ? ' · ' + esc(d.country) : ''}${d.category ? ' · ' + esc(d.category) : ''}</div>
          </div>
          <div class="card-price">${fmtMoney(d.price, d.currency)}</div>
        </div>
        <div class="card-meta">
          ${d.in_stock !== false ? `<span class="card-chip card-chip-green">${d.quantity ? d.quantity + ' шт' : 'В наличии'}</span>` : '<span class="card-chip card-chip-gray">Нет в наличии</span>'}
          ${d.delivery_days ? `<span class="card-chip">${d.delivery_days} дн</span>` : ''}
          ${d.condition ? `<span class="card-chip card-chip-gray">${esc(d.condition)}</span>` : ''}
        </div>
      </div>`;
    },
    qr(d) {
      return `<div class="card qr-card">
        <div class="card-title">${esc(d.title || 'QR-код')}</div>
        ${d.subtitle ? `<div class="qr-sub">${esc(d.subtitle)}</div>` : ''}
        <div class="qr-img"><img src="${esc(d.image_url)}" alt="QR" loading="lazy"/></div>
        <div class="qr-payload">${esc(d.payload || '')}</div>
      </div>`;
    },
    price_breakdown(d) {
      const lines = (d.lines || []).map(l => {
        const sign = l.amount < 0 ? 'pb-neg' : '';
        return `<div class="pb-row ${sign}">
          <span class="pb-label">${esc(l.label)}</span>
          <span class="pb-amount">${fmtMoney(l.amount, d.currency || 'USD')}</span>
        </div>`;
      }).join('');
      const cur = d.currency || 'USD';
      return `<div class="card pb-card">
        <div class="card-title">${esc(d.title || 'Расчёт цены')}</div>
        <div class="pb-rows">${lines}</div>
        <div class="pb-total">
          <span class="pb-total-label">Итого клиенту</span>
          <span class="pb-total-amount">${fmtMoney(d.total, cur)}</span>
        </div>
      </div>`;
    },
    draft(d) {
      const rows = (d.rows || []).map(r =>
        `<div class="dr-row${r.primary ? ' dr-primary' : ''}">
          <span class="dr-label">${esc(r.label || '')}</span>
          <span class="dr-value">${esc(String(r.value ?? '—'))}</span>
        </div>`).join('');
      const warns = (d.warnings || []).map(w =>
        `<div class="dr-warning">⚠️ ${esc(w)}</div>`).join('');
      const confirmParams = JSON.stringify(d.confirm_params || {});
      return `<div class="card dr-card">
        <div class="dr-head">
          <span class="dr-badge">📝 Черновик</span>
          <span class="dr-title">${esc(d.title || 'Подтвердите действие')}</span>
        </div>
        <div class="dr-rows">${rows}</div>
        ${warns ? `<div class="dr-warnings">${warns}</div>` : ''}
        <div class="dr-actions">
          <button class="act-btn dr-confirm" data-action="${esc(d.confirm_action || '')}" data-params='${esc(confirmParams)}' data-label="${esc(d.confirm_label || 'Подтвердить')}">${esc(d.confirm_label || 'Подтвердить')}</button>
          <button class="act-btn dr-cancel" type="button" onclick="(() => { const c = this.closest('.dr-card'); c.outerHTML = '<div class=&quot;dr-cancelled-note&quot;>↩︎ Действие отменено</div>'; })()">${esc(d.cancel_label || 'Отмена')}</button>
        </div>
      </div>`;
    },
    inbox(d) {
      const sections = (d.sections || []).map(s => {
        const rows = (s.rows || []).map(r => {
          const a = r.action;
          const btn = a
            ? `<button class="act-btn ib-btn" data-action="${esc(a.action)}" data-params='${esc(JSON.stringify(a.params || {}))}' data-label="${esc(a.label)}">${esc(a.label)}</button>`
            : '';
          return `<div class="ib-row">
            <div class="ib-main">
              <div class="ib-title">${esc(r.title || '')}</div>
              <div class="ib-sub">${esc(r.subtitle || '')}</div>
            </div>
            ${btn}
          </div>`;
        }).join('');
        return `<div class="ib-section">
          <div class="ib-section-head">
            <span class="ib-section-icon">${esc(s.icon || '•')}</span>
            <span class="ib-section-title">${esc(s.title || '')}</span>
            <span class="ib-section-count">${(s.rows||[]).length}</span>
          </div>
          ${rows}
        </div>`;
      }).join('');
      return `<div class="card ib-card">
        <div class="card-title">${esc(d.title || 'Сегодня')}</div>
        ${sections}
      </div>`;
    },
    catalog(d) {
      const rows = (d.rows || []).map(r => {
        const status = r.is_active ? 'cat-active' : 'cat-archived';
        const stockBadge = r.stock_qty > 0
          ? `<span class="cat-chip cat-chip-green">${r.stock_qty} шт</span>`
          : '<span class="cat-chip cat-chip-gray">нет</span>';
        const sold = r.sold_qty
          ? `<span class="cat-chip cat-chip-blue">${r.sold_qty} продано</span>`
          : '';
        const rev = r.revenue ? `<span class="cat-chip cat-chip-gray">${fmtMoney(r.revenue, 'USD')}</span>` : '';
        const toggle = `<button class="act-btn cat-btn-mini" data-action="toggle_product" data-params='${esc(JSON.stringify({part_id: r.id}))}' data-label="Скрыть/показать">${r.is_active ? '🚫 Скрыть' : '✓ Активировать'}</button>`;
        return `<div class="cat-row ${status}">
          <div class="cat-row-main">
            <div class="cat-art">${esc(r.article || '')}</div>
            <div class="cat-name">${esc(r.title || '')}</div>
            <div class="cat-brand">${esc(r.brand || '')}</div>
          </div>
          <div class="cat-row-meta">
            <span class="cat-price">${fmtMoney(r.price, 'USD')}</span>
            ${stockBadge}${sold}${rev}
          </div>
          <div class="cat-row-actions">${toggle}</div>
        </div>`;
      }).join('');
      return `<div class="card cat-card">
        <div class="card-title">${esc(d.title || 'Каталог')}</div>
        <div class="cat-rows">${rows || '<div class="cat-empty">Пусто</div>'}</div>
      </div>`;
    },
    list(d) {
      const rows = (d.rows || []).map(r => {
        const badge = r.badge ? `<span class="ls-badge">${esc(r.badge)}</span>` : '';
        const cls = r.url ? 'ls-row ls-link' : 'ls-row';
        const open = r.url ? `onclick="window.open('${esc(r.url)}','_blank','noopener')"` : '';
        return `<div class="${cls}" ${open}>
          <div class="ls-main">
            <div class="ls-title">${esc(r.title || '')}</div>
            <div class="ls-sub">${esc(r.subtitle || '')}</div>
          </div>
          ${badge}
        </div>`;
      }).join('');
      return `<div class="card ls-card">
        <div class="card-title">${esc(d.title || 'Список')}</div>
        <div class="ls-rows">${rows || '<div class="ls-empty">Пусто</div>'}</div>
      </div>`;
    },
    kpi_grid(d) {
      const items = (d.kpis || []).map(k => `
        <div class="kpi-cell">
          <div class="kpi-value">${esc(String(k.value ?? '—'))}</div>
          <div class="kpi-label">${esc(k.label || '')}</div>
          ${k.sub ? `<div class="kpi-sub">${esc(k.sub)}</div>` : ''}
        </div>`).join('');
      return `<div class="card kpi-card">
        <div class="card-title">${esc(d.title || 'KPI')}</div>
        <div class="kpi-grid">${items}</div>
      </div>`;
    },
    form(d) {
      const fields = (d.fields || []).map(f => {
        const val = f.default || '';
        const req = f.required ? 'required' : '';
        return `<label class="fm-row">
          <span class="fm-label">${esc(f.label || f.name)}${f.required ? ' <span class="fm-req">*</span>' : ''}</span>
          <input class="fm-input" name="${esc(f.name)}" type="${esc(f.type || 'text')}" value="${esc(val)}" placeholder="${esc(f.placeholder || '')}" ${req} autocomplete="off"/>
        </label>`;
      }).join('');
      const fixed = JSON.stringify(d.fixed_params || {});
      return `<div class="card fm-card" data-form-action="${esc(d.submit_action || '')}" data-fixed='${esc(fixed)}'>
        <div class="card-title">${esc(d.title || 'Введите данные')}</div>
        <div class="fm-fields">${fields}</div>
        <div class="fm-actions">
          <button type="button" class="act-btn fm-submit">${esc(d.submit_label || 'Отправить')}</button>
        </div>
      </div>`;
    },
    seller_queue(d) {
      const sections = (d.sections || []).map(s => {
        const orders = (s.orders || []).map(o => {
          const items = (o.items || []).map(it =>
            `<div class="sq-item">
              <span class="sq-art">${esc(it.article)}</span>
              <span class="sq-name">${esc(it.name)}</span>
              <span class="sq-qty">×${it.qty}</span>
              <span class="sq-sub">${fmtMoney(it.subtotal, 'USD')}</span>
            </div>`
          ).join('');
          const btnAct = s.btn_action || 'advance_order';
          const btn = s.btn ? `<button class="act-btn sq-btn" data-action="${esc(btnAct)}" data-params='${esc(JSON.stringify({order_id: o.id}))}' data-label="${esc(s.btn + ' (#' + o.id + ')')}">${esc(s.btn)}</button>` : '';
          return `<div class="sq-order">
            <div class="sq-order-head">
              <div>
                <span class="sq-order-num">Заказ #${esc(o.id)}</span>
                <span class="sq-buyer">· ${esc(o.buyer || '')}</span>
              </div>
              <span class="sq-order-sub">${fmtMoney(o.subtotal, 'USD')}</span>
            </div>
            <div class="sq-items">${items}</div>
            ${btn ? `<div class="sq-actions">${btn}</div>` : ''}
          </div>`;
        }).join('');
        return `<div class="sq-section">
          <div class="sq-section-head">
            <span class="sq-section-label">${esc(s.label)}</span>
            <span class="sq-section-meta">${s.orders_count} зак. · ${s.items_count} поз. · ${fmtMoney(s.amount, 'USD')}</span>
          </div>
          ${orders}
        </div>`;
      }).join('');
      return `<div class="card sq-card">
        <div class="sq-head">
          <div class="card-title">📦 ${esc(d.title || 'Очередь продавца')}</div>
          <div class="sq-total">${d.total_orders} активных заказа(ов)</div>
        </div>
        ${sections || '<div class="sq-empty">Очередь пуста.</div>'}
      </div>`;
    },
    tracking(d) {
      const stages = (d.stages || []).map(s => {
        const cls = s.state === 'done' ? 'tk-done' : s.state === 'current' ? 'tk-current' : 'tk-pending';
        const dot = s.state === 'done' ? '●' : s.state === 'current' ? '◆' : '○';
        return `<div class="tk-stage ${cls}">
          <span class="tk-dot">${dot}</span>
          <span class="tk-label">${esc(s.label)}</span>
          ${s.eta ? `<span class="tk-eta">${esc(s.eta)}</span>` : ''}
        </div>`;
      }).join('');
      const tl = (d.timeline || []).map(t =>
        `<div class="tk-event"><span class="tk-when">${esc(t.when)}</span><span class="tk-text">${esc(t.text)}</span></div>`
      ).join('') || '<div class="tk-empty">Событий пока нет.</div>';
      const trackingLine = d.tracking_number
        ? `<div class="tk-tracking">📍 ${esc(d.carrier || 'Перевозчик')} · <span class="tk-track-num">${esc(d.tracking_number)}</span></div>`
        : '';
      const nextLine = d.next_event
        ? `<div class="tk-next">🔜 <b>${esc(d.next_actor || 'Дальше')}</b> ${esc(d.next_event)}</div>`
        : '';
      return `<div class="card tk-card">
        <div class="tk-head">
          <div>
            <div class="card-title">${esc(d.title || ('Заказ #' + d.order_id))}</div>
            <div class="card-sub">${esc(d.current_label || '')}</div>
            ${trackingLine}
          </div>
          <div class="tk-total">${fmtMoney(d.total, d.currency)}</div>
        </div>
        ${nextLine}
        <div class="tk-progress-wrap">
          <div class="tk-progress"><div class="tk-progress-fill" style="width:${d.progress_pct || 0}%"></div></div>
          <div class="tk-progress-meta">
            <span>${d.current_idx + 1} из ${d.total_stages}</span>
            <span>ETA: <b>${esc(d.eta_delivery || '—')}</b> · ${d.days_left || 0} дн.</span>
          </div>
        </div>
        <div class="tk-stages">${stages}</div>
        <div class="tk-tl-head">История</div>
        <div class="tk-timeline">${tl}</div>
      </div>`;
    },
    order(d) {
      const cls = ({pending:'orange', shipped:'green', completed:'green', cancelled:'gray'})[d.status_code] || '';
      const oid = d.id || d.number;
      const clickAttrs = oid
        ? ` data-action="track_order" data-params='${esc(JSON.stringify({order_id: parseInt(String(oid).replace(/\D/g, ''), 10) || oid}))}' role="button" tabindex="0" title="Открыть заказ"`
        : '';
      return `<div class="card card-clickable"${clickAttrs}>
        <div class="card-row">
          <div class="card-emoji">📦</div>
          <div class="card-info">
            <div class="card-title">${esc(d.number || ('Order #' + d.id))}</div>
            <div class="card-sub">${esc(d.customer || '')}${d.created_at ? ' · ' + esc(d.created_at) : ''}</div>
          </div>
          <div class="card-price">${fmtMoney(d.total, d.currency)}</div>
        </div>
        <div class="card-meta">
          <span class="card-chip card-chip-${cls}">${esc(d.status || '')}</span>
        </div>
      </div>`;
    },
    rfq(d) {
      // Клик по карточке = переход на /chat/rfq/<id>/ (полная RFQ-страница).
      // Чисто фронтовая навигация через data-href, без HTTP roundtrip.
      const rid = d.id || d.number;
      const href = rid ? `/chat/rfq/${parseInt(String(rid), 10) || rid}/` : '';
      const clickAttrs = href
        ? ` data-href="${esc(href)}" role="link" tabindex="0" title="Открыть RFQ"`
        : '';
      return `<div class="card card-clickable"${clickAttrs}>
        <div class="card-row">
          <div class="card-emoji">📋</div>
          <div class="card-info">
            <div class="card-title">RFQ #${esc(d.number || d.id)}</div>
            <div class="card-sub">${esc((d.description || '').substring(0,140))}</div>
          </div>
        </div>
        <div class="card-meta">
          <span class="card-chip">${esc(d.status || 'new')}</span>
          ${d.quantity ? `<span class="card-chip card-chip-gray">x ${d.quantity}</span>` : ''}
          ${d.created_at ? `<span class="card-chip card-chip-gray">${esc(d.created_at)}</span>` : ''}
        </div>
      </div>`;
    },
    shipment(d) {
      const stages = (d.stages || []).map(s =>
        `<div class="stage${s.done ? ' done' : ''}">${esc(s.label)}</div>`
      ).join('');
      return `<div class="card">
        <div class="card-row">
          <div class="card-emoji">🚢</div>
          <div class="card-info">
            <div class="card-title">Заказ ORD-${esc(d.order_id)}</div>
            <div class="card-sub">${esc(d.status_label || d.status || '')}</div>
          </div>
        </div>
        ${stages ? `<div class="stages">${stages}</div>` : ''}
      </div>`;
    },
    supplier(d) {
      return `<div class="card">
        <div class="card-row">
          <div class="card-emoji">🏭</div>
          <div class="card-info">
            <div class="card-title">${esc(d.name)}</div>
            <div class="card-sub">${d.kpi ? Object.entries(d.kpi).map(([k,v]) => `${k}: ${v}`).join(' · ') : ''}</div>
          </div>
        </div>
      </div>`;
    },
    comparison(d) {
      const headers = (d.headers || []).map(h => `<th>${esc(h)}</th>`).join('');
      const rows = (d.rows || []).map(r =>
        `<tr>${r.map(cell => `<td>${esc(String(cell))}</td>`).join('')}</tr>`
      ).join('');
      return `<div class="card"><table class="ctable"><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>`;
    },
    chart(d) {
      const items = d.items || [];
      const max = Math.max(...items.map(i => i.value || 0)) || 1;
      const bars = items.map(i =>
        `<div class="chart-bar" style="height:${(i.value/max*100)|0}%;${i.color ? 'background:'+i.color : ''}"><div class="chart-bar-label">${esc(i.label)}</div></div>`
      ).join('');
      return `<div class="card">
        <div class="card-title" style="margin-bottom:8px;">${esc(d.title || '')}</div>
        <div class="chart-bars">${bars}</div>
      </div>`;
    },
    file(d) {
      return `<div class="card">
        <div class="card-row">
          <div class="card-emoji">📎</div>
          <div class="card-info">
            <div class="card-title">${esc(d.name || 'Файл')}</div>
            <div class="card-sub">${esc(d.size || '')}</div>
          </div>
        </div>
      </div>`;
    },
    table(d) { return renderers.comparison(d); },

    // ── Spec results: KPIs + detailed table + footer ──
    spec_results(d) {
      const stkClass = (s) => ({in_stock:'in', backorder:'back', not_found:'no'})[s] || 'in';
      const stkLabel = (s) => ({in_stock:'В наличии', backorder:'Backorder', not_found:'—'})[s] || s;
      const condLabel = (c) => {
        if (c === 'oem') return '<span class="spec-cond-oem">OEM</span>';
        if (c === 'analogue') return '<span class="spec-cond-an">Аналог</span>';
        return esc(c || '');
      };
      const rows = (d.items || []).map((it, idx) => {
        if (it.status === 'not_found' && !it.id) {
          return `<tr><td><span class="spec-stk no"><span class="spec-stk-dot"></span>—</span></td>
            <td class="spec-row-num">${idx+1}</td>
            <td colspan="5" class="spec-empty-row" style="text-align:left;">— нет предложений —</td>
            <td>${esc(it.qty || '')}</td><td></td></tr>`;
        }
        return `<tr>
          <td><span class="spec-stk ${stkClass(it.status)}"><span class="spec-stk-dot"></span>${esc(stkLabel(it.status))}</span></td>
          <td class="spec-row-num">${idx+1}</td>
          <td><a class="spec-id-link">${esc(it.id || '')}</a></td>
          <td><div class="spec-name-cell"><span class="spec-name">${esc(it.name || '')}</span>${it.tag ? `<span class="spec-mini-tag">${esc(it.tag)}</span>` : ''}</div></td>
          <td>${esc(it.brand || '')}</td>
          <td>${condLabel(it.condition)}</td>
          <td class="spec-price">${fmtMoney(it.price, it.currency || 'USD')}</td>
          <td>${esc(it.qty || '')}</td>
          <td>${esc(it.weight || '')}</td>
        </tr>`;
      }).join('');

      const moreLink = d.more_count
        ? `<div class="spec-more">... ${d.more_count} ещё · <a href="#" onclick="return false;">раскрыть полный список</a></div>`
        : '';

      const found = d.found || 0;
      const analogue = d.analogue || 0;
      const notFound = d.not_found || 0;
      const offers = d.offers_count;
      const sellers = d.sellers_count;
      const bestMix = d.best_mix;

      const subParts = [];
      if (offers != null) subParts.push(`${offers} предложений`);
      if (sellers != null) subParts.push(`${sellers} поставщиков`);
      if (bestMix != null) subParts.push(`best mix ${fmtMoney(bestMix, d.currency || 'USD')}`);

      return `<div class="card spec">
        <div class="spec-head">
          <div class="spec-head-row">
            <div class="spec-title">${esc(d.title || 'Результаты подбора')}</div>
            <div class="spec-title-meta">${esc(subParts.join(' · '))}</div>
          </div>
        </div>
        <div class="spec-kpis">
          <div class="spec-kpi"><div class="spec-kpi-num green">${found}</div><div class="spec-kpi-label">Found</div></div>
          <div class="spec-kpi"><div class="spec-kpi-num amber">${analogue}</div><div class="spec-kpi-label">Analogue</div></div>
          <div class="spec-kpi"><div class="spec-kpi-num red">${notFound}</div><div class="spec-kpi-label">Not found</div></div>
        </div>
        <div class="spec-tbl-wrap">
          <table class="spec-tbl">
            <thead><tr>
              <th>Stock</th><th>#</th><th>ID</th><th>Name</th><th>Brand</th><th>Condition</th><th>Price</th><th>Qty</th><th>Weight</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        ${moreLink}
        <div class="spec-foot">
          <div class="spec-foot-info">${esc(d.foot_info || '')}</div>
          <div class="spec-foot-total">${d.total != null ? fmtMoney(d.total, d.currency || 'USD') : ''}</div>
        </div>
      </div>`;
    },

    // ── Supplier top: ranked list of 3 suppliers ──
    supplier_top(d) {
      const rows = (d.suppliers || []).map((s, idx) => {
        const rankClass = idx === 0 ? 'gold' : '';
        const stars = s.rating ? `<span class="stop-stars">★ ${esc(s.rating)}</span>` : '';
        return `<div class="stop-row">
          <div class="stop-rank ${rankClass}">${idx+1}</div>
          <div class="stop-info">
            <div><span class="stop-name">${esc(s.name)}</span>${stars}</div>
            <div class="stop-meta">${esc(s.coverage || '')}${s.lead_time ? ' · ср. лидтайм ' + esc(s.lead_time) : ''}${s.note ? ' · ' + esc(s.note) : ''}</div>
          </div>
          <div>
            <div class="stop-price-label">${esc(s.price_label || 'total')}</div>
            <div class="stop-price">${fmtMoney(s.total, s.currency || 'USD')}</div>
          </div>
        </div>`;
      }).join('');
      return `<div class="card stop">${rows}</div>`;
    },
  };

  function renderCards(cards) {
    if (!cards || !cards.length) return '';
    return '<div class="cards">' + cards.map(c => {
      const r = renderers[c.type];
      if (r) {
        try { return r(c.data || {}); }
        catch(err) {
          console.error('Card renderer crashed for', c.type, err, c.data);
          return renderUnknownCard(c.type, c.data);
        }
      }
      console.warn('No renderer for card type:', c.type, '— using fallback');
      return renderUnknownCard(c.type, c.data || {});
    }).join('') + '</div>';
  }

  // Fallback renderer for unknown/broken card types — dumps key-value pairs
  function renderUnknownCard(type, data) {
    const rows = Object.entries(data || {})
      .filter(([k, v]) => v != null && typeof v !== 'object')
      .map(([k, v]) => `<div style="display:flex;gap:8px;padding:3px 0;font-size:12px;"><span style="color:rgba(0,0,0,0.55);min-width:90px;">${esc(k)}:</span><span>${esc(String(v))}</span></div>`)
      .join('');
    return `<div class="card">
      <div class="card-title" style="margin-bottom:8px;">${esc(type)} <span style="font-weight:400;color:rgba(0,0,0,0.45);font-size:11px;">(обновите страницу — Cmd+Shift+R)</span></div>
      ${rows}
    </div>`;
  }

  function renderActions(actions) {
    if (!actions || !actions.length) return '';
    return '<div class="actions">' + actions.map(a =>
      `<button class="act-btn" data-action="${esc(a.action)}" data-params='${esc(JSON.stringify(a.params || {}))}' data-label="${esc(a.label)}">${esc(a.label)}</button>`
    ).join('') + '</div>';
  }

  // Brand mark SVG (8-facet asterisk from official Логобук)
  const STAR_SVG_WHITE = '<svg viewBox="0 0 74.1 74.1" fill="#fff"><polygon points="5.38 46.64 2.24 54.63 17.29 69.68 17.3 69.69 21.44 66.22 24.75 42.14 5.38 46.64"/><polygon points="21.44 66.22 24.87 74.1 46.16 74.1 46.64 68.72 31.95 49.35 21.44 66.22"/><polygon points="46.64 68.72 54.63 71.86 69.69 56.8 66.22 52.66 42.14 49.35 46.64 68.72"/><polygon points="68.71 27.45 71.86 19.47 56.8 4.41 52.65 7.87 49.35 31.95 68.71 27.45"/><polygon points="74.1 27.94 68.71 27.45 49.35 42.14 66.22 52.66 74.1 49.23 74.1 27.94"/><polygon points="52.65 7.87 49.23 0 27.93 0 27.45 5.38 42.14 24.75 52.65 7.87"/><polygon points="27.45 5.38 19.47 2.24 4.41 17.3 7.87 21.44 31.95 24.75 27.45 5.38"/><polygon points="7.87 21.44 0 24.87 0 46.16 5.38 46.64 24.75 31.95 7.87 21.44"/></svg>';
  const STAR_SVG_BLACK = STAR_SVG_WHITE.replace(/fill="#fff"/, 'fill="#1a1a1a"');
  const STAR_SVG = STAR_SVG_WHITE;  // default

  function avatar(role) {
    if (role === 'user') {
      const initial = ((state.config && state.config.user_name || 'U')[0] || 'U').toUpperCase();
      return `<div class="msg-avatar msg-avatar-user">${initial}</div>`;
    }
    if (role === 'action') return '<div class="msg-avatar msg-avatar-act">▸</div>';
    return `<div class="msg-avatar msg-avatar-bot">${STAR_SVG_BLACK}</div>`;
  }

  function authorLabel(role) {
    if (role === 'user') return state.config ? state.config.user_name : 'Вы';
    if (role === 'action') return 'Действие';
    return 'Consolidator';
  }

  // ══════════════════════════════════════════════════════════
  // Working indicator
  // ══════════════════════════════════════════════════════════
  const WORKING_MESSAGES = {
    search: ['Ищу в каталоге...', 'Подбираю варианты...', 'Проверяю наличие...', 'Сравниваю цены...'],
    rfq: ['Готовлю запрос...', 'Уведомляю поставщиков...', 'Создаю карточку RFQ...'],
    orders: ['Загружаю заказы...', 'Сортирую по дате...', 'Проверяю статусы...'],
    shipment: ['Запрашиваю трекинг...', 'Уточняю местоположение...', 'Считаю ETA...'],
    budget: ['Считаю расходы...', 'Группирую по статусам...', 'Готовлю отчёт...'],
    analytics: ['Собираю метрики...', 'Анализирую данные...', 'Формирую сводку...'],
    claim: ['Оформляю рекламацию...', 'Уведомляю поддержку...'],
    sla: ['Проверяю SLA...', 'Считаю нарушения...'],
    suppliers: ['Загружаю поставщиков...', 'Считаю рейтинги...'],
    default: ['Думаю...', 'Анализирую запрос...', 'Готовлю ответ...', 'Подбираю информацию...'],
  };

  function pickIntent(text) {
    const t = (text || '').toLowerCase();
    if (/(search|find_|искать|найти|подобрать|катал|запчаст|товар|оем|oem|brand|hydraulic|cylinder|filter)/i.test(t)) return 'search';
    if (/(rfq|котировк|запрос)/.test(t)) return 'rfq';
    if (/(order|заказ)/i.test(t)) return 'orders';
    if (/(shipment|track|трекинг|отгрузк|доставк)/.test(t)) return 'shipment';
    if (/(budget|бюджет|расход|оплат)/.test(t)) return 'budget';
    if (/(analytic|аналитик|отчёт|метрик)/.test(t)) return 'analytics';
    if (/(claim|рекламац|жалоб)/.test(t)) return 'claim';
    if (/(sla|просрочк)/.test(t)) return 'sla';
    if (/(supplier|поставщик|seller)/i.test(t)) return 'suppliers';
    return 'default';
  }

  let workingTimer = null;

  function addTyping(intentHint) {
    showConv();
    const intent = intentHint || 'default';
    const messages = WORKING_MESSAGES[intent] || WORKING_MESSAGES.default;
    const wrap = document.createElement('div');
    wrap.className = 'msg';
    wrap.id = 'typingMsg';
    wrap.innerHTML = `${avatar('assistant')}
      <div class="msg-body">
        <div class="working">
          <div class="working-logo">${STAR_SVG_BLACK}</div>
          <span class="working-text" id="workingText">${esc(messages[0])}</span>
        </div>
      </div>`;
    $('streamInner').appendChild(wrap);
    scrollBottom();

    let idx = 0;
    if (workingTimer) clearInterval(workingTimer);
    workingTimer = setInterval(() => {
      idx = (idx + 1) % messages.length;
      const el = $('workingText');
      if (!el) { clearInterval(workingTimer); workingTimer = null; return; }
      el.style.opacity = 0;
      setTimeout(() => { el.textContent = messages[idx]; el.style.opacity = 1; }, 200);
    }, 1800);
  }

  function removeTyping() {
    if (workingTimer) { clearInterval(workingTimer); workingTimer = null; }
    const t = $('typingMsg');
    if (t) t.remove();
  }

  // ══════════════════════════════════════════════════════════
  // Messages
  // ══════════════════════════════════════════════════════════
  function renderContextRefs(refs) {
    if (!refs || !refs.length) return '';
    const fileIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
    const items = refs.slice(0, 8).map(r => {
      const label = r.title || r.id || '—';
      const typeLabel = (r.type || '').toUpperCase();
      return `<span class="ctx-ref">${fileIcon}${typeLabel ? `<span class="ctx-ref-label">${esc(typeLabel)}</span>` : ''}${esc(label)}</span>`;
    }).join('');
    return `<div class="ctx-refs">${items}</div>`;
  }

  function addMessage(role, content, cards=[], actions=[], contextRefs=[], messageId=null, suggestions=[], contextualActions=[]) {
    showConv();
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-' + role;
    if (messageId) wrap.dataset.messageId = messageId;
    const isAi = role === 'assistant';
    wrap.innerHTML = `
      ${avatar(role)}
      <div class="msg-body">
        <div class="msg-author">${esc(authorLabel(role))}</div>
        <div class="msg-content${role === 'action' ? ' msg-action-tag' : ''}${isAi ? ' msg-content-ai' : ''}"></div>
        <div class="msg-refs"></div>
        <div class="msg-cards"></div>
        <div class="msg-actions"></div>
        <div class="msg-ctx-actions"></div>
        <div class="msg-suggestions"></div>
      </div>
    `;
    const cEl = wrap.querySelector('.msg-content');
    if (isAi && (content || '').trim()) {
      cEl.innerHTML = linkifyEntities(content || '');
      cEl.classList.add('msg-has-text');
    } else {
      cEl.textContent = content || '';
    }
    wrap.querySelector('.msg-refs').innerHTML = renderContextRefs(contextRefs);
    wrap.querySelector('.msg-cards').innerHTML = renderCards(cards);
    wrap.querySelector('.msg-actions').innerHTML = renderActions(actions);
    wrap.querySelector('.msg-ctx-actions').innerHTML = renderContextualActions(contextualActions);
    wrap.querySelector('.msg-suggestions').innerHTML = renderSuggestions(suggestions);
    $('streamInner').appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  function renderContextualActions(items) {
    if (!items || !items.length) return '';
    const btns = items.map(a =>
      `<button class="act-btn ctx-btn" data-action="${esc(a.action)}" data-params='${esc(JSON.stringify(a.params || {}))}' data-label="${esc(a.label)}">${esc(a.label)}</button>`
    ).join('');
    return `<div class="ctx-row">
      <span class="ctx-label">💡 Также можете:</span>
      ${btns}
    </div>`;
  }

  // Превращает упоминания сущностей в кликабельные ссылки на карточки.
  // Поддерживаемые форматы:
  //   «заказ #123», «#ORD-123», «order #123»  → track_order(123)
  //   «RFQ #45», «RFQ-45»                       → rfq_detail / get_rfq_status
  function linkifyEntities(text) {
    let html = esc(text);
    // Заказ #N — самое частое
    html = html.replace(/(?<![\w-])(заказ|order|зак\.)\s*#?\s*(\d{1,7})\b/gi,
      (full, kw, id) => `<span class="entity-link" data-action="track_order" data-params='{"order_id":${id}}'>${full}</span>`);
    // RFQ #N
    html = html.replace(/(?<![\w-])RFQ\s*[#-]?\s*(\d{1,7})\b/gi,
      (full, id) => `<span class="entity-link" data-action="rfq_detail" data-params='{"rfq_id":${id}}'>${full}</span>`);
    // Просто #N — последний фолбек, если идёт сразу после слов «заказ/order» уже обработано
    return html;
  }

  // Делегируем клик по clickable card → 1) data-href навигация, 2) data-action quickAction
  document.addEventListener('click', (e) => {
    // 1. Чистая навигация (RFQ карточки и т.п.)
    const navCard = e.target.closest('.card-clickable[data-href]');
    if (navCard && navCard.dataset.href) {
      window.location.href = navCard.dataset.href;
      return;
    }
    // 2. Action-карточки (order → track_order и т.п.)
    const target = e.target.closest('.entity-link, .card-clickable[data-action]');
    if (!target) return;
    const action = target.dataset.action;
    if (!action) return;
    let params = {};
    try { params = JSON.parse(target.dataset.params || '{}'); } catch(_){}
    params._label = (target.querySelector('.card-title')?.textContent || target.textContent || '').trim().slice(0, 80);
    if (typeof quickAction === 'function') quickAction(action, params);
  });
  // Поддержка клавиатуры (Enter/Space) для clickable cards
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const target = e.target.closest && e.target.closest('.card-clickable[data-action], .card-clickable[data-href]');
    if (!target) return;
    e.preventDefault();
    target.click();
  });

  function renderSuggestions(suggestions) {
    if (!suggestions || !suggestions.length) return '';
    const chips = suggestions.map(s =>
      `<button class="sg-chip" type="button" onclick="window.heroQuick && window.heroQuick(${JSON.stringify(s).replace(/"/g, '&quot;')})">${esc(s)}</button>`
    ).join('');
    return `<div class="sg-row">
      <span class="sg-label">💡 Также можете:</span>
      ${chips}
    </div>`;
  }

  // Положить текст в input при клике на chip
  window.heroQuick = (text) => {
    const target = $('welcomeStage').classList.contains('hidden') ? $('input') : $('heroInput');
    if (target) {
      target.value = text;
      target.focus();
    }
  };

  function appendStream(text) {
    if (!state.currentBubble) {
      removeTyping();
      const wrap = document.createElement('div');
      wrap.className = 'msg msg-assistant';
      wrap.innerHTML = `${avatar('assistant')}<div class="msg-body"><div class="msg-author">Consolidator</div><div class="msg-content msg-content-ai"></div><div class="msg-refs"></div><div class="msg-cards"></div><div class="msg-actions"></div><div class="msg-ctx-actions"></div><div class="msg-suggestions"></div></div>`;
      $('streamInner').appendChild(wrap);
      state.currentBubble = wrap;
    }
    const el = state.currentBubble.querySelector('.msg-content');
    el.textContent += text;
    if ((el.textContent || '').trim()) el.classList.add('msg-has-text');
    scrollBottom();
  }

  function finishStream(cards, actions, refs, authoritativeText, contextualActions, suggestions) {
    removeTyping();
    if (!state.currentBubble) return;
    const contentEl = state.currentBubble.querySelector('.msg-content');
    let finalText;
    if (authoritativeText != null) {
      finalText = authoritativeText;
    } else {
      finalText = (contentEl.textContent || '')
        .replace(/\[card:\w+\]/g, '')
        .replace(/:::(?:actions|product|rfq|order|shipment|supplier|comparison|chart|file|table|spec_results|supplier_top)[\s\S]*?:::/g, '')
        .trim();
    }
    if (finalText) {
      contentEl.innerHTML = linkifyEntities(finalText);
      contentEl.classList.add('msg-has-text');
    } else {
      contentEl.textContent = '';
    }
    state.currentBubble.querySelector('.msg-refs').innerHTML = renderContextRefs(refs || []);
    state.currentBubble.querySelector('.msg-cards').innerHTML = renderCards(cards);
    state.currentBubble.querySelector('.msg-actions').innerHTML = renderActions(actions);
    const ctxEl = state.currentBubble.querySelector('.msg-ctx-actions');
    if (ctxEl) ctxEl.innerHTML = renderContextualActions(contextualActions || []);
    const sgEl = state.currentBubble.querySelector('.msg-suggestions');
    if (sgEl) sgEl.innerHTML = renderSuggestions(suggestions || []);
    state.currentBubble = null;
    state.streaming = false;
    $('sendBtn').disabled = false;
    $('heroSendBtn').disabled = false;
  }

  function scrollBottom() {
    setTimeout(() => {
      const s = $('stream');
      if (s) s.scrollTop = s.scrollHeight;
    }, 30);
  }

  // ══════════════════════════════════════════════════════════
  // WebSocket
  // ══════════════════════════════════════════════════════════
  function connectWS() {
    if (state.ws && state.ws.readyState <= 1) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const path = state.convId ? `/ws/assistant/${state.convId}/` : '/ws/assistant/';
    try { state.ws = new WebSocket(proto + '//' + location.host + path); } catch(e) { return; }

    state.ws.onopen = () => { state.wsRetry = 0; };
    state.ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type === 'connected') {
          setConvId(d.conversation_id);
          loadConvList();
        } else if (d.type === 'thinking') {
          if (!$('typingMsg')) addTyping(state._intent);
        } else if (d.type === 'stream') {
          removeTyping();
          appendStream(d.content);
        } else if (d.type === 'context') {
          state._lastRefs = d.refs || [];
        } else if (d.type === 'cards') {
          state._lastCards = d.cards || [];
          state._lastActions = d.actions || [];
          state._lastCtxActions = d.contextual_actions || [];
          state._lastSuggestions = d.suggestions || [];
          state._lastText = d.text;
        } else if (d.type === 'done') {
          // Auto-attach «🏠 Главная» если backend не дал свою навигацию
          const ctxActs = ensureHomeNav(state._lastCtxActions || []);
          finishStream(state._lastCards, state._lastActions, state._lastRefs || d.refs, state._lastText, ctxActs, state._lastSuggestions);
          state._lastCards = []; state._lastActions = []; state._lastRefs = []; state._lastText = null;
          state._lastCtxActions = []; state._lastSuggestions = [];
        } else if (d.type === 'error') {
          finishStream([], []);
          addMessage('assistant', '⚠️ ' + d.message);
        } else if (d.type === 'notification') {
          showNotifToast(d.payload || {});
        }
      } catch(e){ console.error(e); }
    };
    state.ws.onclose = (ev) => {
      if (ev.code === 4401) return;
      state.wsRetry++;
      const delay = Math.min(1000 * Math.pow(2, state.wsRetry), 30000);
      setTimeout(connectWS, delay);
    };
  }

  // ══════════════════════════════════════════════════════════
  // Send & actions
  // ══════════════════════════════════════════════════════════
  async function send(fromHero) {
    const inp = fromHero ? $('heroInput') : $('input');
    const text = inp.value.trim();
    if (!text || state.streaming) return;

    const intent = pickIntent(text);
    state._intent = intent;
    addMessage('user', text);
    inp.value = '';
    inp.style.height = 'auto';
    state.streaming = true;
    $('sendBtn').disabled = true;
    $('heroSendBtn').disabled = true;
    setTimeout(() => $('input').focus(), 100);

    if (state.ws && state.ws.readyState === 1) {
      addTyping(intent);
      state.ws.send(JSON.stringify({type:'message', content:text}));
    } else {
      addTyping(intent);
      try {
        const r = await api('/api/assistant/chat/', {
          method:'POST',
          body: JSON.stringify({conversation_id: state.convId, message: text}),
        });
        removeTyping();
        setConvId(r.conversation_id);
        addMessage('assistant', r.response, r.cards, r.actions, r.context_refs || [], r.message_id || null, r.suggestions || [], r.contextual_actions || []);
        state.streaming = false;
        $('sendBtn').disabled = false;
        $('heroSendBtn').disabled = false;
        loadConvList();
      } catch(e) {
        removeTyping();
        addMessage('assistant', '⚠️ ' + e.message);
        state.streaming = false;
        $('sendBtn').disabled = false;
        $('heroSendBtn').disabled = false;
      }
    }
  }

  // Hero button: send if input has text, else voice
  window.heroAction = () => {
    const text = $('heroInput').value.trim();
    if (text) send(true);
    else toggleVoice();
  };

  // Update hero button icon based on input
  function updateHeroIcon() {
    const text = $('heroInput').value.trim();
    const btn = $('heroSendBtn');
    if (text) {
      btn.classList.add('send');
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
    } else {
      btn.classList.remove('send');
      btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2M12 19v4M8 23h8"/></svg>';
    }
  }

  // Whitelist «фастовых» actions — просто DB read, без AI/RAG/external API.
  // Для них спиннер вообще не показывается: открытие должно быть моментальным.
  // Если action не в этом списке (search_parts с эмбеддингом, generate_proposal,
  // analyze_spec, kb_search) — спиннер появится через 600ms.
  const FAST_ACTIONS = new Set([
    // Reads
    'get_orders', 'get_order_detail', 'track_order', 'track_shipment',
    'get_rfq_status', 'rfq_detail', 'view_rfq_quotes', 'view_quote',
    'get_balance', 'get_budget', 'get_analytics', 'get_demand_report',
    'get_sla_report', 'get_claims',
    'compare_products', 'compare_suppliers', 'top_suppliers',
    // Seller cabinet reads
    'seller_dashboard', 'seller_finance', 'seller_rating', 'seller_pipeline',
    'seller_inbox', 'seller_catalog', 'seller_drawings', 'seller_team',
    'seller_integrations', 'seller_reports', 'seller_qr', 'seller_logistics',
    'seller_negotiations',
    // Operator/admin reads
    'op_dashboard', 'op_queue', 'op_sla_breach', 'op_order_detail',
    'op_logistics_stats', 'op_payments_stats', 'op_payments_dashboard',
    'op_kyb_queue', 'op_kyb_review',
    'op_customs_dashboard', 'op_hs_lookup', 'op_calc_duty',
    'op_certs_check', 'op_sanctions_check',
    'admin_dashboard', 'admin_gmv', 'admin_users', 'admin_user_detail',
    'admin_moderation_queue', 'admin_catalog_review', 'admin_platform_settings',
    // Onboarding wizard step rendering
    'start_onboarding', 'kyb_status',
    'submit_company_info', 'submit_legal_address',
    'submit_bank', 'submit_director', 'submit_for_review',
    // Notification settings
    'notif_prefs', 'notif_set_email', 'notif_set_kinds', 'notif_link_telegram',
    // Auth
    'list_api_tokens',
    // Open-card preview steps (DraftCard step1)
    'pay_reserve', 'pay_final', 'confirm_delivery',
    'op_assign', 'op_add_note', 'op_resolve_dispute',
    'op_hs_assign', 'op_cert_upload', 'op_customs_release',
    'admin_ban_user', 'admin_unban_user', 'admin_change_role',
    'create_api_token', 'revoke_api_token',
    'setup_2fa', 'verify_2fa', 'disable_2fa',
    'submit_quote', 'respond_to_counter', 'mark_quote_final',
    'accept_quote', 'counter_offer', 'decline_quote', 'send_rfq_to_suppliers',
    // Operator misc
    'audit_log', 'notifications', 'generate_qr',
    // Misc
    'open_url', 'topup_wallet',
  ]);

  // Quick action from pills/cards
  window.quickAction = async (action, params) => {
    params = params || {};
    params._label = params._label || action;
    // Navigation shortcut: open URL directly (no AI round-trip, no new chat).
    // Sources of _url:
    //   1. Explicit params._url (e.g. "Перейти в кабинет")
    //   2. Backward-compat: legacy "Открыть RFQ"/"Открыть заказ" buttons that
    //      pre-date this fix shipped without _url. Synthesize one from the id
    //      and the action's label, so old chat history keeps working.
    // Navigation: только internal /chat/* — все «старые» URL (cabinet) игнорируются,
    // вся работа идёт внутри chat-first.
    let url = params._url;
    if (url) {
      url = url
        .replace(/^\/buyer\/rfqs\/(\d+)\/?$/, '/chat/rfq/$1/')
        .replace(/^\/rfq\/(\d+)\/?$/, '/chat/rfq/$1/')
        .replace(/^\/buyer\/orders\/(\d+)\/?$/, '/chat/')
        .replace(/^\/seller\/rfqs\/(\d+)\/?$/, '/chat/rfq/$1/');
      if (url.startsWith('/chat/')) {
        window.location.href = url;
        return;
      }
      // Все не-/chat/ URL — это или PDF/файлы, или внешка. Открываем в новой вкладке,
      // чтобы пользователь не уходил из чата.
      const isFile = /\.(pdf|xlsx?|csv|docx?|zip|png|jpe?g)(\?|$)/i.test(url);
      if (isFile) { window.open(url, '_blank', 'noopener'); return; }
      // Иначе — не уходим, превращаем в обычный action call (если action есть).
      if (!action) return;
    }
    // Не пишем ярлык кнопки в чат — это UI affordance, а не сообщение юзера.
    // Открываем conv view (чтобы welcome-stage не моргал).
    //
    // Спиннер: для фастовых actions (просто DB read, без AI) — не показываем
    // вообще. Для AI-actions (search_parts с embedding, generate_proposal,
    // analyze_spec) — после 600ms.
    showConv();
    const isFast = FAST_ACTIONS.has(action);
    const typingDelay = isFast ? null : setTimeout(() => addTyping(pickIntent(action)), 600);
    try {
      const r = await api('/api/assistant/action/', {
        method:'POST',
        body: JSON.stringify({conversation_id: state.convId, action, params}),
      });
      if (typingDelay) clearTimeout(typingDelay);
      removeTyping();
      setConvId(r.conversation_id || state.convId);
      // Auto-add «🏠 Главная» в contextual_actions если бэкенд её не вернул
      const ctxActs = ensureHomeNav(r.contextual_actions || []);
      addMessage('assistant', r.text, r.cards, r.actions, r.context_refs || [], r.message_id || null, r.suggestions || [], ctxActs);
      loadConvList();
    } catch(err) {
      if (typingDelay) clearTimeout(typingDelay);
      removeTyping();
      addMessage('assistant', '⚠️ ' + err.message);
    }
  };

  // Добавить «🏠 Главная» в contextual_actions если нет своей навигации.
  // Helper для quickAction и WS-handler.
  function ensureHomeNav(ctxActs) {
    const hasHome = ctxActs.some(a =>
      a.action === 'go_home'
      || (a.label || '').includes('Главная')
      || (a.label || '').startsWith('🏠')
    );
    if (hasHome) return ctxActs;
    return [...ctxActs, {action: 'go_home', label: '🏠 Главная'}];
  }
  // Special action handler: go_home — без round-trip к серверу
  // Подменяем quickAction для этого случая.
  const _origQuickAction = window.quickAction;
  window.quickAction = (action, params) => {
    if (action === 'go_home') { goHome(); return; }
    return _origQuickAction(action, params);
  };

  // Click handler for action buttons inside messages
  document.addEventListener('click', async (e) => {
    // 1. Submit-кнопка inline-формы (карточка type=form)
    const submit = e.target.closest('.fm-submit');
    if (submit) {
      const card = submit.closest('.fm-card');
      if (!card) return;
      const action = card.dataset.formAction;
      const fixed = JSON.parse(card.dataset.fixed || '{}');
      const params = {...fixed};
      card.querySelectorAll('.fm-input').forEach(inp => {
        if (inp.required && !inp.value.trim()) inp.classList.add('fm-error');
        else inp.classList.remove('fm-error');
        if (inp.value.trim()) params[inp.name] = inp.value.trim();
      });
      const missing = card.querySelectorAll('.fm-input.fm-error').length;
      if (missing) return;
      submit.disabled = true;
      submit.textContent = '…';
      params._label = card.querySelector('.card-title')?.textContent || action;
      quickAction(action, params);
      return;
    }
    // 2. Обычные action-кнопки
    const btn = e.target.closest('.act-btn');
    if (!btn) return;
    const action = btn.dataset.action;
    const params = JSON.parse(btn.dataset.params || '{}');
    params._label = btn.dataset.label;
    quickAction(action, params);
  });

  // ══════════════════════════════════════════════════════════
  // Sidebar conversations + projects
  // ══════════════════════════════════════════════════════════
  const DOT_BG = {
    green:'#22c55e', orange:'#f97316', blue:'#3b82f6',
    purple:'#a855f7', red:'#ef4444', gray:'#9ca3af',
  };

  async function loadConvList() {
    try {
      const r = await fetch('/api/assistant/conversations/');
      const data = await r.json();
      state.convs = data.results || data;
      renderConvList();
    } catch(e){}
  }

  // ── Role toggle (Покупатель / Поставщик / Оператор) ───────
  const ROLE_TABS = ['buyer', 'seller', 'operator'];

  function paintRoleToggle(activeRole) {
    document.querySelectorAll('#roleToggle .role-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.role === activeRole);
    });
  }

  async function setRole(newRole) {
    paintRoleToggle(newRole);
    try {
      const r = await fetch('/api/assistant/role/', {
        method: 'POST',
        headers: {'Content-Type':'application/json','X-CSRFToken': csrf()},
        credentials: 'same-origin',
        body: JSON.stringify({role: newRole}),
      });
      const data = await r.json();
      const role = data.role || newRole;
      state.config = {...(state.config || {}), role};
      applyRoleWelcome(role);
      // Сбрасываем активную беседу — новая роль = новый сценарий
      setConvId(null);
      showWelcome();
      if (state.ws) { try { state.ws.close(); } catch(e){} }
    } catch (err) {
      console.warn('role switch failed', err);
    }
  }

  document.addEventListener('click', (e) => {
    const tab = e.target.closest('#roleToggle .role-tab');
    if (!tab) return;
    const newRole = tab.dataset.role;
    if (!ROLE_TABS.includes(newRole)) return;
    if (state.config && state.config.role === newRole) return;
    setRole(newRole);
  });

  // Welcome screen + quick-pills адаптивны под роль
  const ROLE_WELCOME = {
    buyer: {
      title:    'Какую запчасть найти?',
      subtitle: 'Загрузите спецификацию в Excel, перетащите фото детали или опишите словами — соберу предложения от <strong>200+ поставщиков</strong>.',
      pills: [
        {label:'📦 Мои заказы',      action:'get_orders',     params:{}},
        {label:'📋 Открытые RFQ',    action:'get_rfq_status', params:{}},
        {label:'💰 Баланс депозита', action:'get_balance',    params:{}},
      ],
    },
    seller: {
      title:    'Что в работе сегодня?',
      subtitle: 'Срочные задачи, входящие RFQ и отгрузки. Каталог, финансы и команда — по запросу.',
      pills: [
        {label:'🛡 Верификация',     action:'start_onboarding',  params:{}},
        {label:'🔥 Срочное',         action:'seller_inbox',      params:{}},
        {label:'🚚 К отгрузке',      action:'seller_pipeline',   params:{}},
        {label:'📋 Новые RFQ',       action:'get_rfq_status',    params:{}},
        {label:'📈 Спрос',           action:'get_demand_report', params:{}},
      ],
    },
    operator: {
      title:    'Что в работе на платформе?',
      subtitle: 'Контролируйте процесс: активные заказы, SLA-нарушения, очередь, спор-кейсы.',
      pills: [
        {label:'🎛 Сводка',          action:'op_dashboard',     params:{}},
        {label:'🛡 KYB на проверке', action:'op_kyb_queue',     params:{}},
        {label:'📋 Очередь',         action:'op_queue',         params:{}},
        {label:'⏱ SLA-нарушения',   action:'op_sla_breach',    params:{}},
        {label:'📈 Аналитика',       action:'get_analytics',    params:{}},
      ],
    },
    operator_logist: {
      title:    'Логистика',
      subtitle: 'Отгрузки, контейнеры, SLA — управляйте через чат.',
      pills: [
        {label:'🚚 Аналитика',       action:'op_logistics_stats', params:{}},
        {label:'🎛 Сводка',          action:'op_dashboard',       params:{}},
        {label:'📋 Очередь',         action:'op_queue',           params:{filter:'open'}},
        {label:'⏱ SLA-нарушения',   action:'op_sla_breach',      params:{}},
      ],
    },
    operator_customs: {
      title:    'Таможня',
      subtitle: 'Грузы под растаможкой, ТН ВЭД, документы, санкционный скрининг.',
      pills: [
        {label:'🛂 Сводка таможни',  action:'op_customs_dashboard', params:{}},
        {label:'🔎 ТН ВЭД',           action:'op_hs_lookup',         params:{}},
        {label:'🚫 Санкции',          action:'op_sanctions_check',   params:{}},
        {label:'📋 На таможне',       action:'op_queue',             params:{filter:'open'}},
      ],
    },
    operator_payment: {
      title:    'Платежи',
      subtitle: 'Инвойсы, эскроу, возвраты — управляйте через чат.',
      pills: [
        {label:'💰 Эскроу',          action:'op_payments_dashboard', params:{}},
        {label:'💳 Аналитика',       action:'op_payments_stats',     params:{}},
        {label:'⏳ Ожидают резерва', action:'op_queue',              params:{filter:'awaiting_reserve'}},
        {label:'💸 Возвраты',        action:'op_queue',              params:{filter:'refund'}},
      ],
    },
    operator_manager: {
      title:    'Менеджмент',
      subtitle: 'Конверсия RFQ, топ-клиенты, KPI команды.',
      pills: [
        {label:'🎛 Сводка',          action:'op_dashboard',  params:{}},
        {label:'📋 Очередь',         action:'op_queue',      params:{}},
        {label:'📈 Аналитика',       action:'get_analytics', params:{}},
      ],
    },
    admin: {
      title:    'Платформа',
      subtitle: 'GMV, пользователи, модерация — управление всей площадкой.',
      pills: [
        {label:'🛡 Сводка',           action:'admin_dashboard',         params:{}},
        {label:'📈 GMV',              action:'admin_gmv',               params:{}},
        {label:'👥 Пользователи',     action:'admin_users',             params:{}},
        {label:'🚨 Модерация',        action:'admin_moderation_queue',  params:{}},
        {label:'📦 Каталог',          action:'admin_catalog_review',    params:{}},
        {label:'🛠 Settings',         action:'admin_platform_settings', params:{}},
      ],
    },
  };

  function applyRoleWelcome(role) {
    const cfg = ROLE_WELCOME[role] || ROLE_WELCOME.buyer;
    const t = $('welcomeTitle'), s = $('welcomeSubtitle'), p = $('welcomePills');
    if (t) t.textContent = cfg.title;
    if (s) s.innerHTML = cfg.subtitle;
    if (p) p.innerHTML = cfg.pills.map(b => {
      // Передаём label в params._label чтобы breadcrumb показывал «Мои заказы»,
      // а не raw action name.
      const params = {...(b.params || {}), _label: b.label};
      return `<button class="pill" type="button"
        onclick='quickAction(${JSON.stringify(b.action)}, ${JSON.stringify(params)})'>
        ${esc(b.label)}
      </button>`;
    }).join('');
  }

  async function loadProjects() {
    const el = $('projectsList');
    if (!el) return;
    try {
      const data = await api('/api/assistant/projects/');
      const list = data.projects || [];
      if (!list.length) {
        el.innerHTML = `<div class="side-item" style="color:rgba(0,0,0,0.4);">Нет проектов</div>`;
        return;
      }
      el.innerHTML = list.map(p => {
        const dot = DOT_BG[p.dot_color] || DOT_BG.green;
        return `<a href="/chat/project/${esc(p.id)}/" class="side-item" style="text-decoration:none;">
          <span class="side-item-dot" style="background:${dot};"></span>
          <span class="side-item-text">${esc(p.name)}</span>
          <span class="side-item-meta">${esc(p.chats || 0)}</span>
        </a>`;
      }).join('');
    } catch(e){
      // leave demo items as fallback
    }
  }

  function renderConvList(filter='') {
    const f = filter.toLowerCase();
    const list = state.convs.filter(c => !f || (c.title||'').toLowerCase().includes(f));
    if (!list.length) {
      $('convList').innerHTML = '<div class="side-item-stack"><div class="side-item-stack-meta">Нет чатов</div></div>';
      return;
    }
    $('convList').innerHTML = list.slice(0, 30).map(c => {
      const date = c.updated_at ? new Date(c.updated_at) : null;
      const meta = date ? relativeTime(date) : '';
      const lastMeta = c.last_message ? c.last_message.content.substring(0, 40) : meta;
      return `<div class="side-item-stack ${c.id === state.convId ? 'active' : ''}" onclick="openConv('${c.id}')">
        <div class="side-item-stack-title">${esc(c.title || 'Без названия')}</div>
        <div class="side-item-stack-meta">${esc(meta)} ${lastMeta && lastMeta !== meta ? '· ' + esc(lastMeta) : ''}</div>
      </div>`;
    }).join('');
  }

  function relativeTime(date) {
    const now = new Date();
    const diff = (now - date) / 1000;
    if (diff < 60) return 'только что';
    if (diff < 3600) return Math.floor(diff/60) + ' мин назад';
    if (diff < 86400) return Math.floor(diff/3600) + ' ч назад';
    if (diff < 604800) return Math.floor(diff/86400) + ' дн назад';
    return date.toLocaleDateString('ru-RU', {day:'2-digit', month:'short'});
  }

  window.filterConvs = renderConvList;

  window.openConv = async (id) => {
    setConvId(id);
    showConv();
    $('streamInner').innerHTML = '';
    if (state.ws) { try { state.ws.close(); } catch(e){} }
    try {
      const data = await api('/api/assistant/conversations/' + id + '/');
      (data.messages || []).forEach(m => addMessage(m.role, m.content, m.cards, m.actions, m.context_refs));
    } catch(e){}
    connectWS();
    renderConvList($('convSearch').value);
    if (isMobile()) toggleSidebar(false);
  };

  window.newChat = () => {
    setConvId(null);
    showWelcome();
    if (state.ws) { try { state.ws.close(); } catch(e){} }
    connectWS();
    renderConvList();
    if (isMobile()) toggleSidebar(false);
    setTimeout(() => $('heroInput').focus(), 100);
  };

  // ══════════════════════════════════════════════════════════
  // Voice + file
  // ══════════════════════════════════════════════════════════
  let recog = null;
  let mediaRec = null;
  let recordedChunks = [];

  window.toggleVoice = async () => {
    // Если уже идёт серверная запись — остановить и отправить
    if (mediaRec && mediaRec.state === 'recording') {
      mediaRec.stop();
      return;
    }
    // Web Speech API — если есть, используем (бесплатно, on-device)
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) {
      if (recog) { recog.stop(); recog = null; return; }
      recog = new SR();
      recog.lang = document.documentElement.lang === 'en' ? 'en-US' : 'ru-RU';
      recog.interimResults = true;
      recog.onresult = (e) => {
        const text = Array.from(e.results).map(r => r[0].transcript).join('');
        const target = $('welcomeStage').classList.contains('hidden') ? $('input') : $('heroInput');
        target.value = text;
        if (typeof updateHeroIcon === 'function') updateHeroIcon();
      };
      recog.onend = () => { recog = null; };
      recog.start();
      return;
    }
    // Fallback: пишем через MediaRecorder и шлём на сервер для Whisper
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      alert('Голосовой ввод не поддерживается этим браузером');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      mediaRec = new MediaRecorder(stream, {mimeType: 'audio/webm'});
      recordedChunks = [];
      mediaRec.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
      mediaRec.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        const blob = new Blob(recordedChunks, {type: 'audio/webm'});
        recordedChunks = [];
        const fd = new FormData();
        fd.append('audio', blob, 'voice.webm');
        try {
          const res = await fetch('/api/assistant/transcribe-audio/', {
            method: 'POST',
            headers: {'X-CSRFToken': csrf()},
            body: fd, credentials: 'same-origin',
          });
          const d = await res.json();
          if (d.error && !d.text) {
            alert(d.error);
            return;
          }
          const target = $('welcomeStage').classList.contains('hidden') ? $('input') : $('heroInput');
          target.value = d.text || '';
          if (typeof updateHeroIcon === 'function') updateHeroIcon();
        } catch(err) {
          alert('Не удалось расшифровать: ' + err.message);
        }
      };
      mediaRec.start();
    } catch(err) {
      alert('Доступ к микрофону отклонён: ' + (err.message || err));
    }
  };

  async function uploadSpec(file) {
    showConv();
    addMessage('user', '📎 ' + file.name + ' (' + Math.round(file.size/1024) + ' KB)');
    const pending = addMessage('assistant', 'Парсю файл и ищу артикулы в каталоге…');
    try {
      const fd = new FormData();
      fd.append('file', file);
      if (state.convId) fd.append('conversation_id', state.convId);
      const res = await fetch('/api/assistant/upload-spec/', {
        method: 'POST',
        headers: {'X-CSRFToken': csrf()},
        body: fd,
        credentials: 'same-origin',
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      if (pending && pending.parentNode) pending.remove();
      if (data.conversation_id) {
        setConvId(data.conversation_id);
        if (state.ws) { try { state.ws.close(); } catch(e){} }
        connectWS();
      }
      addMessage('assistant', data.text || 'Готово.', data.cards || [], data.actions || [], [], data.message_id || null, data.suggestions || []);
      renderConvList();
    } catch (err) {
      if (pending && pending.parentNode) pending.remove();
      addMessage('assistant', '⚠️ Не удалось обработать файл: ' + (err.message || err));
    }
  }

  $('fileInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    uploadSpec(file);
    e.target.value = '';
  });

  async function recognizePhoto(file) {
    showConv();
    addMessage('user', '📷 ' + file.name);
    const pending = addMessage('assistant', 'Распознаю шильду…');
    try {
      const fd = new FormData();
      fd.append('photo', file);
      const res = await fetch('/api/assistant/recognize-photo/', {
        method: 'POST',
        headers: {'X-CSRFToken': csrf()},
        body: fd, credentials: 'same-origin',
      });
      const data = await res.json();
      if (pending && pending.parentNode) pending.remove();
      if (data.error) {
        addMessage('assistant', '⚠️ ' + data.error);
        return;
      }
      const t = data.text || '';
      let recognized = t;
      try {
        const j = JSON.parse(t.replace(/^```json\s*/, '').replace(/```$/, ''));
        const parts = [];
        if (j.brand) parts.push('Бренд: ' + j.brand);
        if (j.model) parts.push('Модель: ' + j.model);
        if (j.part_number) parts.push('Артикул: ' + j.part_number);
        if (j.serial) parts.push('Серийный: ' + j.serial);
        if (j.notes) parts.push(j.notes);
        recognized = parts.join('\n') || t;
        // Если есть артикул — сразу предложим search_parts
        if (j.part_number) {
          addMessage('assistant', '✓ Распознал:\n' + recognized,
            [], [{label: '🔍 Найти ' + j.part_number, action: 'search_parts',
                  params: {query: j.part_number}}]);
          return;
        }
      } catch(_){}
      addMessage('assistant', '✓ Распознал:\n' + recognized);
    } catch(err) {
      if (pending && pending.parentNode) pending.remove();
      addMessage('assistant', '⚠️ Ошибка распознавания: ' + (err.message || err));
    }
  }

  $('photoInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    recognizePhoto(file);
    e.target.value = '';
  });

  // ══════════════════════════════════════════════════════════
  // Init
  // ══════════════════════════════════════════════════════════
  async function init() {
    try {
      state.config = await api('/api/assistant/widget-config/');
      const name = state.config.user_name || 'User';
      const initial = name[0].toUpperCase();
      $('sideUserName').textContent = name;
      $('sideUserRole').textContent = (state.config.role || '').replace('operator_', '').replace(/_/g, ' ');
      $('sideAvatar').textContent = initial;
      $('topAvatar').textContent = initial;
      // Активная вкладка role-toggle
      const r = state.config.role || 'buyer';
      const uiRole = r.startsWith('operator') ? 'operator' : (r === 'seller' ? 'seller' : 'buyer');
      paintRoleToggle(uiRole);
      applyRoleWelcome(state.config.role);
      await Promise.all([loadConvList(), loadProjects(), loadNotifications()]);
      applyDefaultSidebar(state.convs.length > 0);
      loadSettings();
    } catch(e) {
      console.warn('Init failed:', e);
      applyDefaultSidebar(false);
    }
    // Conversation resolution priority:
    //   1. ?conv=<uuid> in URL (explicit deep link)
    //   2. localStorage cf_active_conv (continue last session)
    //   3. Most recent existing conversation (returning user)
    //   4. Fresh welcome screen — only here we let WS auto-create a new chat
    try {
      const params = new URLSearchParams(window.location.search);
      const urlConv = params.get('conv');
      const storedConv = getStoredConvId();
      const validIds = new Set((state.convs || []).map(c => c.id));
      let target = null;
      if (urlConv && validIds.has(urlConv)) target = urlConv;
      else if (storedConv && validIds.has(storedConv)) target = storedConv;
      else if (state.convs && state.convs.length) target = state.convs[0].id;
      if (target) {
        await window.openConv(target);
        // Если есть ?run=<action> — выполняем после загрузки conv
        const runAction = params.get('run');
        if (runAction) {
          const actionParams = {};
          for (const [k, v] of params.entries()) {
            if (k === 'run' || k === 'conv') continue;
            // Числовые значения (rfq_id, order_id, quote_id) парсим в int
            const n = parseInt(v, 10);
            actionParams[k] = (String(n) === v && !isNaN(n)) ? n : v;
          }
          setTimeout(() => quickAction(runAction, actionParams), 150);
          // Очистим url чтобы при F5 не повторялось
          history.replaceState({}, '', '/chat/');
        }
        return;
      }
      // Welcome stage — но если ?run= задан, тоже выполняем
      const runAction = params.get('run');
      if (runAction) {
        connectWS();
        const actionParams = {};
        for (const [k, v] of params.entries()) {
          if (k === 'run') continue;
          const n = parseInt(v, 10);
          actionParams[k] = (String(n) === v && !isNaN(n)) ? n : v;
        }
        setTimeout(() => quickAction(runAction, actionParams), 200);
        history.replaceState({}, '', '/chat/');
        updateHeroIcon();
        return;
      }
    } catch(e){ console.warn('conv resolve failed', e); }
    connectWS();
    setTimeout(() => $('heroInput').focus(), 200);
    updateHeroIcon();
  }

  // Auto-grow textareas + update hero icon
  document.addEventListener('input', (e) => {
    if (e.target.id === 'heroInput') {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(e.target.scrollHeight, 240) + 'px';
      updateHeroIcon();
    } else if (e.target.id === 'input') {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px';
    }
  });

  window.send = send;
  window.onKey = (e, fromHero) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(fromHero); }
  };

  // Resize handler — reapply mobile vs desktop sidebar logic
  let lastIsMobile = isMobile();
  window.addEventListener('resize', () => {
    const m = isMobile();
    if (m !== lastIsMobile) {
      lastIsMobile = m;
      if (m) $('sidebar').classList.remove('open');
      else applyDefaultSidebar(state.convs.length > 0);
    }
  });

  document.addEventListener('DOMContentLoaded', init);
})();
