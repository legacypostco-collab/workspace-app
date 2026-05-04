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
        return `<a href="/chat/project/${esc(p.id)}/" class="side-item${active}" style="text-decoration:none;">
          <span class="side-item-dot" style="background:${dot};"></span>
          <span class="side-item-text">${esc(p.name)}</span>
          <span class="side-item-meta">${esc(p.chats || 0)}</span>
        </a>`;
      }).join('');
    } catch(e) {
      $('projectsList').innerHTML = `<div class="side-item" style="color:rgba(0,0,0,0.4);">—</div>`;
    }
  }

  async function loadSidebarConvs() {
    try {
      const r = await fetch('/api/assistant/conversations/');
      const data = await r.json();
      const convs = data.results || data;
      if (!convs.length) {
        $('convList').innerHTML = `<div class="side-item-stack"><div class="side-item-stack-meta">Нет чатов</div></div>`;
        return;
      }
      $('convList').innerHTML = convs.slice(0, 30).map(c => {
        const date = c.updated_at ? new Date(c.updated_at) : null;
        const meta = date ? relativeTime(date) : '';
        return `<a href="/chat/?conv=${esc(c.id)}" class="side-item-stack" style="text-decoration:none;">
          <div class="side-item-stack-title">${esc(c.title || 'Без названия')}</div>
          <div class="side-item-stack-meta">${esc(meta)}</div>
        </a>`;
      }).join('');
      return convs.length;
    } catch(e) {
      $('convList').innerHTML = '';
      return 0;
    }
  }

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

    // KPI cards
    const stats = p.stats || {};
    const kpiHTML = `
      <div class="kpi">
        <div class="kpi-label">Open RFQs</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.open_rfqs?.count || 0}</div>
        </div>
        ${stats.open_rfqs?.urgent ? `<div class="kpi-sub"><span class="kpi-warn">${stats.open_rfqs.urgent} urgent</span> · ${esc(stats.open_rfqs.urgent_left || '')} left</div>` : ''}
      </div>
      <div class="kpi">
        <div class="kpi-label">Active Orders</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.active_orders?.count || 0}</div>
        </div>
        <div class="kpi-sub">${fmtMoney(stats.active_orders?.value_usd)} value</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">In Transit</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.in_transit?.count || 0}</div>
        </div>
        <div class="kpi-sub">earliest ETA: ${esc(stats.in_transit?.earliest_eta || '—')}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Spend MTD</div>
        <div class="kpi-value">
          <div class="kpi-num">${fmtMoney(stats.spend_mtd?.value_usd)}</div>
        </div>
        <div class="kpi-sub"><span class="${stats.spend_mtd?.delta_pct >= 0 ? 'kpi-good' : 'kpi-warn'}">${stats.spend_mtd?.delta_pct >= 0 ? '+' : ''}${stats.spend_mtd?.delta_pct ?? 0}%</span> vs ${esc(stats.spend_mtd?.vs_period || '')}</div>
      </div>
    `;

    // Documents
    const docs = p.documents || [];
    const docsHTML = docs.length ? docs.map(d => {
      const tagClass = DOC_TAG_COLORS[d.doctype] || '';
      const sizeStr = d.size_kb ? `${d.size_kb} KB` : '';
      const meta = [sizeStr, ...(d.meta?.summary ? [d.meta.summary] : [])].filter(Boolean).join(' · ');
      return `<div class="doc">
        <div class="doc-icon ${d.doctype === 'regulation' ? 'red' : ''}">${FILE_ICON}</div>
        <div class="doc-info">
          <div class="doc-row1">
            <span class="doc-name">${esc(d.name)}</span>
            <span class="doc-tag ${tagClass}">${esc(d.doctype_label || d.doctype)}</span>
          </div>
          <div class="doc-meta">${esc(meta)}</div>
        </div>
        <div class="doc-status"><span class="dot"></span>${esc(d.status === 'processed' ? 'обработан' : d.status)}</div>
      </div>`;
    }).join('') : `<div class="doc"><div class="doc-info"><div class="doc-meta">Документы не загружены</div></div></div>`;

    // RFQs
    const rfqs = p.rfqs || [];
    const rfqsHTML = rfqs.length ? rfqs.map(r => {
      const respClass = r.responded_color === 'amber' ? 'amber' : '';
      return `<div class="rfq">
        <span class="rfq-num">${esc(r.number)}</span>
        <div class="rfq-info">
          <div class="rfq-title">${esc(r.title)}${r.tag ? ` <span class="rfq-tag">${esc(r.tag)}</span>` : ''}</div>
          <div class="rfq-meta">${esc(r.meta)}</div>
        </div>
        <span class="rfq-resp ${respClass}">${esc(r.responded)}</span>
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
        <a href="#" class="sec-title-link" onclick="alert('Загрузка документов — скоро');return false;">+ Загрузить</a>
      </div>
      <div class="docs-grid">${docsHTML}</div>
      <div class="ai-note">
        <svg class="ai-note-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span>AI использует <strong>все эти документы</strong> как контекст для ответов в чатах этого проекта</span>
      </div>

      <div class="sec-title">
        <h2>Активные RFQ</h2>
        <span class="sec-title-count">${rfqs.length}</span>
      </div>
      <div class="rfq-list">${rfqsHTML}</div>

      <div class="sec-title">
        <h2>Открытые заказы</h2>
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
