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

  // Status-banner: главное действие зависит от stage + mode RFQ.
  // 3 режима в порядке приоритета:
  //   AUTO       — платформа сама рассылает + собирает котировки. Buyer ждёт.
  //   SEMI       — AI подобрал кандидатов; buyer подтверждает рассылку.
  //   MANUAL_OEM — buyer сам выбирает поставщиков и шлёт.
  function buildStageBanner(d) {
    const stage = d.stage || 'draft';
    const mode = (d.mode || 'auto').toLowerCase();
    const sent = d.sent_count || 0;
    const quotes = d.quotes_count || 0;
    const isOwner = d.is_owner !== false;
    const rfqId = d.id;

    if (stage === 'cancelled') {
      return {tone: 'gray', emoji: '✗', title: 'RFQ отменён',
              sub: 'Этот запрос был отменён. Создайте новый.', cta: null};
    }
    if (stage === 'needs_review') {
      return {tone: 'warn', emoji: '⚠️', title: 'Требует проверки оператором',
              sub: 'Часть позиций нужно вручную сматчить. Оператор скоро свяжется.', cta: null};
    }
    if (stage === 'quotes_received') {
      return {
        tone: 'green', emoji: '💬',
        title: `Получено ${quotes} котировок`,
        sub: 'Сравните оффера и выберите лучший — продавец примется за заказ.',
        cta: isOwner ? {
          label: `📊 Просмотреть котировки (${quotes})`,
          action: 'view_rfq_quotes', params: {rfq_id: rfqId},
        } : null,
        primary: true,
      };
    }
    if (stage === 'awaiting_quotes') {
      // AUTO ждёт сам; SEMI/MANUAL может разослать ещё
      if (mode === 'auto') {
        return {
          tone: 'blue', emoji: '🤖',
          title: 'AI собирает котировки автоматически',
          sub: `Запрос ушёл ${sent} поставщикам · ждём ответы. Обычно 6–24 часа. Уведомления приходят сразу.`,
          cta: null,
        };
      }
      return {
        tone: 'blue', emoji: '⏳',
        title: 'Запрос разослан · ждём котировки',
        sub: `Разослано ${sent} поставщикам. Обычно отвечают за 6–24 часа.`,
        cta: isOwner ? {
          label: '📨 Разослать ещё поставщикам',
          action: 'send_rfq_to_suppliers', params: {rfq_id: rfqId},
        } : null,
      };
    }
    // draft (создан, не разослан) — поведение зависит от mode
    if (mode === 'auto') {
      // В норме при mode=auto draft не должен случиться (auto-send в create_rfq).
      // Но если случилось (timeout / network error) — даём ручной trigger.
      return {
        tone: 'blue', emoji: '🤖',
        title: 'AUTO режим · готовим рассылку',
        sub: 'AI скоро автоматически разошлёт RFQ верифицированным поставщикам.',
        cta: isOwner ? {
          label: '🚀 Запустить рассылку сейчас',
          action: 'send_rfq_to_suppliers', params: {rfq_id: rfqId},
        } : null,
      };
    }
    if (mode === 'manual_oem') {
      return {
        tone: 'orange', emoji: '🎯',
        title: 'MANUAL OEM режим · выберите получателей',
        sub: 'Вы создали запрос на конкретные OEM-номера. Выберите кому разослать.',
        cta: isOwner ? {
          label: '🎯 Разослать выбранным',
          action: 'send_rfq_to_suppliers', params: {rfq_id: rfqId},
        } : null,
        primary: true,
      };
    }
    // SEMI (default fallback for non-auto, non-manual)
    return {
      tone: 'orange', emoji: '📨',
      title: 'SEMI режим · подтвердите рассылку',
      sub: 'AI подобрал кандидатов-поставщиков. Подтвердите чтобы они получили запрос.',
      cta: isOwner ? {
        label: '📨 Разослать кандидатам',
        action: 'send_rfq_to_suppliers', params: {rfq_id: rfqId},
      } : null,
      primary: true,
    };
  }

  function renderStageBanner(b) {
    const ctaHtml = b.cta
      ? `<a class="banner-cta ${b.primary ? 'primary' : ''}" href="/chat/?run=${esc(b.cta.action)}&rfq_id=${esc(b.cta.params.rfq_id)}">${esc(b.cta.label)} →</a>`
      : '';
    return `<div class="stage-banner stage-${b.tone}">
      <div class="stage-emoji">${b.emoji}</div>
      <div class="stage-text">
        <div class="stage-title">${esc(b.title)}</div>
        <div class="stage-sub">${esc(b.sub)}</div>
      </div>
      ${ctaHtml}
    </div>`;
  }

  function renderRFQ(d) {
    const items = d.items || [];
    const matchedCount = items.filter(it => (it.state === 'matched' || it.state === 'quoted')).length;
    const noMatchCount = items.filter(it => it.state === 'no_match').length;
    const status = d.status || 'new';
    const totalUsd = Number(d.total_usd || 0);
    const banner = buildStageBanner(d);

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
            ${d.urgency && d.urgency !== 'standard' ? `<span>Срочность: <span class="rfq-meta-strong">${esc(d.urgency)}</span></span>` : ''}
          </div>
        </div>
      </div>

      ${renderStageBanner(banner)}

      ${d.has_priced ? `
      <div class="hero-total">
        <div class="hero-total-label">Ориентировочный бюджет (USD)</div>
        <div class="hero-total-val">${fmtMoney(totalUsd, 'USD')}</div>
        <div class="hero-total-sub">${items.length} позиций · цены конвертированы из исходных валют поставщиков</div>
      </div>` : `
      <div class="hero-total hero-total-empty">
        <div class="hero-total-label">Бюджет</div>
        <div class="hero-total-val" style="font-size:24px;color:rgba(0,0,0,0.5);">Уточняется</div>
        <div class="hero-total-sub">Цены появятся после получения котировок от поставщиков</div>
      </div>`}

      <div class="kpi-grid">
        <div class="kpi">
          <div class="kpi-label">Позиций</div>
          <div class="kpi-value"><span class="kpi-num">${items.length}</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Сматчено</div>
          <div class="kpi-value"><span class="kpi-num kpi-good">${matchedCount}</span><span class="kpi-unit">из ${items.length}</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Разослано</div>
          <div class="kpi-value"><span class="kpi-num">${d.sent_count || 0}</span><span class="kpi-unit">пост.</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Котировок</div>
          <div class="kpi-value"><span class="kpi-num ${(d.quotes_count || 0) > 0 ? 'kpi-good' : ''}">${d.quotes_count || 0}</span></div>
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

      <div class="rfq-actions" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:24px;justify-content:flex-end;">
        <button class="rfq-btn" onclick="window.history.back()">← Назад</button>
        <button class="rfq-btn" onclick="window.location.href='/chat/'">💬 Обсудить в чате</button>
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
