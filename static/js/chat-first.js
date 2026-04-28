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

  function addTyping() {
    showConv();
    const wrap = document.createElement('div');
    wrap.className = 'msg';
    wrap.id = 'typingMsg';
    wrap.innerHTML = `${avatar('assistant')}<div class="msg-body"><div class="typing"><span></span><span></span><span></span></div></div>`;
    $('streamInner').appendChild(wrap);
    scrollBottom();
  }

  function removeTyping() {
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
          addTyping();
        } else if (d.type === 'stream') {
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

    addMessage('user', text);
    inp.value = '';
    inp.style.height = 'auto';
    state.streaming = true;
    $('sendBtn').disabled = true;
    $('heroSendBtn').disabled = true;

    // Focus the conv input after switching
    setTimeout(() => $('input').focus(), 100);

    if (state.ws && state.ws.readyState === 1) {
      state.ws.send(JSON.stringify({type:'message', content:text}));
    } else {
      try {
        const r = await api('/api/assistant/chat/', {
          method:'POST',
          body: JSON.stringify({conversation_id: state.convId, message: text}),
        });
        state.convId = r.conversation_id;
        addMessage('assistant', r.response, r.cards, r.actions);
        state.streaming = false;
        $('sendBtn').disabled = false;
        $('heroSendBtn').disabled = false;
        loadConvList();
      } catch(e) {
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
    addMessage('action', '▸ ' + btn.dataset.label);
    addTyping();
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
      if (state.config.latest_conversation_id) {
        state.convId = state.config.latest_conversation_id;
        await openConv(state.convId);
      }
      await loadConvList();
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
