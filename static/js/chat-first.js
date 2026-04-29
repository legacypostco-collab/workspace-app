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

  // ══════════════════════════════════════════════════════════
  // Sidebar toggle
  // ══════════════════════════════════════════════════════════
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
      return r ? r(c.data || {}) : '';
    }).join('') + '</div>';
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

  function addMessage(role, content, cards=[], actions=[], contextRefs=[]) {
    showConv();
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-' + role;
    wrap.innerHTML = `
      ${avatar(role)}
      <div class="msg-body">
        <div class="msg-author">${esc(authorLabel(role))}</div>
        <div class="msg-content${role === 'action' ? ' msg-action-tag' : ''}"></div>
        <div class="msg-refs"></div>
        <div class="msg-cards"></div>
        <div class="msg-actions"></div>
      </div>
    `;
    wrap.querySelector('.msg-content').textContent = content || '';
    wrap.querySelector('.msg-refs').innerHTML = renderContextRefs(contextRefs);
    wrap.querySelector('.msg-cards').innerHTML = renderCards(cards);
    wrap.querySelector('.msg-actions').innerHTML = renderActions(actions);
    $('streamInner').appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  function appendStream(text) {
    if (!state.currentBubble) {
      removeTyping();
      const wrap = document.createElement('div');
      wrap.className = 'msg';
      wrap.innerHTML = `${avatar('assistant')}<div class="msg-body"><div class="msg-author">Consolidator</div><div class="msg-content"></div><div class="msg-refs"></div><div class="msg-cards"></div><div class="msg-actions"></div></div>`;
      $('streamInner').appendChild(wrap);
      state.currentBubble = wrap;
    }
    const el = state.currentBubble.querySelector('.msg-content');
    el.textContent += text;
    scrollBottom();
  }

  function finishStream(cards, actions, refs) {
    removeTyping();
    if (!state.currentBubble) return;
    const text = state.currentBubble.querySelector('.msg-content').textContent;
    state.currentBubble.querySelector('.msg-content').textContent = text.replace(/\[card:\w+\]/g, '').trim();
    state.currentBubble.querySelector('.msg-refs').innerHTML = renderContextRefs(refs || []);
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
          state.convId = d.conversation_id;
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
        } else if (d.type === 'done') {
          finishStream(state._lastCards, state._lastActions, state._lastRefs || d.refs);
          state._lastCards = []; state._lastActions = []; state._lastRefs = [];
        } else if (d.type === 'error') {
          finishStream([], []);
          addMessage('assistant', '⚠️ ' + d.message);
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
        state.convId = r.conversation_id;
        addMessage('assistant', r.response, r.cards, r.actions, r.context_refs || []);
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

  // Quick action from pills/cards
  window.quickAction = async (action, params) => {
    params = params || {};
    params._label = params._label || action;
    addMessage('action', '▸ ' + (params._label || action));
    addTyping(pickIntent(action));
    try {
      const r = await api('/api/assistant/action/', {
        method:'POST',
        body: JSON.stringify({conversation_id: state.convId, action, params}),
      });
      removeTyping();
      state.convId = r.conversation_id || state.convId;
      addMessage('assistant', r.text, r.cards, r.actions, r.context_refs || []);
      loadConvList();
    } catch(err) {
      removeTyping();
      addMessage('assistant', '⚠️ ' + err.message);
    }
  };

  // Click handler for action buttons inside messages
  document.addEventListener('click', async (e) => {
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
    state.convId = id;
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
    state.convId = null;
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
  window.toggleVoice = () => {
    if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
      alert('Голосовой ввод не поддерживается этим браузером');
      return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (recog) { recog.stop(); recog = null; return; }
    recog = new SR();
    recog.lang = document.documentElement.lang === 'en' ? 'en-US' : 'ru-RU';
    recog.interimResults = true;
    recog.onresult = (e) => {
      const text = Array.from(e.results).map(r => r[0].transcript).join('');
      const target = $('welcomeStage').classList.contains('hidden') ? $('input') : $('heroInput');
      target.value = text;
      updateHeroIcon();
    };
    recog.onend = () => { recog = null; };
    recog.start();
  };

  $('fileInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    addMessage('user', '📎 ' + file.name + ' (' + Math.round(file.size/1024) + ' KB)');
    addMessage('assistant', 'Обработка файлов будет в Phase 2. Опишите запрос текстом.');
    e.target.value = '';
  });

  $('photoInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    addMessage('user', '📷 ' + file.name);
    addMessage('assistant', 'Распознавание фото деталей будет в Phase 2.');
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
      await Promise.all([loadConvList(), loadProjects()]);
      applyDefaultSidebar(state.convs.length > 0);
    } catch(e) {
      console.warn('Init failed:', e);
      applyDefaultSidebar(false);
    }
    // Auto-open conversation from URL (?conv=<uuid>)
    try {
      const params = new URLSearchParams(window.location.search);
      const cid = params.get('conv');
      if (cid) {
        await window.openConv(cid);
        return;
      }
    } catch(e){}
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
