/* Chat-First UI — minimalist gradient design.
 * Two states: empty hero (centered floating input) → conv (messages + bottom input)
 */
(function(){
  'use strict';

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
  };

  // ── Helpers ──────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const csrf = () => document.cookie.replace(/(?:(?:^|.*;\s*)csrftoken\s*=\s*([^;]*).*$)|^.*$/, '$1');
  const esc = s => (s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
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

  // ── Card renderers ───────────────────────────────────────
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
    order(d) {
      const cls = ({pending:'orange', shipped:'green', completed:'green', cancelled:'gray'})[d.status_code] || '';
      return `<div class="card">
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
      return `<div class="card">
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
  };

  function renderCards(cards) {
    if (!cards || !cards.length) return '';
    return '<div class="cards">' + cards.map(c => {
      const r = renderers[c.type];
      return r ? r(c.data || {}) : '';
    }).join('') + '</div>';
  }

  function renderActions(actions) {
    if (!actions || !actions.length) return '';
    return '<div class="actions">' + actions.map(a =>
      `<button class="act-btn" data-action="${esc(a.action)}" data-params='${esc(JSON.stringify(a.params || {}))}' data-label="${esc(a.label)}">${esc(a.label)}</button>`
    ).join('') + '</div>';
  }

  function avatar(role) {
    if (role === 'user') return '<div class="msg-avatar msg-avatar-user">' + ((state.config && state.config.user_name || 'U')[0].toUpperCase()) + '</div>';
    if (role === 'action') return '<div class="msg-avatar msg-avatar-act">▸</div>';
    return '<div class="msg-avatar msg-avatar-bot">AI</div>';
  }

  function authorLabel(role) {
    if (role === 'user') return state.config ? state.config.user_name : 'Вы';
    if (role === 'action') return 'Действие';
    return 'Consolidator AI';
  }

  // ── State transition ────────────────────────────────────
  function showConv() {
    $('emptyStage').classList.add('hidden');
    $('convStage').classList.remove('hidden');
  }

  function showEmpty() {
    $('emptyStage').classList.remove('hidden');
    $('convStage').classList.add('hidden');
    $('streamInner').innerHTML = '';
  }

  // ── Render messages ──────────────────────────────────────
  function addMessage(role, content, cards=[], actions=[]) {
    showConv();
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-' + role;
    wrap.innerHTML = `
      ${avatar(role)}
      <div class="msg-body">
        <div class="msg-author">${esc(authorLabel(role))}</div>
        <div class="msg-content${role === 'action' ? ' msg-action-tag' : ''}"></div>
        <div class="msg-cards"></div>
        <div class="msg-actions"></div>
      </div>
    `;
    wrap.querySelector('.msg-content').textContent = content || '';
    wrap.querySelector('.msg-cards').innerHTML = renderCards(cards);
    wrap.querySelector('.msg-actions').innerHTML = renderActions(actions);
    $('streamInner').appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  // ── Working indicator (spinning logo + rotating status text) ──
  // Status messages by intent — picked based on user's message or action name
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
    if (/(search|find|find_|искать|найти|подобрать|катал|запчаст|товар|оем|oem|brand)/i.test(t)) return 'search';
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
          <div class="working-logo">
            <svg viewBox="0 0 32 32" fill="none">
              <circle cx="16" cy="16" r="13" stroke="currentColor" stroke-width="2.5"/>
              <circle cx="16" cy="16" r="5"/>
            </svg>
          </div>
          <span class="working-text" id="workingText">${esc(messages[0])}</span>
        </div>
      </div>`;
    $('streamInner').appendChild(wrap);
    scrollBottom();

    // Cycle through messages every 1.8s
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

  function appendStream(text) {
    if (!state.currentBubble) {
      removeTyping();
      const wrap = document.createElement('div');
      wrap.className = 'msg';
      wrap.innerHTML = `${avatar('assistant')}<div class="msg-body"><div class="msg-author">Consolidator AI</div><div class="msg-content"></div><div class="msg-cards"></div><div class="msg-actions"></div></div>`;
      $('streamInner').appendChild(wrap);
      state.currentBubble = wrap;
    }
    const el = state.currentBubble.querySelector('.msg-content');
    el.textContent += text;
    scrollBottom();
  }

  function finishStream(cards, actions) {
    removeTyping();
    if (!state.currentBubble) return;
    const text = state.currentBubble.querySelector('.msg-content').textContent;
    state.currentBubble.querySelector('.msg-content').textContent = text.replace(/\[card:\w+\]/g, '').trim();
    state.currentBubble.querySelector('.msg-cards').innerHTML = renderCards(cards);
    state.currentBubble.querySelector('.msg-actions').innerHTML = renderActions(actions);
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

  // ── WebSocket ────────────────────────────────────────────
  function connectWS() {
    if (state.ws && state.ws.readyState <= 1) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const path = state.convId ? `/ws/assistant/${state.convId}/` : '/ws/assistant/';
    try { state.ws = new WebSocket(proto + '//' + location.host + path); } catch(e) { return; }

    state.ws.onopen = () => {
      state.wsRetry = 0;
      $('wsStatus').className = 'ws-pill live';
      $('wsStatusText').textContent = 'На связи';
    };
    state.ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type === 'connected') {
          state.convId = d.conversation_id;
          loadConvList();
        } else if (d.type === 'thinking') {
          // intent already set in send() via state._intent
          // (don't add another typing if we already added one)
          if (!$('typingMsg')) addTyping(state._intent);
        } else if (d.type === 'stream') {
          removeTyping();
          appendStream(d.content);
        } else if (d.type === 'cards') {
          state._lastCards = d.cards || [];
          state._lastActions = d.actions || [];
        } else if (d.type === 'done') {
          finishStream(state._lastCards, state._lastActions);
          state._lastCards = []; state._lastActions = [];
        } else if (d.type === 'error') {
          finishStream([], []);
          addMessage('assistant', '⚠️ ' + d.message);
        }
      } catch(e){ console.error(e); }
    };
    state.ws.onclose = (ev) => {
      $('wsStatus').className = 'ws-pill';
      if (ev.code === 4401) {
        $('wsStatusText').textContent = 'Войдите в систему';
        return;
      }
      state.wsRetry++;
      $('wsStatusText').textContent = 'Подключение...';
      const delay = Math.min(1000 * Math.pow(2, state.wsRetry), 30000);
      setTimeout(connectWS, delay);
    };
  }

  // ── Send message ─────────────────────────────────────────
  async function send(fromHero) {
    const inp = fromHero ? $('heroInput') : $('input');
    const text = inp.value.trim();
    if (!text || state.streaming) return;

    const intent = pickIntent(text);
    addMessage('user', text);
    inp.value = '';
    inp.style.height = 'auto';
    state.streaming = true;
    $('sendBtn').disabled = true;
    $('heroSendBtn').disabled = true;
    state._intent = intent;

    // Focus the conv input after switching
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
        state.convId = r.conversation_id;
        addMessage('assistant', r.response, r.cards, r.actions);
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

  // ── Action button click ──────────────────────────────────
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.act-btn');
    if (!btn) return;
    const action = btn.dataset.action;
    const params = JSON.parse(btn.dataset.params || '{}');
    params._label = btn.dataset.label;
    const intent = pickIntent(action);
    addMessage('action', '▸ ' + btn.dataset.label);
    addTyping(intent);
    try {
      const r = await api('/api/assistant/action/', {
        method:'POST',
        body: JSON.stringify({conversation_id: state.convId, action, params}),
      });
      removeTyping();
      addMessage('assistant', r.text, r.cards, r.actions);
      if (r.suggestions) renderConvSuggest(r.suggestions);
    } catch(err) {
      removeTyping();
      addMessage('assistant', '⚠️ ' + err.message);
    }
  });

  // ── Suggestions ──────────────────────────────────────────
  function renderHeroSuggest(suggestions) {
    $('heroSuggest').innerHTML = (suggestions || []).map(s =>
      `<button class="sug" onclick="chatAsk('${esc(s).replace(/'/g,"\\'")}', true)">${esc(s)}</button>`
    ).join('');
  }

  // ── Quick action cards (home page) ───────────────────────
  const ROLE_ACTIONS = {
    buyer: [
      {icon:'🔍', title:'Найти запчасть', desc:'OEM-номер, бренд или название', action:'search_parts', params:{query:''}},
      {icon:'📋', title:'Создать RFQ', desc:'Запрос котировки поставщикам', action:'create_rfq', params:{quantity:1}},
      {icon:'📦', title:'Мои заказы', desc:'История и статус', action:'get_orders', params:{}},
      {icon:'🚢', title:'Трекинг отгрузок', desc:'Где сейчас груз', action:'track_shipment', params:{}},
      {icon:'💰', title:'Бюджет', desc:'Расходы и остатки', action:'get_budget', params:{}},
      {icon:'📊', title:'Аналитика', desc:'Сводка по платформе', action:'get_analytics', params:{}},
    ],
    seller: [
      {icon:'📥', title:'Новые RFQ', desc:'Входящие запросы котировок', action:'get_rfq_status', params:{}},
      {icon:'📦', title:'Мои заказы', desc:'Активные сделки', action:'get_orders', params:{}},
      {icon:'📈', title:'Аналитика спроса', desc:'Что ищут клиенты', action:'get_demand_report', params:{}},
      {icon:'⏱', title:'Мой SLA', desc:'KPI скорости ответа', action:'get_sla_report', params:{}},
      {icon:'💼', title:'Мой каталог', desc:'Управление товарами', action:'search_parts', params:{query:''}},
      {icon:'📊', title:'Выручка', desc:'Аналитика продаж', action:'get_analytics', params:{}},
    ],
    operator_logist: [
      {icon:'🚢', title:'Отгрузки в пути', desc:'Активный трекинг', action:'track_shipment', params:{}},
      {icon:'📦', title:'Все заказы', desc:'Статусы и приоритеты', action:'get_orders', params:{}},
      {icon:'⚠️', title:'SLA нарушения', desc:'Требуют внимания', action:'get_sla_report', params:{}},
      {icon:'📊', title:'Аналитика', desc:'Метрики логистики', action:'get_analytics', params:{}},
    ],
    operator_customs: [
      {icon:'🛃', title:'На таможне', desc:'Грузы ожидающие растаможки', action:'track_shipment', params:{}},
      {icon:'📦', title:'Все заказы', desc:'Полный список', action:'get_orders', params:{}},
      {icon:'📊', title:'Аналитика', desc:'Метрики таможни', action:'get_analytics', params:{}},
    ],
    operator_payment: [
      {icon:'💰', title:'Платежи', desc:'Бюджеты и расходы', action:'get_budget', params:{}},
      {icon:'📦', title:'Заказы по статусу', desc:'Оплаты в работе', action:'get_orders', params:{}},
      {icon:'📊', title:'Аналитика', desc:'Финансовая сводка', action:'get_analytics', params:{}},
    ],
    operator_manager: [
      {icon:'📋', title:'Активные RFQ', desc:'Входящие запросы', action:'get_rfq_status', params:{}},
      {icon:'📦', title:'Все заказы', desc:'Воронка продаж', action:'get_orders', params:{}},
      {icon:'🏭', title:'Поставщики', desc:'Сравнение и рейтинги', action:'compare_suppliers', params:{}},
      {icon:'📈', title:'Спрос', desc:'Тренды по категориям', action:'get_demand_report', params:{}},
      {icon:'⏱', title:'SLA отчёт', desc:'KPI команды', action:'get_sla_report', params:{}},
      {icon:'📊', title:'Аналитика', desc:'Сводка платформы', action:'get_analytics', params:{}},
    ],
    admin: [
      {icon:'📊', title:'Метрики платформы', desc:'Полная сводка', action:'get_analytics', params:{}},
      {icon:'📦', title:'Все заказы', desc:'Глобальный список', action:'get_orders', params:{}},
      {icon:'📋', title:'Все RFQ', desc:'Активные запросы', action:'get_rfq_status', params:{}},
      {icon:'⏱', title:'SLA нарушения', desc:'Критичные', action:'get_sla_report', params:{}},
    ],
  };

  function renderHomeActions(role) {
    const items = ROLE_ACTIONS[role] || ROLE_ACTIONS.buyer;
    $('homeActions').innerHTML = items.map(it =>
      `<button class="home-act" data-act="${esc(it.action)}" data-prm='${esc(JSON.stringify(it.params))}' data-lab="${esc(it.title)}">
        <div class="home-act-icon">${it.icon}</div>
        <div class="home-act-title">${esc(it.title)}</div>
        <div class="home-act-desc">${esc(it.desc)}</div>
      </button>`
    ).join('');
  }

  function renderHomeRecent() {
    if (!state.convs || !state.convs.length) {
      $('homeRecent').style.display = 'none';
      return;
    }
    const top = state.convs.slice(0, 4);
    $('homeRecent').style.display = 'block';
    $('homeRecentList').innerHTML = top.map(c => {
      const date = c.updated_at ? new Date(c.updated_at).toLocaleDateString('ru-RU', {day:'2-digit', month:'short'}) : '';
      return `<div class="home-recent-item" onclick="openConv('${c.id}')">
        <div class="home-recent-title">${esc(c.title || 'Без названия')}</div>
        <div class="home-recent-meta">${esc(date)}</div>
      </div>`;
    }).join('');
  }

  // Click handler for home action cards
  document.addEventListener('click', async (e) => {
    const card = e.target.closest('.home-act');
    if (!card) return;
    const action = card.dataset.act;
    const params = JSON.parse(card.dataset.prm || '{}');
    params._label = card.dataset.lab;
    addMessage('action', '▸ ' + card.dataset.lab);
    addTyping(pickIntent(action));
    try {
      const r = await api('/api/assistant/action/', {
        method:'POST',
        body: JSON.stringify({conversation_id: state.convId, action, params}),
      });
      removeTyping();
      addMessage('assistant', r.text, r.cards, r.actions);
      if (r.suggestions) renderConvSuggest(r.suggestions);
      state.convId = r.conversation_id || state.convId;
      loadConvList();
    } catch(err) {
      removeTyping();
      addMessage('assistant', '⚠️ ' + err.message);
    }
  });

  function renderConvSuggest(suggestions) {
    $('convSuggest').innerHTML = (suggestions || []).slice(0, 3).map(s =>
      `<button class="sug" onclick="chatAsk('${esc(s).replace(/'/g,"\\'")}', false)">${esc(s)}</button>`
    ).join('');
  }

  window.chatAsk = (text, fromHero) => {
    const inp = fromHero ? $('heroInput') : $('input');
    inp.value = text;
    send(fromHero);
  };

  // ── Conversations sidebar ────────────────────────────────
  async function loadConvList() {
    try {
      const r = await fetch('/api/assistant/conversations/');
      const data = await r.json();
      state.convs = data.results || data;
      renderConvList();
      renderHomeRecent();
    } catch(e){}
  }

  function renderConvList(filter='') {
    const f = filter.toLowerCase();
    const list = state.convs.filter(c => !f || (c.title||'').toLowerCase().includes(f));
    if (!list.length) {
      $('convList').innerHTML = '<div class="side-group">Нет чатов</div>';
      return;
    }
    $('convList').innerHTML = '<div class="side-group">Недавние</div>' + list.map(c =>
      `<div class="side-item${c.id === state.convId ? ' active' : ''}" onclick="openConv('${c.id}')">${esc(c.title || 'Без названия')}</div>`
    ).join('');
  }

  window.filterConvs = renderConvList;

  window.openConv = async (id) => {
    state.convId = id;
    showConv();
    $('streamInner').innerHTML = '';
    if (state.ws) { try { state.ws.close(); } catch(e){} }
    try {
      const data = await api('/api/assistant/conversations/' + id + '/');
      $('convTitle').textContent = data.title || '';
      (data.messages || []).forEach(m => addMessage(m.role, m.content, m.cards, m.actions));
    } catch(e){}
    connectWS();
    renderConvList($('convSearch').value);
    toggleSidebar(false);
  };

  window.newChat = () => {
    state.convId = null;
    showEmpty();
    if (state.ws) { try { state.ws.close(); } catch(e){} }
    $('convTitle').textContent = '';
    connectWS();
    renderConvList();
    toggleSidebar(false);
    setTimeout(() => $('heroInput').focus(), 100);
  };

  window.toggleSidebar = (force) => {
    const sb = $('sidebar');
    const ov = $('sideOverlay');
    const open = force === undefined ? !sb.classList.contains('open') : force;
    sb.classList.toggle('open', open);
    ov.classList.toggle('open', open);
  };

  // ── Voice input ──────────────────────────────────────────
  let recog = null;
  window.toggleVoice = () => {
    if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
      alert('Голосовой ввод не поддерживается');
      return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (recog) { recog.stop(); recog = null; return; }
    recog = new SR();
    recog.lang = document.documentElement.lang || 'ru-RU';
    recog.interimResults = true;
    recog.onresult = (e) => {
      const text = Array.from(e.results).map(r => r[0].transcript).join('');
      const target = $('emptyStage').classList.contains('hidden') ? $('input') : $('heroInput');
      target.value = text;
    };
    recog.onend = () => { recog = null; };
    recog.start();
  };

  // ── File upload ──────────────────────────────────────────
  $('fileInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    addMessage('user', '📎 ' + file.name + ' (' + Math.round(file.size/1024) + ' KB)');
    addMessage('assistant', 'Обработка файлов будет в Phase 2. Опишите запрос текстом.');
    e.target.value = '';
  });

  // ── Greeting based on time ───────────────────────────────
  function setGreeting() {
    const h = new Date().getHours();
    const name = state.config ? state.config.user_name.split(' ')[0] : '';
    let g;
    if (h < 6) g = 'Доброй ночи';
    else if (h < 12) g = 'Доброе утро';
    else if (h < 18) g = 'Добрый день';
    else g = 'Добрый вечер';
    $('greetingText').textContent = name ? `${g}, ${name}` : `${g}`;
  }

  // ── Init ─────────────────────────────────────────────────
  async function init() {
    try {
      state.config = await api('/api/assistant/widget-config/');
      $('userName').textContent = state.config.user_name || 'User';
      $('userRole').textContent = state.config.role || '';
      $('userAvatar').textContent = (state.config.user_name || 'U')[0].toUpperCase();
      setGreeting();
      renderHeroSuggest(state.config.suggestions);
      renderConvSuggest(state.config.suggestions);
      renderHomeActions(state.config.role);
      // Don't auto-open latest conversation — show home first
      await loadConvList();
      renderHomeRecent();
    } catch(e) {
      console.warn('Init failed:', e);
    }
    connectWS();
    setTimeout(() => $('heroInput').focus(), 200);
  }

  // Auto-grow textareas
  document.addEventListener('input', (e) => {
    if (e.target.id === 'input' || e.target.id === 'heroInput') {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px';
    }
  });

  window.send = send;
  window.onKey = (e, fromHero) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(fromHero); }
  };

  document.addEventListener('DOMContentLoaded', init);
})();
