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
    green:'#22c55e', orange:'#f97316', blue:'#E84A21',
    purple:'#8d8d8d', red:'#ef4444', gray:'#9ca3af',
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
    // Памятка «зачем проект» по роли/подроли (как в /chat/).
    const r = window.__role || 'buyer';
    let note, ph;
    if (r.indexOf('operator') === 0) {
      const sub = r.replace('operator_', '').replace('operator', 'manager') || 'manager';
      const ON = {
        manager: '🎛 Проект — это сделка / консолидированная поставка. Контракты, таможня, логистика, платежи — вся поставка от RFQ до доставки.',
        logist:  '🚚 Проект — поставка с фокусом на доставке. Логистика (BL/CMR, маршрут) и статус таможни — все отгрузки сделки под контролем.',
        customs: '🛂 Проект — поставка с фокусом на растаможке. Декларации, HS-коды, инвойсы, сертификаты — таможня по всей сделке.',
        payment: '💳 Проект — поставка с фокусом на финансах. Инвойсы, эскроу, акты, выплаты — деньги по сделке.',
      };
      note = ON[sub] || ON.manager; ph = 'напр. Сделка Урал Q3';
    } else if (r === 'seller') {
      note = '🏷 Проект — это ваше товарное направление. Соберите прайс, чертежи, сертификаты и фото по сегменту — быстрее КП и больше доверия покупателя.';
      ph = 'напр. Ходовка Komatsu';
    } else {
      note = '📦 Проект — это ваша закупка под технику/объект. Загрузите парк техники, историю и чертежи — AI точнее подберёт и соберёт RFQ в контексте проекта.';
      ph = 'напр. Парк Komatsu — Ковдор';
    }
    const name = (window.prompt(note + '\n\nНазвание проекта (' + ph + '):', '') || '').trim();
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
  // KPI-блоки кликабельны → плавный скролл к разделу + подсветка заголовка.
  // Надёжно через scrollBy(delta) — scrollIntoView нестабилен в .content.
  window.__projScrollTo = function(id) {
    const el = document.getElementById(id);
    if (!el) return;
    // Нативный scrollIntoView (instant) — браузер сам находит нужный скролл-
    // контейнер (.content) и доводит секцию до видимой области. smooth здесь
    // в .content не срабатывает, поэтому без него.
    el.scrollIntoView({block: 'start'});
    const h = el.querySelector('h2') || el;
    const oc = h.style.color;
    h.style.transition = 'color .25s';
    h.style.color = '#E84A21';
    setTimeout(() => { h.style.color = oc; }, 1000);
  };

  // Настройки проекта — модалка редактирования (PATCH /projects/<id>/update/).
  window.__openProjectSettings = function() {
    const info = window.__projInfo || {};
    const field = (name, label, val, ph) =>
      '<label style="display:block;margin-top:10px"><span style="display:block;font-size:12px;opacity:.6;margin-bottom:4px">' + label + '</span>'
      + '<input class="__ps-inp" data-field="' + name + '" type="text" value="' + esc(val || '') + '" placeholder="' + esc(ph || '') + '" style="width:100%;box-sizing:border-box;padding:9px 11px;border-radius:9px;border:1px solid rgba(0,0,0,.16);font:inherit"/></label>';
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:99999';
    ov.innerHTML =
      '<div style="background:#fff;color:#1a1a1a;width:min(94vw,480px);border-radius:16px;padding:20px;box-shadow:0 16px 50px rgba(0,0,0,.4)">'
      + '<div style="font-weight:700;font-size:17px;margin-bottom:6px">Настройки проекта</div>'
      + field('name', 'Название', info.name, 'Напр. Norilsk Q2')
      + field('code', 'Код', info.code, 'NORQ2')
      + field('customer', 'Заказчик', info.customer, 'Норникель — Кольская ГМК')
      + field('description', 'Описание', info.description, 'Кратко о проекте')
      + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">'
      + '<button type="button" class="__ps-cancel" style="padding:9px 16px;border-radius:9px;border:none;background:rgba(0,0,0,.07);font:inherit;cursor:pointer">Отмена</button>'
      + '<button type="button" class="__ps-save" style="padding:9px 18px;border-radius:9px;border:none;background:#E84A21;color:#fff;font:inherit;cursor:pointer">Сохранить</button>'
      + '</div></div>';
    const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(ov);
    ov.querySelector('.__ps-cancel').addEventListener('click', close);
    ov.querySelector('.__ps-save').addEventListener('click', () => {
      const data = {};
      ov.querySelectorAll('.__ps-inp').forEach(i => { data[i.dataset.field] = i.value.trim(); });
      const btn = ov.querySelector('.__ps-save'); btn.disabled = true; btn.textContent = '…';
      fetch('/api/assistant/projects/' + PID + '/update/', {
        method: 'PATCH', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf()}, credentials: 'same-origin',
        body: JSON.stringify(data),
      }).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(() => { close(); if (typeof loadProject === 'function') loadProject(); else location.reload(); })
        .catch(e => { btn.disabled = false; btn.textContent = 'Сохранить'; alert('Не удалось сохранить: ' + e.message); });
    });
  };

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

  // Открыть категорию документов как «папку» — модалка с карточками файлов.
  // Для чертежей в карточке есть 🔗-привязка артикула (поиск по общей базе).
  window.__openDocFolder = function(key) {
    const all = key === '__all__';
    const slot = all ? {label: 'Файлы проекта', icon: '📁'}
                     : ((window.__projSlots || []).find(s => s.key === key) || {label: 'Документы', icon: '📁'});
    const docs = all ? (window.__projDocs || []).slice()
                     : (window.__projDocs || []).filter(d => (d.doctype || 'other') === key);
    const cardHtml = (d) => {
      const canBind = !!d.drawing_id;
      return `<div class="__df-card" data-drawing-id="${esc(d.drawing_id || '')}" style="border:1px solid rgba(0,0,0,.08);border-radius:12px;background:#fff;overflow:hidden">
        <div style="display:flex;align-items:center;gap:12px;padding:12px 14px">
          <span style="font-size:22px">📄</span>
          <span style="flex:1;min-width:0">
            <span style="display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(d.name)}</span>
            <span style="display:block;font-size:12px;opacity:.55">${esc(d.doctype_label || '')}${d.size_kb ? ' · ' + esc(String(d.size_kb)) + ' КБ' : ''}<span class="__df-oemtag" style="color:#E84A21;font-weight:600">${d.oem ? ' · 🔗 ' + esc(d.oem) : ''}</span></span>
          </span>
          <a href="/api/assistant/projects/${PID}/documents/${d.id}/file/" target="_blank" rel="noopener" style="opacity:.6;font-size:13px;text-decoration:none;color:inherit;white-space:nowrap">Открыть ›</a>
        </div>
        ${canBind ? `<div style="padding:0 14px 12px">
          <button type="button" class="__df-bindbtn" style="font-size:13px;padding:5px 11px;border-radius:8px;border:1px solid rgba(0,0,0,.12);background:rgba(0,0,0,.03);cursor:pointer">🔗 <span class="__df-bindlabel">${d.oem ? 'Изменить артикул' : 'Привязать артикул'}</span></button>
          <div class="__df-bindpanel" style="display:none;margin-top:8px">
            <input class="__df-oeminput" type="text" placeholder="Артикул из общей базы — напр. 6D102" autocomplete="off" style="width:100%;box-sizing:border-box;padding:7px 10px;border-radius:8px;border:1px solid rgba(0,0,0,.14);font:inherit"/>
            <div class="__df-oemresults" style="display:flex;flex-direction:column;gap:4px;margin-top:6px;max-height:210px;overflow:auto"></div>
          </div>
        </div>` : ''}
      </div>`;
    };
    const cards = docs.map(cardHtml).join('') || '<div style="opacity:.6;padding:14px 4px">В этой папке пока нет файлов.</div>';
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:99999;animation:dffade .12s ease';
    ov.innerHTML = `
      <style>@keyframes dffade{from{opacity:0}to{opacity:1}}</style>
      <div style="background:#f5f5f7;color:#1a1a1a;width:min(94vw,560px);max-height:84vh;border-radius:16px;padding:18px;box-shadow:0 16px 50px rgba(0,0,0,.4);display:flex;flex-direction:column">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <span style="font-size:22px">${slot.icon || '📁'}</span>
          <span style="font-weight:700;font-size:17px;flex:1">${esc(slot.label || 'Документы')} <span style="opacity:.5;font-weight:500">· ${docs.length}</span></span>
          <button type="button" class="__df-add" style="padding:7px 13px;border-radius:9px;border:none;background:#E84A21;color:#fff;font:inherit;cursor:pointer">+ Добавить</button>
          <button type="button" class="__df-close" style="padding:7px 11px;border-radius:9px;border:none;background:rgba(0,0,0,.08);font:inherit;cursor:pointer">✕</button>
        </div>
        <div style="display:flex;flex-direction:column;gap:8px;overflow:auto">${cards}</div>
      </div>`;
    const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(ov);
    ov.querySelector('.__df-close').addEventListener('click', close);
    ov.querySelector('.__df-add').addEventListener('click', () => { close(); window.__uploadProjectDoc && window.__uploadProjectDoc(all ? undefined : key); });

    // 🔗 привязка артикула: раскрытие, поиск, выбор
    let _bt = null;
    ov.addEventListener('click', (e) => {
      const bb = e.target.closest('.__df-bindbtn');
      if (bb) {
        const card = bb.closest('.__df-card');
        const panel = card.querySelector('.__df-bindpanel');
        const show = panel.style.display === 'none';
        panel.style.display = show ? 'block' : 'none';
        if (show) card.querySelector('.__df-oeminput').focus();
        return;
      }
      const res = e.target.closest('.__df-oemres');
      if (res) {
        const card = res.closest('.__df-card');
        const did = card.dataset.drawingId, oem = res.dataset.oem;
        fetch('/api/assistant/action/', {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf()}, credentials:'same-origin',
          body: JSON.stringify({action:'bind_drawing', params:{drawing_id: did, oem}})}).then(() => {
          card.querySelector('.__df-oemtag').textContent = ' · 🔗 ' + oem;
          const lbl = card.querySelector('.__df-bindlabel'); if (lbl) lbl.textContent = 'Изменить артикул';
          card.querySelector('.__df-bindpanel').style.display = 'none';
          // обновим кэш, чтобы при переоткрытии папки артикул сохранился
          const dd = (window.__projDocs || []).find(x => x.drawing_id === did); if (dd) dd.oem = oem;
        }).catch(() => {});
      }
    });
    ov.addEventListener('input', (e) => {
      const inp = e.target.closest('.__df-oeminput');
      if (!inp) return;
      const card = inp.closest('.__df-card');
      const did = card.dataset.drawingId, q = (inp.value || '').trim();
      const out = card.querySelector('.__df-oemresults');
      clearTimeout(_bt);
      _bt = setTimeout(() => {
        fetch('/api/assistant/action/', {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf()}, credentials:'same-origin',
          body: JSON.stringify({action:'link_drawing', params:{drawing_id: did, q}})})
          .then(r => r.json()).then(resp => {
            if ((inp.value || '').trim() !== q) return;
            const c = (resp.cards || []).find(x => x.type === 'drawing_link');
            const rows = (c && c.data && c.data.rows) || [];
            if (!c || !c.data.searched) { out.innerHTML = '<div style="opacity:.55;font-size:13px;padding:6px">Введите артикул</div>'; return; }
            if (!rows.length) { out.innerHTML = '<div style="opacity:.55;font-size:13px;padding:6px">Ничего не найдено</div>'; return; }
            out.innerHTML = rows.map(rr => {
              const oem = (rr.params || {}).oem || '';
              return '<div class="__df-oemres" data-oem="' + esc(oem) + '" style="padding:7px 10px;border-radius:7px;border:1px solid rgba(0,0,0,.08);background:#fafafa;cursor:pointer;font-size:13px">' + esc(rr.title || '') + '</div>';
            }).join('');
          }).catch(() => {});
      }, 350);
    });
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
      window.__role = cfg.role || 'buyer';
      // Аноним: «Войти» вместо фейкового «Гость / buyer».
      const anon = !!cfg.anonymous;
      const name = anon ? 'Войти' : (cfg.user_name || 'User');
      const initial = anon ? '→' : (name[0] || '?').toUpperCase();
      $('sideUserName').textContent = name;
      $('sideUserRole').textContent = anon ? '' : (cfg.role || '').replace('operator_', '').replace(/_/g, ' ');
      $('sideAvatar').textContent = initial;
      const _topAv = $('topAvatar'); if (_topAv) _topAv.textContent = anon ? '?' : initial;
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
    // Роль: покупатель → закупочный проект; продавец → ТОВАРНОЕ НАПРАВЛЕНИЕ;
    // оператор → СДЕЛКА / консолидированная поставка (видит обе стороны).
    const role = (p.role || 'buyer');
    const isSeller = role === 'seller';
    const isOperator = role.indexOf('operator') === 0;
    // Подроль оператора: manager(КАМ) / logist / customs / payment. Общий operator → manager.
    const opSub = isOperator ? (role.replace('operator_', '').replace('operator', 'manager') || 'manager') : '';
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
           sec_orders: 'Заказы в работе',
           sec_rfqs_op: 'Позиции и подбор', sec_rfqs_customs: 'Позиции на таможне',
           sec_orders_op: 'Отгрузки и этапы', sec_rfqs_seller: 'Входящие RFQ по направлению',
           op_pos:'Позиции в работе', op_log:'Логистика и таможня',
           op_pay:'Платежи · эскроу', op_deal:'Оборот сделки',
           op_at_customs:'На таможне', op_nearest_eta:'Ближайший ETA',
           op_delays:'Задержки', op_hs:'Ждут HS-кода',
           op_declarations:'Декларации', op_sanctions:'Проверка санкций',
           op_escrow:'В эскроу', op_awaiting_payout:'Ждут выплаты',
           op_paid_buyer:'Оплачено покупателем', op_margin_deal:'Маржа сделки',
           sub_shipments:'отгрузок в дороге', sub_cleared:'всё прошло',
           sub_next_del:'следующая доставка', sub_on_sched:'в графике',
           sub_clearing:'позиций оформляется', sub_all_codes:'все коды есть',
           sub_in_work:'в работе', sub_no_risk:'рисков нет',
           sub_held:'удержано до поставки', sub_paid_out:'выплачено',
           sub_for_deal:'по сделке', sub_fee:'комиссия платформы',
           sub_await_cus:'ждут растаможки', sub_await_act:'ждут вашего действия',
           sub_await_pay:'ждут выплаты', sub_margin:'маржа',
           sub_risk_sla:'риск SLA', sub_set_code:'проставьте код',
           sub_check:'требует проверки', sub_sellers:'продавцам'},
      en: {open_rfqs: 'Open RFQs', active_orders: 'Active Orders',
           in_transit: 'In Transit', spend_mtd: 'Spend MTD',
           awaiting_op: 'awaiting operator',
           value: 'value', earliest_eta: 'earliest ETA',
           vs_prev_month: 'vs previous month',
           sec_rfqs: 'Matches — awaiting your decision',
           sec_orders: 'Orders in progress',
           sec_rfqs_op: 'Positions and sourcing', sec_rfqs_customs: 'Positions in customs',
           sec_orders_op: 'Shipments and stages', sec_rfqs_seller: 'Incoming RFQs by category',
           op_pos:'Positions in progress', op_log:'Logistics and customs',
           op_pay:'Payments · escrow', op_deal:'Deal volume',
           op_at_customs:'In customs', op_nearest_eta:'Nearest ETA',
           op_delays:'Delays', op_hs:'Awaiting HS code',
           op_declarations:'Declarations', op_sanctions:'Sanctions check',
           op_escrow:'In escrow', op_awaiting_payout:'Awaiting payout',
           op_paid_buyer:'Paid by buyer', op_margin_deal:'Deal margin',
           sub_shipments:'shipments on the way', sub_cleared:'all cleared',
           sub_next_del:'next delivery', sub_on_sched:'on schedule',
           sub_clearing:'positions clearing', sub_all_codes:'all codes set',
           sub_in_work:'in progress', sub_no_risk:'no risks',
           sub_held:'held until delivery', sub_paid_out:'paid out',
           sub_for_deal:'for the deal', sub_fee:'platform commission',
           sub_await_cus:'awaiting customs', sub_await_act:'awaiting action',
           sub_await_pay:'awaiting payout', sub_margin:'margin',
           sub_risk_sla:'SLA risk', sub_set_code:'set the code',
           sub_check:'needs review', sub_sellers:'sellers'},
      es: {open_rfqs: 'RFQ abiertos', active_orders: 'Pedidos activos',
           in_transit: 'En tránsito', spend_mtd: 'Gasto del mes',
           awaiting_op: 'esperan operador',
           value: 'valor', earliest_eta: 'ETA más cercano',
           vs_prev_month: 'vs mes anterior',
           sec_rfqs: 'Coincidencias — esperan su decisión',
           sec_orders: 'Pedidos en curso',
           sec_rfqs_op: 'Posiciones y selección', sec_rfqs_customs: 'Posiciones en aduana',
           sec_orders_op: 'Envíos y etapas', sec_rfqs_seller: 'RFQ entrantes por categoría',
           op_pos:'Posiciones en curso', op_log:'Logística y aduana',
           op_pay:'Pagos · depósito', op_deal:'Volumen del trato',
           op_at_customs:'En aduana', op_nearest_eta:'ETA más próximo',
           op_delays:'Retrasos', op_hs:'Esperando código HS',
           op_declarations:'Declaraciones', op_sanctions:'Verificación de sanciones',
           op_escrow:'En depósito', op_awaiting_payout:'Esperando pago',
           op_paid_buyer:'Pagado por comprador', op_margin_deal:'Margen del trato',
           sub_shipments:'envíos en camino', sub_cleared:'todo despachado',
           sub_next_del:'siguiente entrega', sub_on_sched:'en calendario',
           sub_clearing:'posiciones en trámite', sub_all_codes:'todos los códigos OK',
           sub_in_work:'en curso', sub_no_risk:'sin riesgos',
           sub_held:'retenido hasta entrega', sub_paid_out:'pagado',
           sub_for_deal:'del trato', sub_fee:'comisión plataforma',
           sub_await_cus:'en espera de aduana', sub_await_act:'esperan acción',
           sub_await_pay:'esperan pago', sub_margin:'margen',
           sub_risk_sla:'riesgo SLA', sub_set_code:'asigne el código',
           sub_check:'requiere revisión', sub_sellers:'proveedores'},
      zh: {open_rfqs: '待处理 RFQ', active_orders: '活动订单',
           in_transit: '运输中', spend_mtd: '本月支出',
           awaiting_op: '等待操作员',
           value: '总额', earliest_eta: '最早预计',
           vs_prev_month: '环比上月',
           sec_rfqs: '匹配 — 等待您的决定',
           sec_orders: '进行中的订单',
           sec_rfqs_op: '头寸与采购', sec_rfqs_customs: '清关中的头寸',
           sec_orders_op: '发货与阶段', sec_rfqs_seller: '按类别的入库询价',
           op_pos:'在途头寸', op_log:'物流与海关',
           op_pay:'付款 · 托管', op_deal:'交易额',
           op_at_customs:'清关中', op_nearest_eta:'最近预计到达',
           op_delays:'延误', op_hs:'等待HS编码',
           op_declarations:'申报单', op_sanctions:'制裁检查',
           op_escrow:'托管中', op_awaiting_payout:'等待付款',
           op_paid_buyer:'买方已付', op_margin_deal:'交易利润',
           sub_shipments:'发货在途', sub_cleared:'全部通关',
           sub_next_del:'下次交货', sub_on_sched:'按时',
           sub_clearing:'头寸清关中', sub_all_codes:'所有编码齐全',
           sub_in_work:'进行中', sub_no_risk:'无风险',
           sub_held:'待交货释放', sub_paid_out:'已支付',
           sub_for_deal:'本次交易', sub_fee:'平台佣金',
           sub_await_cus:'等待清关', sub_await_act:'等待处理',
           sub_await_pay:'等待付款', sub_margin:'利润',
           sub_risk_sla:'SLA风险', sub_set_code:'设置编码',
           sub_check:'需要审查', sub_sellers:'供应商',
           sel_inc_rfqs:'入库询价', sub_await_kp:'等待您的报价',
           sel_orders_work:'在途订单', sub_to_ship:'待发货',
           sel_catalog:'目录中产品', sub_with_draw:'含图纸/照片',
           sub_from_total:'其中', sel_revenue:'月收入'},
    };
    LBL.ru.sel_inc_rfqs='Входящие RFQ'; LBL.ru.sub_await_kp='ждут вашего КП';
    LBL.ru.sel_orders_work='Заказы в работе'; LBL.ru.sub_to_ship='к отгрузке';
    LBL.ru.sel_catalog='Товаров в каталоге'; LBL.ru.sub_with_draw='с чертежом/фото';
    LBL.ru.sub_from_total='из них'; LBL.ru.sel_revenue='Выручка за месяц';
    LBL.en.sel_inc_rfqs='Incoming RFQs'; LBL.en.sub_await_kp='awaiting your quote';
    LBL.en.sel_orders_work='Orders in progress'; LBL.en.sub_to_ship='to ship';
    LBL.en.sel_catalog='Products in catalog'; LBL.en.sub_with_draw='with drawing/photo';
    LBL.en.sub_from_total='of which'; LBL.en.sel_revenue='Revenue MTD';
    LBL.es.sel_inc_rfqs='RFQ entrantes'; LBL.es.sub_await_kp='esperan su oferta';
    LBL.es.sel_orders_work='Pedidos en curso'; LBL.es.sub_to_ship='a enviar';
    LBL.es.sel_catalog='Productos en catálogo'; LBL.es.sub_with_draw='con plano/foto';
    LBL.es.sub_from_total='de los cuales'; LBL.es.sel_revenue='Ingreso del mes';
    const L = LBL[lang] || LBL.ru;
    if (isSeller) {
      L.sec_rfqs = L.sec_rfqs_seller;
      L.sec_orders = L.sec_orders;
    } else if (isOperator) {
      L.sec_rfqs = (opSub === 'customs') ? L.sec_rfqs_customs : L.sec_rfqs_op;
      L.sec_orders = L.sec_orders_op;
    }
    // KPI cards. Подписи под цифрами объясняют ЧТО за число.
    const stats = p.stats || {};
    const kpiClick = (id) => `onclick="window.__projScrollTo&amp;&amp;window.__projScrollTo('${id}')" style="cursor:pointer"`;
    let kpiHTML;
    if (isSeller) {
      // Продавец: Входящие RFQ / Заказы в работе / Товаров в каталоге / Выручка за месяц.
      const awaiting = stats.incoming_rfqs?.awaiting || 0;
      const toShip = stats.active_orders?.to_ship || 0;
      const withDr = stats.catalog_items?.with_drawing || 0;
      const rev = stats.revenue_mtd || {};
      const rd = rev.delta_pct ?? 0;
      kpiHTML = `
        <div class="kpi" ${kpiClick('sec-rfqs')} title="К разделу входящих RFQ">
          <div class="kpi-label">${L.sel_inc_rfqs}</div>
          <div class="kpi-value"><div class="kpi-num">${stats.incoming_rfqs?.count || 0}</div></div>
          ${awaiting ? `<div class="kpi-sub"><span class="kpi-warn">${awaiting}</span> ${L.sub_await_kp}</div>` : ''}
        </div>
        <div class="kpi" ${kpiClick('sec-orders')} title="К разделу заказов">
          <div class="kpi-label">${L.sel_orders_work}</div>
          <div class="kpi-value"><div class="kpi-num">${stats.active_orders?.count || 0}</div></div>
          <div class="kpi-sub">${fmtMoney(stats.active_orders?.value_usd)}${toShip ? ` · <span class="kpi-warn">${toShip}</span> ${L.sub_to_ship}` : ''}</div>
        </div>
        <div class="kpi" title="Товары направления в каталоге">
          <div class="kpi-label">${L.sel_catalog}</div>
          <div class="kpi-value"><div class="kpi-num">${stats.catalog_items?.count || 0}</div></div>
          <div class="kpi-sub">${L.sub_from_total} <span class="kpi-good">${withDr}</span> ${L.sub_with_draw}</div>
        </div>
        <div class="kpi" ${kpiClick('sec-orders')} title="Выручка по направлению">
          <div class="kpi-label">${L.sel_revenue}</div>
          <div class="kpi-value"><div class="kpi-num">${fmtMoney(rev.value_usd)}</div></div>
          <div class="kpi-sub"><span class="${rd >= 0 ? 'kpi-good' : 'kpi-warn'}">${rd >= 0 ? '+' : ''}${rd}%</span> ${L.vs_prev_month}</div>
        </div>
      `;
    } else if (isOperator) {
      // Оператор: 4 KPI под подроль (manager/logist/customs/payment).
      const pos = stats.positions || {};
      const log = stats.logistics || {};
      const cus = stats.customs || {};
      const pay = stats.payments || {};
      const deal = stats.deal_turnover || {};
      const md = deal.margin_pct ?? 0;
      const warn = (n) => `<span class="kpi-warn">${n}</span>`;
      const good = (n) => `<span class="kpi-good">${n}</span>`;
      const card = (id, label, num, sub) =>
        `<div class="kpi" ${id ? kpiClick(id) : ''}>
          <div class="kpi-label">${label}</div>
          <div class="kpi-value"><div class="kpi-num">${num}</div></div>
          ${sub ? `<div class="kpi-sub">${sub}</div>` : ''}
        </div>`;
      if (opSub === 'logist') {
        kpiHTML =
          card('sec-orders', L.in_transit, log.in_transit || 0, L.sub_shipments) +
          card('sec-orders', L.op_at_customs, log.at_customs || 0, log.at_customs ? L.sub_await_cus : L.sub_cleared) +
          card('sec-orders', L.op_nearest_eta, esc(log.earliest_eta || '—'), L.sub_next_del) +
          card('sec-orders', L.op_delays, log.delays || 0, log.delays ? warn(log.delays) + ' ' + L.sub_risk_sla : L.sub_on_sched);
      } else if (opSub === 'customs') {
        kpiHTML =
          card('sec-rfqs', L.op_at_customs, cus.at_customs || 0, L.sub_clearing) +
          card('sec-rfqs', L.op_hs, cus.hs_pending || 0, cus.hs_pending ? warn(cus.hs_pending) + ' ' + L.sub_set_code : L.sub_all_codes) +
          card('sec-rfqs', L.op_declarations, cus.declarations || 0, L.sub_in_work) +
          card('', L.op_sanctions, cus.sanctions_risk || 0, cus.sanctions_risk ? warn(cus.sanctions_risk) + ' ' + L.sub_check : good('✓') + ' ' + L.sub_no_risk);
      } else if (opSub === 'payment') {
        kpiHTML =
          card('sec-orders', L.op_escrow, fmtMoney(pay.escrow_usd), L.sub_held) +
          card('sec-orders', L.op_awaiting_payout, pay.awaiting_payout || 0, pay.awaiting_payout ? warn(pay.awaiting_payout) + ' ' + L.sub_sellers : L.sub_paid_out) +
          card('', L.op_paid_buyer, fmtMoney(pay.paid_by_buyer_usd), L.sub_for_deal) +
          card('', L.op_margin_deal, (md >= 0 ? '+' : '') + md + '%', L.sub_fee);
      } else {
        // manager / КАМ / общий оператор — полная картина
        kpiHTML =
          card('sec-rfqs', L.op_pos, pos.count || 0, pos.awaiting ? warn(pos.awaiting) + ' ' + L.sub_await_act : '') +
          card('sec-orders', L.op_log, log.count || 0, (log.at_customs ? warn(log.at_customs) + ' ' + L.op_at_customs.toLowerCase() + ' · ' : '') + 'ETA ' + esc(log.earliest_eta || '—')) +
          card('sec-orders', L.op_pay, fmtMoney(pay.escrow_usd), pay.awaiting_payout ? warn(pay.awaiting_payout) + ' ' + L.sub_await_pay : '') +
          card('sec-orders', L.op_deal, fmtMoney(deal.value_usd), L.sub_margin + ' ' + (md >= 0 ? good('+' + md + '%') : warn(md + '%')));
      }
    } else {
      const semiCount = stats.open_rfqs?.semi || 0;  // RFQ ждущие подбора оператора
      kpiHTML = `
      <div class="kpi" ${kpiClick('sec-rfqs')} title="К разделу RFQ">
        <div class="kpi-label">${L.open_rfqs}</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.open_rfqs?.count || 0}</div>
        </div>
        ${semiCount ? `<div class="kpi-sub"><span class="kpi-warn">${semiCount}</span> ${L.awaiting_op}</div>` : ''}
      </div>
      <div class="kpi" ${kpiClick('sec-orders')} title="К разделу заказов">
        <div class="kpi-label">${L.active_orders}</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.active_orders?.count || 0}</div>
        </div>
        <div class="kpi-sub">${fmtMoney(stats.active_orders?.value_usd)} ${L.value}</div>
      </div>
      <div class="kpi" ${kpiClick('sec-orders')} title="К разделу заказов">
        <div class="kpi-label">${L.in_transit}</div>
        <div class="kpi-value">
          <div class="kpi-num">${stats.in_transit?.count || 0}</div>
        </div>
        <div class="kpi-sub">${L.earliest_eta}: ${esc(stats.in_transit?.earliest_eta || '—')}</div>
      </div>
      <div class="kpi" ${kpiClick('sec-orders')} title="К разделу заказов">
        <div class="kpi-label">${L.spend_mtd}</div>
        <div class="kpi-value">
          <div class="kpi-num">${fmtMoney(stats.spend_mtd?.value_usd)}</div>
        </div>
        <div class="kpi-sub"><span class="${stats.spend_mtd?.delta_pct >= 0 ? 'kpi-good' : 'kpi-warn'}">${stats.spend_mtd?.delta_pct >= 0 ? '+' : ''}${stats.spend_mtd?.delta_pct ?? 0}%</span> ${L.vs_prev_month}</div>
      </div>
    `;
    }

    // Documents — empty-state карточка (0 docs) или категоризованные слоты (есть docs)
    const docs = p.documents || [];
    const DOC_SLOTS = isOperator ? (function(){
      const OS = {
        contract:  {key: "contract",  icon: "📑", label: "Контракты и условия",
          descShort: "Договоры покупатель/продавцы и Incoterms — основа сделки",
          descFull: "PDF: договоры, спецификации, Incoterm"},
        customs:   {key: "customs",   icon: "🛂", label: "Таможенные документы",
          descShort: "Декларации, HS-коды, инвойсы, сертификаты — таможня проходит без задержек",
          descFull: "Декларации, HS, инвойсы, сертификаты происхождения"},
        logistics: {key: "logistics", icon: "🚚", label: "Логистика",
          descShort: "BL/CMR, упаковочные листы, маршрут — отгрузки под контролем",
          descFull: "BL/CMR, packing list, маршрут, трекинг"},
        payment:   {key: "payment",   icon: "💳", label: "Платежи",
          descShort: "Инвойсы, эскроу, акты, выплаты — финансовая часть сделки",
          descFull: "Инвойсы, эскроу, акты, payout продавцам"},
      };
      // Фокус-документ подроли — первым.
      const order = opSub === 'logist'  ? ['logistics', 'customs', 'contract', 'payment']
                  : opSub === 'customs' ? ['customs', 'logistics', 'contract', 'payment']
                  : opSub === 'payment' ? ['payment', 'contract', 'customs', 'logistics']
                  : ['contract', 'customs', 'logistics', 'payment'];
      return order.map(function(k){ return OS[k]; });
    })() : isSeller ? [
      {key: "pricelist",   icon: "📤", label: "Прайс-лист направления",
       descShort: "Цены по этой группе — оператор и AI быстрее формируют КП по входящим RFQ",
       descFull: "Excel/CSV: артикул, цена, наличие, срок"},
      {key: "drawing",     icon: "📐", label: "Чертежи и спецификации",
       descShort: "Покупатель сразу видит, что вы предлагаете — котировка проходит за часы",
       descFull: "DWG/PDF сборок, ведомости узлов"},
      {key: "certificate", icon: "🛡", label: "Сертификаты и паспорта",
       descShort: "Качество и происхождение — проходит проверку оператора без задержек",
       descFull: "PDF: сертификаты качества, паспорта, происхождение"},
      {key: "photo",       icon: "🖼", label: "Фото и описания товаров",
       descShort: "Карточки товаров с фото — больше доверия и выше конверсия в заказ",
       descFull: "JPG/PNG + описания, габариты, аналоги"},
    ] : [
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
      descShort: isOperator
        ? "Прочие документы по сделке — переписка, доп.соглашения, фото — AI учитывает в ответах"
        : isSeller
        ? "Гарантии, условия поставки, прайс-история — AI учитывает их в ответах по направлению"
        : "Контракты и условия поставки — AI учитывает их при формировании заказа",
      descFull: isOperator ? "Доп.соглашения, переписка, фото" : (isSeller ? "Гарантии, Incoterm, прайс-история" : "Контракты, условия Incoterm")}];
    const docsByType = {};
    docs.forEach(d => {
      const k = d.doctype || "other";
      if (!docsByType[k]) docsByType[k] = [];
      docsByType[k].push(d);
    });
    // Глобально — чтобы модалка-папка (window.__openDocFolder) имела доступ.
    window.__projDocs = docs;
    window.__projSlots = SLOTS_WITH_OTHER;
    window.__projInfo = {name: p.name || '', code: p.code || '', customer: p.customer || '', description: p.description || ''};
    const connectedTypes = SLOTS_WITH_OTHER.filter(s => (docsByType[s.key] || []).length > 0).length;
    const progressPct = Math.round(connectedTypes / SLOTS_WITH_OTHER.length * 100);
    const renderSlot = (slot) => {
      const slotDocs = docsByType[slot.key] || [];
      const filled = slotDocs.length > 0;
      const addLabel = filled ? '+ Добавить' : '+ Загрузить';
      const cornerBadge = filled
        ? `<span class="doc-slot-corner-badge" title="${slotDocs.length} документ(ов)">${slotDocs.length}</span>`
        : '';
      return `<div class="doc-slot${filled ? ' doc-slot-filled' : ''}"${filled ? ` onclick="window.__openDocFolder&amp;&amp;window.__openDocFolder('${slot.key}')" style="cursor:pointer"` : ''}>
        ${cornerBadge}
        <div class="doc-slot-head">
          <span class="doc-slot-icon">${slot.icon}</span>
          <span class="doc-slot-label">${esc(slot.label)}</span>
          <a href="#" class="doc-slot-add" onclick="event.stopPropagation();window.__uploadProjectDoc&amp;&amp;window.__uploadProjectDoc('${slot.key}');return false;">${addLabel}</a>
        </div>
        <div class="doc-slot-desc-short">${esc(slot.descShort)}</div>
        <div class="doc-slot-desc">↓ <span>${esc(slot.descFull)}</span></div>
        ${filled ? `<div style="margin-top:8px;font-size:12.5px;font-weight:600;opacity:.7">📂 Открыть папку (${slotDocs.length}) ›</div>` : ''}
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
      return `<div class="rfq" onclick="window.location.href='/chat/?action=get_rfq_status'" style="cursor:pointer" title="Открыть RFQ в чате">
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
      return `<div class="po" onclick="window.location.href='/chat/?action=get_orders'" style="cursor:pointer" title="Открыть заказы в чате">
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

    // Участники сделки — только оператор (видит обе стороны). Используем стиль строки .rfq.
    const participants = p.participants || [];
    const participantsHTML = participants.map(pt => `
      <div class="rfq" style="cursor:default;">
        <span class="rfq-num">${esc(pt.role)}</span>
        <div class="rfq-info">
          <div class="rfq-title">${esc(pt.name)}</div>
          <div class="rfq-meta">${esc(pt.meta || '')}</div>
        </div>
      </div>`).join('');

    // Секции списков — для логиста «Отгрузки» выше «Позиций».
    const rfqsSection = `
      <div class="sec-title" id="sec-rfqs">
        <h2>${L.sec_rfqs}</h2>
        <span class="sec-title-count">${rfqs.length}</span>
      </div>
      <div class="rfq-list">${rfqsHTML}</div>`;
    const ordersSection = `
      <div class="sec-title" id="sec-orders">
        <h2>${L.sec_orders}</h2>
        <span class="sec-title-count">${orders.length}</span>
      </div>
      <div class="po-list">${ordersHTML}</div>`;
    const listsSection = (isOperator && opSub === 'logist')
      ? (ordersSection + rfqsSection)
      : (rfqsSection + ordersSection);

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
          <button class="pj-btn" onclick="window.__openDocFolder&amp;&amp;window.__openDocFolder('__all__')">Файлы</button>
          <button class="pj-btn" onclick="window.__openProjectSettings&amp;&amp;window.__openProjectSettings()">Настройки</button>
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

      ${listsSection}

      ${isOperator && participants.length ? `
      <div class="sec-title">
        <h2>Участники сделки</h2>
        <span class="sec-title-count">${participants.length}</span>
      </div>
      <div class="rfq-list">${participantsHTML}</div>
      ` : ''}

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
