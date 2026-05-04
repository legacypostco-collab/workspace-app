/* КП (Commercial Proposal) page — chat-first style.
 * Reuses the /api/assistant/rfq/<id>/ endpoint and renders an offer-style view.
 */
(function(){
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => (s == null ? '' : String(s)).replace(/[&<>"']/g, m =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const fmtMoney = (v, c='USD') => {
    if (v == null || v === '') return '—';
    const sym = {USD:'$', EUR:'€', RUB:'₽', CNY:'¥'}[c] || '';
    return sym + Number(v).toLocaleString('en-US', {maximumFractionDigits: 2, minimumFractionDigits: 0});
  };
  const fmtDate = s => {
    if (!s) return '';
    try { return new Date(s).toLocaleDateString('ru-RU', {day:'2-digit',month:'long',year:'numeric'}); }
    catch(e) { return s; }
  };

  function badge(state) {
    const cls = (state === 'matched' || state === 'quoted') ? 'matched'
              : (state === 'no_match') ? 'no_match' : 'pending';
    const label = (state === 'matched' || state === 'quoted') ? 'Доступно'
                : (state === 'no_match') ? 'Нет совпадений' : 'В работе';
    return `<span class="badge ${cls}">${label}</span>`;
  }

  function render(d) {
    const items = d.items || [];
    const priced = items.filter(it => it.price);
    const lineTotal = it => (Number(it.price) || 0) * (Number(it.qty) || 1);
    const total = priced.reduce((s, it) => s + lineTotal(it), 0);
    const currency = (priced[0] || {}).currency || 'USD';
    const proposalNo = `КП-${String(d.id).padStart(5, '0')}`;
    const validUntil = (() => {
      const dd = new Date(); dd.setDate(dd.getDate() + 14);
      return dd.toLocaleDateString('ru-RU', {day:'2-digit', month:'long', year:'numeric'});
    })();

    const html = `
      <div class="pp-head">
        <div>
          <div class="pp-eyebrow">${esc(proposalNo)}</div>
          <h1 class="pp-name">Коммерческое предложение</h1>
          <div class="pp-meta" style="margin-top:8px;">
            <span>RFQ <strong>#${esc(d.id)}</strong></span>
            ${d.customer_name ? `<span>Клиент: <strong>${esc(d.customer_name)}</strong></span>` : ''}
            ${d.created_at ? `<span>Дата: <strong>${esc(fmtDate(d.created_at))}</strong></span>` : ''}
            <span>Действительно до: <strong>${esc(validUntil)}</strong></span>
          </div>
        </div>
        <div class="pp-actions">
          <button class="pp-btn" onclick="window.print()">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
            Печать
          </button>
          <a class="pp-btn" href="/rfq/${esc(d.id)}/proposal/pdf/" target="_blank">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Скачать PDF
          </a>
          <a class="pp-btn primary" href="/chat/rfq/${esc(d.id)}/">
            Вернуться к RFQ
          </a>
        </div>
      </div>

      <div class="summary">
        <div class="sum">
          <div class="sum-lbl">Позиций</div>
          <div class="sum-val">${items.length}</div>
        </div>
        <div class="sum">
          <div class="sum-lbl">С ценами</div>
          <div class="sum-val ${priced.length === items.length ? '' : 'muted'}">${priced.length} / ${items.length}</div>
        </div>
        <div class="sum">
          <div class="sum-lbl">Поставщиков</div>
          <div class="sum-val">${new Set(items.map(it => it.supplier).filter(Boolean)).size}</div>
        </div>
        <div class="sum">
          <div class="sum-lbl">Итого</div>
          <div class="sum-val">${fmtMoney(total, currency)}</div>
        </div>
      </div>

      <div class="sec">
        <div class="sec-h">
          <h2>Состав КП</h2>
          <span class="sec-pill">${items.length} позиций</span>
        </div>
        ${items.length === 0 ? '<div class="loading" style="padding:30px;">В RFQ нет позиций для КП</div>' : `
        <table class="lines">
          <thead>
            <tr>
              <th style="width:40px;">№</th>
              <th>Артикул / Деталь</th>
              <th>Поставщик</th>
              <th class="right">Цена</th>
              <th class="right" style="width:60px;">Кол-во</th>
              <th class="right" style="width:140px;">Сумма</th>
            </tr>
          </thead>
          <tbody>
            ${items.map((it, i) => `
              <tr>
                <td>${i + 1}</td>
                <td>
                  <div class="art">${esc(it.article || '—')}</div>
                  ${it.match ? `<div class="match-name">${esc(it.match)}${it.brand ? ' · ' + esc(it.brand) : ''}</div>` : ''}
                  <div style="margin-top:4px;">${badge(it.state)}</div>
                </td>
                <td>${esc(it.supplier || '—')}</td>
                <td class="right">${fmtMoney(it.price, it.currency)}</td>
                <td class="right">${esc(it.qty || 1)}</td>
                <td class="right"><strong>${fmtMoney(lineTotal(it), it.currency)}</strong></td>
              </tr>`).join('')}
          </tbody>
        </table>
        `}
        <div class="total-row">
          <span class="total-lbl">Итого к оплате (без НДС)</span>
          <span class="total-val">${fmtMoney(total, currency)}</span>
        </div>
      </div>

      <div class="sec">
        <div class="sec-h"><h2>Условия</h2></div>
        <div class="terms">
          <div class="term">
            <div class="term-lbl">Срок поставки</div>
            <div class="term-val">14–21 день после оплаты</div>
          </div>
          <div class="term">
            <div class="term-lbl">Оплата</div>
            <div class="term-val">${esc(d.mode === 'auto' ? '100% предоплата' : '10% резерв + 90% перед отгрузкой')}</div>
          </div>
          <div class="term">
            <div class="term-lbl">Гарантия</div>
            <div class="term-val">12 месяцев на OEM, 6 месяцев на аналоги</div>
          </div>
          <div class="term">
            <div class="term-lbl">Условия поставки</div>
            <div class="term-val">DAP / FCA по согласованию</div>
          </div>
        </div>
      </div>
    `;
    $('ppContent').innerHTML = html;
  }

  async function load() {
    try {
      const r = await fetch('/api/assistant/rfq/' + window.RFQ_ID + '/');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      render(await r.json());
    } catch(e) {
      $('ppContent').innerHTML = `<div class="loading">⚠️ Не удалось загрузить КП: ${esc(e.message)}</div>`;
    }
  }
  load();
})();
