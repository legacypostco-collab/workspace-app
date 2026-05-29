/* Project Page — /chat/project/<uuid>/
 *
 * Standalone (does NOT depend on chat-first.js).
 * Handles:
 *   - Sidebar toggle + state persistence (shared key 'cf_sidebar_open')
 *   - Loads widget config → user/role/avatar
 *   - Loads projects + conversations into sidebar
 *   - Loads project detail and renders KPI / docs / RFQs / orders / chats
 */
(function(){
  'use strict';

  const SB_KEY = 'cf_sidebar_open';
  const PID = window.PROJECT_ID;

  // ── Тёмная тема (синхронно с /chat/ через localStorage cf_dark_mode) ──
  // Применяем СРАЗУ, чтобы не было «вспышки» светлой темы при загрузке.
  try {
    if (localStorage.getItem('cf_dark_mode') === '1') {
      document.body.classList.add('dark-mode');
    }
  } catch(e) {}
  window.toggleTheme = function() {
    const isDark = document.body.classList.contains('dark-mode');
    document.body.classList.toggle('dark-mode', !isDark);
    try { localStorage.setItem('cf_dark_mode', !isDark ? '1' : '0'); } catch(e) {}
  };

  // ── Колокольчик: счётчик непрочитанных уведомлений (полная панель на /chat/) ──
  async function loadNotifBadge() {
    try {
      const r = await fetch('/api/assistant/notifications/?limit=1', {credentials:'same-origin'});
      if (!r.ok) return;
      const data = await r.json();
      const cnt = Number(data && data.unread_count) || 0;
      const b = document.getElementById('bellBadge');
      if (!b) return;
      if (cnt > 0) {
        b.textContent = cnt > 99 ? '99+' : String(cnt);
        b.style.display = 'flex';
      } else {
        b.style.display = 'none';
      }
    } catch(e) {}
  }

  // ── Helpers ──────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const csrf = () => document.cookie.replace(/(?:(?:^|.*;\s*)csrftoken\s*=\s*([^;]*).*$)|^.*$/, '$1');
  const esc = s => (s == null ? '' : String(s)).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

  function fmtMoney(v, c='USD') {
    if (v == null) return '—';
    const sym = {USD:'$', EUR:'€', RUB:'₽', CNY:'¥'}[c] || '';
    if (Math.abs(v) >= 1000) {
      return sym + (v/1000).toLocaleString('en-US', {maximumFractionDigits:1}) + 'K';
    }
    return sym + Number(v).toLocaleString('en-US', {maximumFractionDigits:0});
  }

  async function api(path, opts={}) {
    const res = await fetch(path, {
      headers: {'Content-Type':'application/json','X-CSRFToken': csrf(), ...(opts.headers||{})},
      ...opts,
    });
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json();
  }

  // ── Sidebar ──────────────────────────────────────────────
  function isMobile() { return window.innerWidth <= 768; }

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
      $('sidebar').classList.remove('open');
      return;
    }
    const saved = localStorage.getItem(SB_KEY);
    let open;
    if (saved === '1') open = true;
    else if (saved === '0') open = false;
    else open = hasHistory;
    $('sidebar').classList.toggle('open', open);
  }

  document.addEventListener('click', (e) => {
    if (!isMobile()) return;
    const sb = $('sidebar');
    if (!sb.classList.contains('open')) return;
    if (sb.contains(e.target) || e.target.closest('.top-burger')) return;
    sb.classList.remove('open');
  });

  function relativeTime(date) {
    const now = new Date();
    const diff = (now - date) / 1000;
    if (diff < 60) return 'только что';
    if (diff < 3600) return Math.floor(diff/60) + ' мин назад';
    if (diff < 86400) return Math.floor(diff/3600) + ' ч назад';
    if (diff < 604800) return Math.floor(diff/86400) + ' дн назад';
    return date.toLocaleDateString('ru-RU', {day:'2-digit', month:'short'});
  }

  // ── Sidebar data loading ─────────────────────────────────
  const DOT_BG = {
    green:'#22c55e', orange:'#f97316', blue:'#3b82f6',
    purple:'#a855f7', red:'#ef4444', gray:'#9ca3af',
  };

  async function loadSidebarProjects() {
    try {
      const data = await api('/api/assistant/projects/');
      const list = (data.projects || []);
      if (!list.length) {
        $('projectsList').innerHTML = `<div class="side-item" style="color:rgba(0,0,0,0.4);">Нет проектов</div>`;
        return;
      }
      $('projectsList').innerHTML = list.map(p => {
        const dot = DOT_BG[p.dot_color] || DOT_BG.green;
        const active = (p.id === PID) ? ' active' : '';
        const pidStr = String(p.id).replace(/'/g, "&#39;");
        const nameStr = String(p.name || '').replace(/'/g, "&#39;").replace(/"/g, '&quot;');
        // Inline onclick на самой кнопке — гарантированно стопает навигацию по <a>
        return `<a href="/chat/project/${esc(p.id)}/" class="side-item side-item-proj${active}" data-project-id="${esc(p.id)}" data-project-name="${esc(p.name)}" style="text-decoration:none;">
          <span class="side-item-dot" style="background:${dot};"></span>
          <span class="side-item-text">${esc(p.name)}</span>
          <span class="side-item-meta">${esc(p.chats || 0)}</span>
          <button class="side-item-del" type="button" title="Удалить проект" aria-label="Удалить" onclick="event.preventDefault();event.stopPropagation();window.__deleteProject&amp;&amp;window.__deleteProject('${pidStr}','${nameStr}');return false;">×</button>
        </a>`;
      }).join('');
    } catch(e) {
      $('projectsList').innerHTML = `<div class="side-item" style="color:rgba(0,0,0,0.4);">—</div>`;
    }
  }

  // «+ Новый проект» в сайдбаре project.html: prompt → POST → переход на новую страницу.
  async function createProjectFromSidebar() {
    const name = (window.prompt('Название проекта:', '') || '').trim();
    if (!name) return;
    try {
      const r = await fetch('/api/assistant/projects/', {
        method: 'POST',
        headers: {'Content-Type':'application/json','X-CSRFToken': csrf()},
        body: JSON.stringify({name}),
      });
      if (!r.ok) { alert('Не удалось создать проект (HTTP ' + r.status + ')'); return; }
      const data = await r.json();
      if (data && data.id) {
        window.location.href = '/chat/project/' + data.id + '/';
      } else {
        await loadSidebarProjects();
      }
    } catch(e) {
      alert('Ошибка: ' + (e && e.message ? e.message : e));
    }
  }
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('#sideProjects .side-section-add');
    if (!btn) return;
    e.preventDefault(); e.stopPropagation();
    createProjectFromSidebar();
  });

  // Загрузка документа в проект: открываем системный file-picker, POST multipart,
  // перерендериваем страницу проекта.
  window.__uploadProjectDoc = function(doctypeHint) {
    if (!PID) return;
    // accept-фильтр по конкретному типу слота
    const ACCEPT_BY_TYPE = {
      fleet:      '.xlsx,.xls,.csv',
      spec:       '.xlsx,.xls,.csv',
      regulation: '.pdf,.docx,.doc',
      drawing:    '.dwg,.dxf,.pdf,.png,.jpg,.jpeg',
      other:      '.pdf,.xlsx,.xls,.csv,.docx,.doc,.txt,.png,.jpg,.jpeg,.zip',
    };
    const input = document.createElement('input');
    input.type = 'file';
    input.style.display = 'none';
    input.accept = ACCEPT_BY_TYPE[doctypeHint] || ACCEPT_BY_TYPE.other;
    input.onchange = async () => {
      const f = input.files && input.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append('file', f);
      if (doctypeHint) fd.append('doctype', doctypeHint);
      try {
        const r = await fetch('/api/assistant/projects/' + PID + '/documents/', {
          method: 'POST',
          headers: {'X-CSRFToken': csrf()},
          credentials: 'same-origin',
          body: fd,
        });
        if (!r.ok && r.status !== 201) {
          let msg = 'HTTP ' + r.status;
          try { const j = await r.json(); if (j && j.error) msg = j.error; } catch(_){}
          alert('Не удалось загрузить: ' + msg);
          return;
        }
        // Успех — обновляем страницу проекта (документ появится в списке)
        if (typeof loadProject === 'function') await loadProject();
        else window.location.reload();
      } catch(err) {
        alert('Ошибка загрузки: ' + (err && err.message ? err.message : err));
      } finally {
        input.remove();
      }
    };
    document.body.appendChild(input);
    input.click();
  };

  // Глобальный helper для inline-onclick (более надёжно чем capture-listener
  // на anchor-tag — в Chrome иногда anchor вообще не пропускает click до handler'а).
  window.__deleteProject = async function(pid, name) {
    if (!pid) return;
    if (!window.confirm(`Удалить проект «${name || 'проект'}»?`)) return;
    try {
      const r = await fetch('/api/assistant/projects/' + pid + '/', {
        method: 'DELETE',
        headers: {'X-CSRFToken': csrf()},
        credentials: 'same-origin',
      });
      if (!r.ok && r.status !== 204) { alert('Не удалось удалить (HTTP ' + r.status + ')'); return; }
      if (pid === PID) { window.location.href = '/chat/'; return; }
      await loadSidebarProjects();
    } catch(err) {
      alert('Ошибка: ' + (err && err.message ? err.message : err));
    }
  };

  // ── Список чатов с группировкой (как в /chat/) ──────────────
  // Иконки категорий — те же, что в chat-first.js.
  const CATEGORY_ICON = {admin:'🛡', purchase:'🛒', support:'🎧', general:'💬'};
  // ChatGPT/Linear-style группировка по дате последнего обновления.
  function _bucketForDate(d, now) {
    if (!d) return 'older';
    const dStart = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const nStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const daysAgo = Math.floor((nStart - dStart) / 86400000);
    if (daysAgo <= 0) return 'today';
    if (daysAgo === 1) return 'yesterday';
    if (daysAgo <= 7) return 'week';
    if (daysAgo <= 30) return 'month';
    return 'older';
  }
  const _BUCKET_LABELS = {
    today: 'Сегодня', yesterday: 'Вчера',
    week: 'На этой неделе', month: 'В этом месяце', older: 'Ранее',
  };
  const _BUCKET_ORDER = ['today', 'yesterday', 'week', 'month', 'older'];

  async function loadSidebarConvs() {
    const wrap = $('convList');
    const clearBtn = $('clearHistoryBtn');
    try {
      const r = await fetch('/api/assistant/conversations/');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      const convs = data.results || data;
      if (clearBtn) clearBtn.style.display = (convs && convs.length) ? '' : 'none';
      if (!convs.length) {
        wrap.innerHTML = `<div class="side-item-stack"><div class="side-item-stack-meta">Нет чатов</div></div>`;
        return 0;
      }
      const now = new Date();
      const buckets = {today:[], yesterday:[], week:[], month:[], older:[]};
      for (const c of convs.slice(0, 60)) {
        const d = c.updated_at ? new Date(c.updated_at) : null;
        buckets[_bucketForDate(d, now)].push(c);
      }
      const renderItem = (c) => {
        const date = c.updated_at ? new Date(c.updated_at) : null;
        const meta = date ? relativeTime(date) : '';
        const lastMeta = c.last_message ? c.last_message.content.substring(0, 40) : meta;
        const icon = CATEGORY_ICON[c.category || 'general'] || '💬';
        const cid = esc(c.id);
        // На странице проекта клик ведёт обратно в /chat/?conv=ID (там же
        // работает ws / ⋯-меню / переименование). ⋯-кнопка — то же самое.
        return `<a href="/chat/?conv=${cid}" class="side-item-stack" data-conv-id="${cid}">
          <div class="side-item-stack-content">
            <div class="side-item-stack-title"><span class="conv-cat-icon">${icon}</span>${esc(c.title || 'Без названия')}</div>
            <div class="side-item-stack-meta">${esc(meta)}${lastMeta && lastMeta !== meta ? ' · ' + esc(lastMeta) : ''}</div>
          </div>
          <button class="side-item-stack-more" type="button" title="Действия" onclick="event.preventDefault();event.stopPropagation();window.__openConvMenu&amp;&amp;window.__openConvMenu('${cid}', this);return false;" aria-label="Действия">⋯</button>
        </a>`;
      };
      wrap.innerHTML = _BUCKET_ORDER
        .filter(k => buckets[k].length)
        .map(k => `<div class="conv-bucket"><div class="conv-bucket-label">${esc(_BUCKET_LABELS[k])}</div>${buckets[k].map(renderItem).join('')}</div>`)
        .join('');
      return convs.length;
    } catch(e) {
      if (wrap) wrap.innerHTML = '<div class="side-item-stack"><div class="side-item-stack-meta">⚠️ Не удалось загрузить</div></div>';
      return 0;
    }
  }

  // ⋯-меню чата: всплывающий popover с Переименовать / Удалить.
  // Тот же UX, что в /chat/ — те же endpoints.
  function _renameConv(convId) {
    const v = window.prompt('Новое название чата:', '');
    if (v === null) return;
    const t = v.trim();
    if (!t) return;
    fetch('/api/assistant/conversations/' + convId + '/', {
      method: 'PATCH',
      headers: {'Content-Type':'application/json','X-CSRFToken': csrf()},
      credentials: 'same-origin',
      body: JSON.stringify({title: t.slice(0,200)}),
    }).then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return loadSidebarConvs();
    }).catch(err => alert('Не удалось переименовать: ' + (err && err.message ? err.message : err)));
  }
  function _deleteConv(convId) {
    if (!window.confirm('Удалить чат?')) return;
    fetch('/api/assistant/conversations/' + convId + '/', {
      method: 'DELETE',
      headers: {'X-CSRFToken': csrf()},
      credentials: 'same-origin',
    }).then(r => {
      if (!r.ok && r.status !== 204) throw new Error('HTTP ' + r.status);
      return loadSidebarConvs();
    }).catch(err => alert('Не удалось удалить: ' + (err && err.message ? err.message : err)));
  }

  let _convMenuEl = null;
  function _closeConvMenu() {
    if (_convMenuEl) { _convMenuEl.remove(); _convMenuEl = null; }
  }
  document.addEventListener('click', (e) => {
    if (_convMenuEl && !_convMenuEl.contains(e.target)) _closeConvMenu();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _closeConvMenu(); });

  window.__openConvMenu = function(convId, btn) {
    _closeConvMenu();
    const rect = btn.getBoundingClientRect();
    const menu = document.createElement('div');
    menu.className = 'conv-ctx-menu';
    menu.style.cssText = 'position:fixed;z-index:9999;min-width:180px;background:#fff;border:1px solid rgba(0,0,0,0.10);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,0.18);padding:6px;display:flex;flex-direction:column;gap:2px;';
    if (document.body.classList.contains('dark-mode')) {
      menu.style.background = '#1a1a1a'; menu.style.borderColor = 'rgba(255,255,255,0.10)';
    }
    menu.innerHTML = `
      <button type="button" data-action="rename" style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:transparent;border:none;border-radius:6px;cursor:pointer;font-size:13px;color:inherit;text-align:left;">
        <span>✏️</span><span>Переименовать</span>
      </button>
      <button type="button" data-action="delete" style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:transparent;border:none;border-radius:6px;cursor:pointer;font-size:13px;color:#dc3545;text-align:left;">
        <span>🗑</span><span>Удалить</span>
      </button>
    `;
    // Hover-стили инлайново (никаких глобальных классов на лету)
    menu.querySelectorAll('button').forEach(b => {
      b.addEventListener('mouseenter', () => { b.style.background = 'rgba(0,0,0,0.06)'; });
      b.addEventListener('mouseleave', () => { b.style.background = 'transparent'; });
      b.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const act = b.dataset.action;
        _closeConvMenu();
        if (act === 'rename') _renameConv(convId);
        else if (act === 'delete') _deleteConv(convId);
      });
    });
    document.body.appendChild(menu);
    const mw = menu.getBoundingClientRect();
    let x = rect.right - mw.width;
    let y = rect.bottom + 4;
    x = Math.max(8, Math.min(x, window.innerWidth - mw.width - 8));
    y = Math.max(8, Math.min(y, window.innerHeight - mw.height - 8));
    menu.style.left = x + 'px';
    menu.style.top  = y + 'px';
    _convMenuEl = menu;
  };

  // «Очистить» в шапке «Недавние»: confirm + DELETE на bulk endpoint.
  window.__clearHistory = async function() {
    if (!window.confirm('Очистить всю историю чатов? Это действие нельзя отменить.')) return;
    try {
      const r = await fetch('/api/assistant/conversations/', {
        method: 'DELETE',
        headers: {'X-CSRFToken': csrf()},
        credentials: 'same-origin',
      });
      if (!r.ok && r.status !== 204) {
        // Если endpoint не поддерживает bulk DELETE — открываем /chat/ где есть UI
        window.location.href = '/chat/?action=clear_history';
        return;
      }
      await loadSidebarConvs();
    } catch(err) {
      window.location.href = '/chat/?action=clear_history';
    }
  };

  async function loadConfig() {
    try {
      const cfg = await api('/api/assistant/widget-config/');
      const name = cfg.user_name || 'User';
      const initial = (name[0] || '?').toUpperCase();
      $('sideUserName').textContent = name;
      $('sideUserRole').textContent = (cfg.role || '').replace('operator_', '').replace(/_/g, ' ');
      $('sideAvatar').textContent = initial;
      $('topAvatar').textContent = initial;
    } catch(e){}
  }

  // ── Project rendering ────────────────────────────────────
  const DOC_TAG_COLORS = {
    spec: '',         // green (default)
    fleet: 'blue',
    drawing: 'gray',
    regulation: 'red',
    conditions: 'amber',
    contract: 'amber',
    invoice: 'amber',
    other: 'gray',
  };

  const FILE_ICON = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="15" y2="17"/></svg>`;

  function renderProject(p) {
    const tags = (p.tags && p.tags.length) ? p.tags.join(' · ') : '';
    const deadlineStr = p.deadline ? `Дедлайн: <span class="pj-meta-strong">${esc(p.deadline)}</span>` : '';
    const customer = p.customer ? `<span class="pj-meta-strong">${esc(p.customer)}</span>` : '';
    const dotBg = DOT_BG[p.dot_color] || DOT_BG.green;

    // Локализация лейблов под текущий язык интерфейса.
    // Подписи строго по делу: только то что несёт смысл для покупателя.
    const lang = (document.documentElement.lang || 'ru').toLowerCase().split('-')[0];
    const LBL = {
      ru: {open_rfqs: 'Открытые RFQ', active_orders: 'Активные заказы',
           in_transit: 'В пути', spend_mtd: 'Расходы за месяц',
           awaiting_op: 'ждут оператора',
           value: 'сумма', earliest_eta: 'ближайший ETA',
           vs_prev_month: 'к прошлому месяцу',
           sec_rfqs: 'Подбор — ждут вашего решения',
           sec_orders: 'Заказы в работе'},
      en: {open_rfqs: 'Open RFQs', active_orders: 'Active Orders',
           in_transit: 'In Transit', spend_mtd: 'Spend MTD',
           awaiting_op: 'awaiting operator',
           value: 'value', earliest_eta: 'earliest ETA',
           vs_prev_month: 'vs previous month',
           sec_rfqs: 'Matches — awaiting your decision',
           sec_orders: 'Orders in progress'},
      es: {open_rfqs: 'RFQ abiertos', active_orders: 'Pedidos activos',
           in_transit: 'En tránsito', spend_mtd: 'Gasto del mes',
           awaiting_op: 'esperan operador',
           value: 'valor', earliest_eta: 'ETA más cercano',
           vs_prev_month: 'vs mes anterior',
           sec_rfqs: 'Coincidencias — esperan su decisión',
           sec_orders: 'Pedidos en curso'},
      zh: {open_rfqs: '待处理 RFQ', active_orders: '活动订单',
           in_transit: '运输中', spend_mtd: '本月支出',
           awaiting_op: '等待操作员',
           value: '总额', earliest_eta: '最早预计',
           vs_prev_month: '环比上月',
           sec_rfqs: '匹配 — 等待您的决定',
           sec_orders: '进行中的订单'},
    };
    const L = LBL[lang] || LBL.ru;
    // KPI cards. Подписи под цифрами объясняют ЧТО за число.
    const stats = p.stats || {};
    const semiCount = stats.open_rfqs?.semi || 0;  // RFQ ждущие подбора оператора
    const kpiHTML = `
      <div class="kpi">
        <div class="kpi-label">${L.open_rfqs}</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.open_rfqs?.count || 0}</div>
        </div>
        ${semiCount ? `<div class="kpi-sub"><span class="kpi-warn">${semiCount}</span> ${L.awaiting_op}</div>` : ''}
      </div>
      <div class="kpi">
        <div class="kpi-label">${L.active_orders}</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.active_orders?.count || 0}</div>
        </div>
        <div class="kpi-sub">${fmtMoney(stats.active_orders?.value_usd)} ${L.value}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">${L.in_transit}</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.in_transit?.count || 0}</div>
        </div>
        <div class="kpi-sub">${L.earliest_eta}: ${esc(stats.in_transit?.earliest_eta || '—')}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">${L.spend_mtd}</div>
        <div class="kpi-value">
          <div class="kpi-num">${fmtMoney(stats.spend_mtd?.value_usd)}</div>
        </div>
        <div class="kpi-sub"><span class="${stats.spend_mtd?.delta_pct >= 0 ? 'kpi-good' : 'kpi-warn'}">${stats.spend_mtd?.delta_pct >= 0 ? '+' : ''}${stats.spend_mtd?.delta_pct ?? 0}%</span> ${L.vs_prev_month}</div>
      </div>
    `;

    // Documents — empty-state карточка (0 docs) или категоризованные слоты (есть docs)
    const docs = p.documents || [];
    const DOC_SLOTS = [
      {key: "fleet",      icon: "🚜", label: "Парк техники",
       descShort: "Точные подборы запчастей по моделям и серийникам — без перепросов",
       descFull: "Excel/CSV: модель, S/N, год, моточасы"},
      {key: "spec",       icon: "📊", label: "Закупки за прошлый год",
       descShort: "AI видит ваши паттерны: что, сколько, у кого. Подсказывает аналоги дешевле",
       descFull: "Excel из 1С/SAP с суммами и поставщиками"},
      {key: "regulation", icon: "📄", label: "Регламенты ТО",
       descShort: "Прогноз когда что закончится — заявки за неделю до простоя, а не после",
       descFull: "PDF от производителя с интервалами замены"},
      {key: "drawing",    icon: "📐", label: "Чертежи и спецификации",
       descShort: "Поставщик сразу видит, что нужно — котировка приходит за часы, не дни",
       descFull: "DWG/PDF сборок, ведомости узлов"},
    ];

    // Documents: единый layout (прогресс-бар + 5 слотов). При 0 docs прогресс показывает
    // tagline «Чем больше данных — тем точнее аналитика» вместо счётчика, и слоты пустые.
    const SLOTS_WITH_OTHER = [...DOC_SLOTS, {key:"other", icon:"📦", label:"Другое",
      descShort:"Контракты и условия поставки — AI учитывает их при формировании заказа",
      descFull:"Контракты, условия Incoterm"}];
    const docsByType = {};
    docs.forEach(d => {
      const k = d.doctype || "other";
      if (!docsByType[k]) docsByType[k] = [];
      docsByType[k].push(d);
    });
    const connectedTypes = SLOTS_WITH_OTHER.filter(s => (docsByType[s.key] || []).length > 0).length;
    const progressPct = Math.round(connectedTypes / SLOTS_WITH_OTHER.length * 100);
    const renderSlot = (slot) => {
      const slotDocs = docsByType[slot.key] || [];
      const filled = slotDocs.length > 0;
      const addLabel = filled ? '+ Добавить' : '+ Загрузить';
      const cornerBadge = filled
        ? `<span class="doc-slot-corner-badge" title="${slotDocs.length} документ(ов)">${slotDocs.length}</span>`
        : '';
      return `<div class="doc-slot${filled ? ' doc-slot-filled' : ''}">
        ${cornerBadge}
        <div class="doc-slot-head">
          <span class="doc-slot-icon">${slot.icon}</span>
          <span class="doc-slot-label">${esc(slot.label)}</span>
          <a href="#" class="doc-slot-add" onclick="window.__uploadProjectDoc&amp;&amp;window.__uploadProjectDoc('${slot.key}');return false;">${addLabel}</a>
        </div>
        <div class="doc-slot-desc-short">${esc(slot.descShort)}</div>
        <div class="doc-slot-desc">↓ ${esc(slot.descFull)}</div>
      </div>`;
    };
    // 4 функциональных слота, потом рекламная мини-карточка, потом «Другое» — в нижнем ряду
    const mainSlotsHTML = DOC_SLOTS.map(renderSlot).join('');
    const otherSlot = SLOTS_WITH_OTHER.find(s => s.key === 'other');
    const otherSlotHTML = renderSlot(otherSlot);
    // Рекламная мини-карточка (всегда показывается как 5-й слот) — занимает левую
    // позицию в нижнем ряду, рядом с «Другое». Это постоянное напоминание зачем грузить.
    const ctaLabel = docs.length === 0 ? '+ Загрузить первый документ' : '+ Загрузить ещё';
    const marketingCardHTML = `
      <div class="doc-slot doc-slot-cta">
        <div class="doc-slot-cta-eyebrow">ПОДКЛЮЧИТЕ ДАННЫЕ</div>
        <div class="doc-slot-cta-title">Чем больше данных — тем точнее аналитика</div>
        <div class="doc-slot-cta-sub">AI работает в контексте вашего проекта — получите подборы, прогнозы и сравнения за секунды.</div>
        <button type="button" class="doc-slot-cta-btn" onclick="window.__uploadProjectDoc&amp;&amp;window.__uploadProjectDoc()">${ctaLabel}</button>
        <div class="doc-slot-cta-hint">PDF / Excel / DWG · до 25 МБ</div>
      </div>
    `;
    const docsHTML = `
      <div class="doc-progress">
        <div class="doc-progress-bar"><div class="doc-progress-fill" style="width:${progressPct}%"></div></div>
        <div class="doc-progress-label"><strong>${connectedTypes} / ${SLOTS_WITH_OTHER.length}</strong> типов подключено${docs.length === 0 ? ' · начните с любого' : (connectedTypes < SLOTS_WITH_OTHER.length ? ' · загрузите ещё для полного контекста' : ' · всё подключено')}</div>
      </div>
      ${mainSlotsHTML}
      ${marketingCardHTML}
      ${otherSlotHTML}
    `;

    // RFQs
    const rfqs = p.rfqs || [];
    const rfqsHTML = rfqs.length ? rfqs.map(r => {
      const respClass = r.responded_color === 'amber' ? 'amber' : '';
      // Локализованный лейбл режима подбора. r.tag — машинно-читаемый код
      // ("AUTO" / "SEMI") или произвольная строка (legacy). Если AUTO/SEMI —
      // переводим, иначе показываем как есть.
      const MODE_LABEL = {
        ru: {AUTO: 'Авто-подбор', SEMI: 'Подбор оператором'},
        en: {AUTO: 'Auto-match',  SEMI: 'Operator-match'},
        es: {AUTO: 'Auto-coincid.', SEMI: 'Por operador'},
        zh: {AUTO: '自动匹配',     SEMI: '操作员匹配'},
      };
      const ML = MODE_LABEL[lang] || MODE_LABEL.ru;
      const tagText = r.tag ? (ML[r.tag.toUpperCase()] || r.tag) : '';
      return `<div class="rfq">
        <span class="rfq-num">${esc(r.number)}</span>
        <div class="rfq-info">
          <div class="rfq-title">${esc(r.title)}${tagText ? ` <span class="rfq-tag rfq-tag-${esc((r.tag||'').toLowerCase())}">${esc(tagText)}</span>` : ''}</div>
          <div class="rfq-meta">${esc(r.meta)}</div>
        </div>
        <div class="rfq-best">
          <div class="rfq-best-label">${esc(r.best_label || 'best so far')}</div>
          <div class="rfq-best-val">${fmtMoney(r.best_so_far)}</div>
        </div>
      </div>`;
    }).join('') : `<div class="rfq" style="border-left-color:rgba(0,0,0,0.1);"><div class="rfq-info"><div class="rfq-meta">Нет активных RFQ</div></div></div>`;

    // Orders
    const orders = p.orders || [];
    const ordersHTML = orders.length ? orders.map(o => {
      const stages = o.stages || [];
      const stageBars = stages.map(s => `<div class="po-stage ${s ? 'done' : ''}"></div>`).join('');
      const statusClass = o.status_color === 'green' ? 'green' : '';
      return `<div class="po">
        <div class="po-row1">
          <span class="po-num">${esc(o.number)}</span>
          <span class="po-title">${esc(o.title)}</span>
          <span class="po-status ${statusClass}">${esc(o.status)}</span>
          <span class="po-eta">${esc(o.eta || '')}</span>
        </div>
        <div class="po-stages">${stageBars}</div>
        <div class="po-row2">
          <span><strong>${esc(o.seller || '—')}</strong></span>
          <span>${esc(o.operator || '')}</span>
          <span class="po-amount">${fmtMoney(o.amount)}</span>
        </div>
      </div>`;
    }).join('') : `<div class="po"><div class="po-row2"><span>Нет открытых заказов</span></div></div>`;

    // Chats
    const chats = p.chats || [];
    const chatsHTML = chats.length ? chats.map(c => {
      const date = c.updated_at ? new Date(c.updated_at) : null;
      const meta = date ? relativeTime(date) : '';
      return `<a href="/chat/?conv=${esc(c.id)}" class="chat" style="text-decoration:none;">
        <div class="chat-info">
          <div class="chat-title">${esc(c.title || 'Без названия')}</div>
          ${c.preview ? `<div class="chat-preview">${esc(c.preview)}</div>` : ''}
        </div>
        <span class="chat-time">${esc(meta)}</span>
      </a>`;
    }).join('') : `<div class="chat" style="cursor:default;"><div class="chat-info"><div class="chat-preview">Нет чатов в этом проекте</div></div></div>`;

    return `
      <div class="crumbs">
        <a href="/chat/">Проекты</a>
        <span class="crumbs-sep">/</span>
        <span>${esc(p.name)}</span>
      </div>

      <div class="pj-head">
        <div class="pj-head-left">
          <div class="pj-title-row">
            <span class="pj-dot" style="background:${dotBg};"></span>
            <h1 class="pj-name">${esc(p.name)}</h1>
          </div>
          <div class="pj-meta">
            ${customer}
            ${tags ? `<span>${esc(tags)}</span>` : ''}
            ${deadlineStr ? `<span>${deadlineStr}</span>` : ''}
          </div>
        </div>
        <div class="pj-actions">
          <button class="pj-btn" onclick="newProjectChat()">+ Новый чат</button>
          <button class="pj-btn" onclick="alert('Файлы — скоро')">Файлы</button>
          <button class="pj-btn" onclick="alert('Настройки проекта — скоро')">Настройки</button>
        </div>
      </div>

      <div class="kpi-grid">${kpiHTML}</div>

      <div class="sec-title">
        <h2>Документы проекта</h2>
        <span class="sec-title-count">${docs.length}</span>
        <a href="#" class="sec-title-link" onclick="window.__uploadProjectDoc&amp;&amp;window.__uploadProjectDoc();return false;">+ Загрузить</a>
      </div>
      <div class="doc-slots-stack">${docsHTML}</div>
      <div class="ai-note">
        <svg class="ai-note-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span>AI использует <strong>все эти документы</strong> как контекст для ответов в чатах этого проекта</span>
      </div>

      <div class="sec-title">
        <h2>${L.sec_rfqs}</h2>
        <span class="sec-title-count">${rfqs.length}</span>
      </div>
      <div class="rfq-list">${rfqsHTML}</div>

      <div class="sec-title">
        <h2>${L.sec_orders}</h2>
        <span class="sec-title-count">${orders.length}</span>
      </div>
      <div class="po-list">${ordersHTML}</div>

      <div class="sec-title">
        <h2>Чаты по проекту</h2>
        <span class="sec-title-count">${chats.length}</span>
        <a href="#" class="sec-title-link" onclick="newProjectChat();return false;">+ Новый чат</a>
      </div>
      <div class="chat-list">${chatsHTML}</div>
    `;
  }

  async function loadProject() {
    if (!PID) {
      $('projectContent').innerHTML = `<div style="text-align:center;padding:60px 20px;color:rgba(0,0,0,0.6);">Проект не указан</div>`;
      return;
    }
    try {
      const p = await api(`/api/assistant/projects/${PID}/`);
      $('projectContent').innerHTML = renderProject(p);
      document.title = `${p.name} — Consolidator Parts`;
    } catch(e) {
      $('projectContent').innerHTML = `<div style="text-align:center;padding:60px 20px;color:rgba(0,0,0,0.6);">
        <div style="font-size:18px;font-weight:600;margin-bottom:8px;">Не удалось загрузить проект</div>
        <div style="font-size:13px;">${esc(e.message)}</div>
        <a href="/chat/" style="display:inline-block;margin-top:16px;padding:8px 16px;background:rgba(255,255,255,0.6);border-radius:8px;color:#1a1a1a;font-weight:600;text-decoration:none;">← Назад в чаты</a>
      </div>`;
    }
  }

  // Create new chat in this project
  window.newProjectChat = async () => {
    if (!PID) return;
    try {
      const res = await fetch(`/api/assistant/projects/${PID}/chats/`, {
        method: 'POST',
        headers: {'Content-Type':'application/json','X-CSRFToken': csrf()},
      });
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      window.location.href = `/chat/?conv=${data.conversation_id}`;
    } catch(e) {
      alert('Не удалось создать чат: ' + e.message);
    }
  };

  // ── Init ─────────────────────────────────────────────────
  async function init() {
    await loadConfig();
    const [, convCount] = await Promise.all([
      loadSidebarProjects(),
      loadSidebarConvs(),
    ]);
    applyDefaultSidebar((convCount || 0) > 0 || true);  // open by default on project page
    loadProject();
    loadNotifBadge();
    // Авто-обновление счётчика каждые 60 сек (как на /chat/)
    setInterval(loadNotifBadge, 60000);
  }

  // Resize handler
  let lastIsMobile = isMobile();
  window.addEventListener('resize', () => {
    const m = isMobile();
    if (m !== lastIsMobile) {
      lastIsMobile = m;
      if (m) $('sidebar').classList.remove('open');
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
