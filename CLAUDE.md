# Consolidator — Контекст проекта для Claude

## Стек
- Django (Python), SQLite
- Шаблоны: `templates/` — HTML с inline JS, без фреймворков
- Сервер: `python manage.py runserver` (порт 8001 через launch.json)
- Django кэширует шаблоны — **после правки HTML нужно перезапускать сервер**

## Структура кабинетов
```
/operator/  → роли: manager, logist, customs, payments (base: base_operator.html)
/seller/    → продавец
/buyer/     → покупатель
/admin_panel/ → администратор
```

## Демо-аккаунты
- `demo_operator` / `demo12345` — оператор
- `demo_seller` / `demo12345` — продавец
- `demo_buyer` / `demo12345` — покупатель

## Паттерны UI (операторские страницы)

### Кликабельные стат-блоки
```html
<!-- Навигация на другую страницу -->
<a class="op-stat" href="/operator/payments/invoices/">...</a>

<!-- JS-фильтр таблицы (toggle) -->
<div class="op-stat" id="stat-id" onclick="filterFunc('value')" style="cursor:pointer;">...</div>
```

CSS hover для `a.op-stat` уже в `base_operator.html`.
Для `div.op-stat` используй inline onclick.

### Подсветка активного стат-блока
```js
var active = 'rgba(100,181,246,0.35)';   // синий
var warn   = 'rgba(232,92,13,0.5)';      // оранжевый (для ошибок/расхождений)
var inactive = 'rgba(255,255,255,0.04)';
el.style.borderColor = active;
```

### JS-фильтр таблицы (стандартный паттерн)
```js
var activeStatus = '';
window.filterByStatus = function(status) {
  activeStatus = (activeStatus === status) ? '' : status; // toggle
  // сбросить все подсветки
  document.querySelectorAll('[id^="stat-"]').forEach(function(el){ el.style.borderColor = inactive; });
  // подсветить активный
  if (activeStatus) document.getElementById('stat-' + activeStatus).style.borderColor = active;
  // фильтровать строки
  document.querySelectorAll('tbody tr[data-status]').forEach(function(row) {
    row.style.display = (!activeStatus || row.getAttribute('data-status') === activeStatus) ? '' : 'none';
  });
};
```

### Тосты
```js
// Оператор
opToast('Сообщение', 2000, 'success'); // type: 'success'=зелёный, 'warn'=оранжевый, default=синий

// Продавец/покупатель
showToast('Сообщение'); // своя реализация в каждом кабинете
```

**Важно:** НЕ добавляй "— функция в разработке" к сообщениям. Это было багом, уже исправлено в base_operator.html.

## Что уже сделано

### Кликабельные стат-блоки ✅
- `operator/payments/dashboard` — фильтр очереди по статусу
- `operator/payments/analytics` — карточки типов платежей → /invoices/?type=...
- `operator/payments/escrow` — фильтр по статусу (Удержание/Спор)
- `operator/payments/reconciliation` — стат-блоки → переключают вкладки
- `operator/customs/documents` — фильтр по типу документа + комбинируется с вкладками
- `operator/logist/analytics` — карточки способов доставки → /logist/documents/
- `operator/manager/analytics` — карточки статусов заказов → /manager/orders/
- `operator/customs/analytics`, `customs/dashboard` — осмысленная навигация
- `operator/logist/dashboard` — осмысленная навигация
- `operator/manager/dashboard` — осмысленная навигация
- `seller/rating` — rat-metric блоки кликабельны
- `seller/integrations` — тост исправлен
- `buyer/negotiations` — neg-stat блоки кликабельны

### Баги исправлены ✅
- `base_operator.html`: opToast больше не добавляет "функция в разработке"
- `base_operator.html`: CSS hover для `a.op-stat`

## Что остаётся сделать

### Оператор — самоссылающиеся стат-блоки
- `operator/manager/negotiations.html` — все 5 блоков → self-links
- `operator/logist/ports.html` — 2 блока → self-links
- `operator/customs/analytics.html` — 5 блоков (внутри таблицы — сложнее)

### Покупатель — нефункциональные ссылки
- `buyer/orders/list.html` — детали заказов → `href="javascript:void(0)"`
- `buyer/suppliers/list.html` — сравнить / история → `href="javascript:void(0)"`
- `buyer/analytics/list.html` — отчёты → `href="javascript:void(0)"`
- `buyer/claims/list.html` — открыть рекламацию, действия → `href="javascript:void(0)"`

### Продавец
- `seller/team/list.html` — кнопка "Новая задача" → `showToast` без действия
- `seller/logistics/list.html` — "Ручной расчёт менеджером" → `showToast` без действия

## Git workflow
- Основная ветка: `main`
- Десктоп сессия: `claude/focused-kilby`
- Dispatch (телефон): `claude/confident-wozniak`
- После завершения задачи — коммитить и мержить в `main`
- Копировать изменения в основной репо: `cp worktrees/X/templates/... templates/...`
