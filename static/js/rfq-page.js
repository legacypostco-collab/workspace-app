/* RFQ detail page — chat-first style.
 * Loads RFQ + items from /api/assistant/rfq/<id>/ and renders Slack-like view.
 */
(function(){
  'use strict';
  const $ = id => document.getElementById(id);
  const SB_KEY = 'cf_sidebar_open';

  const esc = s => (s == null ? '' : String(s)).replace(/[&<>"']/g, m =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const fmtMoney = (v, c='USD') => {
    if (v == null || v === '') return '—';
    const sym = {USD:'$', EUR:'€', RUB:'₽', CNY:'¥'}[c] || '';
    return sym + Number(v).toLocaleString('en-US', {maximumFractionDigits: 0});
  };
  const fmtDate = (s) => {
    if (!s) return '';
    try { return new Date(s).toLocaleDateString('ru-RU', {day:'2-digit',month:'long',year:'numeric'}); }
    catch(e) { return s; }
  };

  function isMobile() { return window.innerWidth <= 768; }
  window.toggleSidebar = (force) => {
    const sb = $('sidebar');
    const open = force === undefined ? !sb.classList.contains('open') : force;
    sb.classList.toggle('open', open);
    if (!isMobile()) {
      try { localStorage.setItem(SB_KEY, open ? '1' : '0'); } catch(e){}
    }
  };

  // Restore sidebar state
  try {
    const saved = localStorage.getItem(SB_KEY);
    if (saved === '1' && !isMobile()) $('sidebar').classList.add('open');
  } catch(e){}

  async function api(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(path + ' → ' + r.status);
    return r.json();
  }

  // ── Sidebar load (projects + conversations) ──
  async function loadSidebar() {
    try {
      const cfg = await api('/api/assistant/widget-config/');
      const name = cfg.user_name || 'User';
      $('sideAvatar').textContent = name[0].toUpperCase();
      $('topAvatar').textContent = name[0].toUpperCase();
      $('sideUserName').textContent = name;
      $('sideUserRole').textContent = (cfg.role || '').replace('operator_', '').replace(/_/g, ' ');
    } catch(e){}
    try {
      const data = await api('/api/assistant/projects/');
      const list = data.projects || [];
      const el = $('projectsList');
      if (!list.length) {
        el.innerHTML = '<div class="side-item" style="color:rgba(0,0,0,0.4);">Нет проектов</div>';
      } else {
        el.innerHTML = list.map(p =>
          `<a href="/chat/project/${esc(p.id)}/" class="side-item" style="text-decoration:none;">
             <span class="side-item-dot" style="background:${esc(p.dot_color || '#9ca3af')};"></span>
             <span class="side-item-text">${esc(p.name)}</span>
             ${p.chats_count ? `<span class="side-item-meta">${p.chats_count}</span>` : ''}
           </a>`
        ).join('');
      }
    } catch(e){}
    try {
      const r = await fetch('/api/assistant/conversations/');
      const data = await r.json();
      const list = data.results || data || [];
      const el = $('convList');
      if (!list.length) {
        el.innerHTML = '';
      } else {
        el.innerHTML = list.slice(0, 8).map(c => {
          const time = c.updated_at ? new Date(c.updated_at).toLocaleString('ru', {hour:'2-digit',minute:'2-digit'}) : '';
          return `<a href="/chat/?conv=${esc(c.id)}" class="side-item-stack" style="text-decoration:none;display:flex;">
             <span class="side-item-stack-title">${esc(c.title || 'Без названия')}</span>
             <span class="side-item-stack-meta">${esc(time)}</span>
           </a>`;
        }).join('');
      }
    } catch(e){}
  }

  // ── RFQ render ──
  function statusLabel(s) {
    return ({new:'Новый', quoted:'С котировками', needs_review:'Требует проверки', cancelled:'Отменён'}[s] || s);
  }
  function itemStateClass(state) {
    if (state === 'matched' || state === 'quoted') return 'matched';
    if (state === 'no_match' || state === 'unknown') return 'no_match';
    return 'pending';
  }

  function renderRFQ(d) {
    const items = d.items || [];
    const total = items.reduce((sum, it) => sum + (Number(it.price) || 0) * (Number(it.qty) || 1), 0);
    const matchedCount = items.filter(it => (it.state === 'matched' || it.state === 'quoted')).length;
    const noMatchCount = items.filter(it => it.state === 'no_match').length;
    const supplierSet = new Set(items.map(it => it.supplier).filter(Boolean));
    const status = d.status || 'new';

    const html = `
      <div class="crumbs">
        <a href="/chat/">Чат</a>
        <span class="crumbs-sep">/</span>
        <span>RFQ #${esc(d.id)}</span>
      </div>

      <div class="rfq-head">
        <div class="rfq-head-left">
          <div class="rfq-title-row">
            <h1 class="rfq-name">RFQ #${esc(d.id)}</h1>
            <span class="rfq-status ${esc(status)}">${esc(statusLabel(status))}</span>
          </div>
          <div class="rfq-meta">
            <span><span class="rfq-meta-strong">${esc(d.customer_name || '—')}</span></span>
            ${d.created_at ? `<span>${esc(fmtDate(d.created_at))}</span>` : ''}
            ${d.mode ? `<span>Mode: <span class="rfq-meta-strong">${esc((d.mode || '').toUpperCase())}</span></span>` : ''}
            ${d.urgency ? `<span>Urgency: <span class="rfq-meta-strong">${esc(d.urgency)}</span></span>` : ''}
          </div>
        </div>
        <div class="rfq-actions">
          <button class="rfq-btn" onclick="window.history.back()">Назад</button>
          <button class="rfq-btn primary" onclick="window.location.href='/chat/'">Обсудить в чате</button>
        </div>
      </div>

      <div class="kpi-grid">
        <div class="kpi">
          <div class="kpi-label">Всего позиций</div>
          <div class="kpi-value"><span class="kpi-num">${items.length}</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">С котировками</div>
          <div class="kpi-value"><span class="kpi-num kpi-good">${matchedCount}</span><span class="kpi-unit">из ${items.length}</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Поставщиков</div>
          <div class="kpi-value"><span class="kpi-num">${supplierSet.size}</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Без совпадений</div>
          <div class="kpi-value"><span class="kpi-num ${noMatchCount ? 'kpi-warn' : ''}">${noMatchCount}</span></div>
        </div>
      </div>

      <div class="sec-title">
        <h2>Позиции</h2>
        <span class="sec-title-count">${items.length}</span>
      </div>

      <div class="items">
        ${items.length === 0 ? '<div class="loading">Нет позиций</div>' :
          items.map(it => `
            <div class="item">
              <span class="item-state ${itemStateClass(it.state)}"></span>
              <div class="item-info">
                <div class="item-art">${esc(it.article || '—')}</div>
                ${it.match ? `<div class="item-match">Match: <strong>${esc(it.match)}</strong>${it.brand ? ' · ' + esc(it.brand) : ''}</div>` : ''}
              </div>
              <div class="item-qty">× ${esc(it.qty || 1)}</div>
              <div class="item-supplier">${esc(it.supplier || '')}</div>
              <div class="item-price">${fmtMoney(it.price, it.currency)}</div>
            </div>`).join('')
        }
      </div>

      <div class="total-bar">
        <span class="total-label">Итого</span>
        <span class="total-val">${fmtMoney(total, items[0]?.currency || 'USD')}</span>
      </div>
    `;
    $('rfqContent').innerHTML = html;
  }

  async function loadRFQ() {
    try {
      const data = await api('/api/assistant/rfq/' + window.RFQ_ID + '/');
      renderRFQ(data);
    } catch(e) {
      $('rfqContent').innerHTML = `<div class="loading">⚠️ Не удалось загрузить RFQ: ${esc(e.message)}</div>`;
    }
  }

  loadSidebar();
  loadRFQ();
})();
