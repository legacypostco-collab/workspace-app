# Consolidator Parts — Техническое Задание v4.0

**Дата:** Апрель 2026
**Owner:** Mansour | 1@legacypostco.tech
**Статус:** Действующее (после рефакторинга в chat-first парадигму)

> Этот документ заменяет TZ_CLAUDE_CODE.docx v3.0. Сохраняет vision, но обновляет архитектуру (гибридный AI), стек (Django вместо Next.js), приоритеты (упаковка end-to-end в чат вместо переписывания) и реальный roadmap.

---

## 1. Vision

**Consolidator Parts — это end-to-end B2B procurement platform для запчастей тяжёлой техники, где AI-чат заменяет 10-шаговый процесс закупки одной фразой.**

Аналогия — не «Cursor для запчастей», а **Flexport / Stripe для B2B procurement**. Конкуренты дают только прайс по списку. Мы даём **закрытый цикл**: поиск → RFQ → контракт → эскроу → производство → логистика → таможня → документы → приёмка → закрытие. Всё через один чат, оплата 5% с оборота.

### 1.1 Кто пользуется

| Роль | Кто | Главная боль |
|------|-----|--------------|
| **Buyer** | Снабженцы шахт/строек/торговых компаний с 30 одновременными RFQ | Купить надёжно, быстро, просто, по выгодной цене |
| **Seller** | OEM/distributors/китайские фабрики | Получать качественные RFQ, быстро отвечать, гарантия оплаты |
| **Operator** | Команда платформы (logist, customs, payments, manager) | Закрыть max сделок min ручного труда |
| **Admin** | Платформа | Контроль, метрики, биллинг |

### 1.2 Ключевые принципы продукта

1. **Chat-first для buyer и seller.** Никаких форм, таблиц, дашбордов в стиле админки.
2. **Operator остаётся в power-UI** (классический кабинет с фильтрами/таблицами) — он обрабатывает 50+ объектов в день.
3. **AI никогда не галлюцинирует.** Каждое значение приходит из БД через tool call, а не из памяти модели.
4. **Карточка = продукт.** Каждый ответ AI — интерактивный объект (RFQ-карточка с 5 кнопками), а не текст.
5. **End-to-end удержание.** Все 10 шагов процесса должны быть удобнее в платформе, чем off-platform — иначе bypass-риск убивает 5% модель.

---

## 2. Бизнес-модель

### 2.1 Монетизация

**5% от GMV, поделено между buyer и seller** (подробное распределение зависит от категории сделки).

**Юнит-экономика на одного активного buyer:**
- Средний чек: $50K
- Одновременных сделок: 30
- Закрытие за квартал: ~60% (18 сделок × $50K = $900K оборота)
- Платформа: 5% × $900K = **$45K/квартал = $180K/год** с одного buyer'а

Это marketplace fee класса Booking.com / Uber, не SaaS подписка.

### 2.2 Главный бизнес-риск: bypass

Если buyer использует платформу для просчёта/аналогов, а потом закрывает сделку off-platform — у нас **0 выручки за всю работу**. Удержание строится не запретами, а ценностью:

| Удерживающий механизм | Статус |
|----------------------|--------|
| Эскроу как обязательный шаг подтверждения сделки | ⚠️ Есть, не обязательный |
| Эксклюзивные цены поставщиков (off-platform дороже) | ❌ Нет |
| Документы под РФ-таможню (которые сам не сделаешь) | ✅ Есть через operator_customs |
| Гарантия качества / защита от брака | ✅ Есть Claim flow |
| Логистика под ключ (не нужно искать перевозчика) | ✅ Есть operator_logist |
| Закрепление клиент↔seller через успешные сделки | ⚠️ Есть KPI, но без gating |

**Критический пункт roadmap:** обязательный эскроу + эксклюзивные цены — иначе модель не масштабируется.

---

## 3. Архитектура

### 3.1 Гибридный AI (3 уровня)

Принцип: **код для точности, LLM для ума**. ~95% запросов в B2B procurement предсказуемы — нет смысла платить и ждать LLM.

```
Запрос пользователя
        │
        ▼
┌───────────────────┐
│  Fast-path (Code) │  95%, 30мс, $0    — 10 правил intent (мои rfq, мои заказы, paste артикулов, кп, бюджет, …)
└─────────┬─────────┘
          │ no match
          ▼
┌───────────────────┐
│  Llama 3.3 70B    │  4%, ~1с, $0.0002 — простой RAG-поиск, формулировка ответов
│  (DeepSeek V3)    │
└─────────┬─────────┘
          │ complexity > threshold
          ▼
┌───────────────────┐
│  Claude Sonnet 4  │  1%, ~5с, $0.003  — сложное (negotiation, multi-step planning, multilingual)
└───────────────────┘
```

**Текущее состояние:** fast-path реализован (`assistant/fast_path.py`), Claude tool-use работает, Llama пока не подключён (использует Claude как fallback).

### 3.2 Tool-use агент

Claude и Llama не отвечают свободным текстом — они **выбирают и вызывают tools** (функции в `assistant/actions.py`). Каждый tool возвращает структурированный `ActionResult { text, cards, actions, suggestions }`. Frontend рендерит карточки и кнопки.

Существующие tools (15):
- `search_parts`, `analyze_spec`, `top_suppliers`, `compare_products`, `compare_suppliers`
- `create_rfq`, `get_rfq_status`, `respond_rfq`, `generate_proposal`
- `get_orders`, `get_order_detail`, `track_shipment`
- `get_budget`, `get_analytics`, `get_demand_report`, `get_sla_report`
- `get_claims`, `create_claim`, `upload_pricelist`, `upload_parts_list`
- `open_url`

### 3.3 Стек (актуальный, не Next.js)

| Слой | Что | Почему |
|------|-----|--------|
| Backend | **Django 5.1 + DRF + Channels** | Уже работает, не переписываем |
| Realtime | **WebSocket (Channels) + REST fallback** | Streaming Claude tokens |
| Frontend | **Django templates + vanilla JS + WebSocket** | Без React/Next — приложение за логином, SSR не нужен. htmx/Alpine можно добавить если разрастётся |
| База | **PostgreSQL 15 + pgvector** | Семантический поиск, не нужен Neo4j отдельно |
| Очереди | **Celery + Redis** | Импорт каталогов, embeddings, email |
| Файлы | **Django storage / S3-совместимый** | Чертежи, документы, аватары |
| AI | **Anthropic Claude API + (TBD: Llama API через Together/Groq)** | Не self-host пока — операционная сложность |
| Vision | **Claude 3.5 Sonnet Vision** для OCR чертежей, **DINOv2** для image search (TBD) | CLIP устарел |
| Деплой | **Hetzner / Timeweb VPS, Docker** | $80-150/мес на старте |

---

## 4. Что уже сделано

### 4.1 Backend / модели

- `User` + `UserProfile` с ролями (buyer/seller/operator_*/admin)
- `Part`, `Brand`, `Category`, `PartAnalogue` (каталог запчастей)
- `Project`, `ProjectDocument` (группировка работы по проектам клиента)
- `RFQ`, `RFQItem`, `RFQResponse` (запросы котировок)
- `Order`, `OrderItem`, `OrderStatusHistory`, `OrderDocument` (заказы с 12 статусами)
- `Claim`, `ClaimStatus` (рекламации)
- `Conversation`, `Message` (чат + история)
- Платёжные схемы: 10/90, 10/50/40
- Документооборот: invoice PDF (`order_invoice_pdf`), proposal PDF (`rfq_proposal_pdf`)

### 4.2 Operator-кабинеты (классический UI)

- `operator/manager` — orders, negotiations, analytics, dashboard
- `operator/logist` — documents, ports, dashboard, analytics
- `operator/customs` — documents, dashboard, analytics
- `operator/payments` — invoices, escrow, reconciliation, dashboard, analytics

### 4.3 Chat-first слой (текущая сессия)

- `templates/chat/index.html` — главный чат (sidebar + welcome + thread + input)
- `templates/chat/project.html` — страница проекта в chat-first стиле
- `templates/chat/rfq.html` — детали RFQ
- `templates/chat/proposal.html` — коммерческое предложение
- `static/js/chat-first.js` — WebSocket, streaming, карточки, history persistence
- `static/js/project-page.js`, `rfq-page.js`, `proposal-page.js`
- Редиректы со старого UI: `/rfq/<id>/` → `/chat/rfq/<id>/`, `/rfq/<id>/proposal/` → `/chat/proposal/<id>/`

### 4.4 AI слой

- `assistant/rag.py` — sync + streaming pipeline, RAG поиск, контекст
- `assistant/actions.py` — 21 tool с JSON schema для Claude tool-use
- `assistant/fast_path.py` — 10 детерминированных intent-правил, ~30мс latency
- `assistant/consumers.py` — WebSocket с lazy conversation creation
- `assistant/prompts.py` — system prompt по ролям

### 4.5 Карточки в чате

`product`, `rfq`, `order`, `shipment`, `supplier`, `comparison`, `chart`, `spec_results`, `supplier_top`, `actions`

### 4.6 Demo

- `demo_buyer / demo_seller / demo_operator` (`demo12345`)
- Сидер `manage.py seed --all-demo` с проектами и заказами

---

## 5. Что ещё нужно (приоритезированный roadmap)

### Tier 0: критические дыры (1-2 недели)

**T0.1 Обязательный эскроу для подтверждения сделки**
- Кнопка «Подтвердить заказ» в чате запускает оплату резерва (10%)
- Без этого Order не переходит из `pending` в `confirmed`
- Сейчас Order можно создать без оплаты — закрывает bypass-риск

**T0.2 Платёжный шлюз**
- Интеграция с Tinkoff Business / ЮKassa / Sberbank (выбор по партнёрке)
- Эскроу-счёт для buyer→platform→seller
- Webhook-обработка платежей в `Order.payment_status`

**T0.3 Email уведомления**
- Seller: новый RFQ + дедлайн ответа
- Buyer: получены КП, статус заказа, документы готовы
- Operator: новые задачи в очереди

**T0.4 Pipeline view для buyer (Kanban)**
- 30 одновременных RFQ через chat невозможно отслеживать
- Нужна вьюха: `[Просчёт] [RFQ] [КП получены] [Контракт] [Производство] [Логистика] [Таможня] [Доставлен]`
- Клик на сделку → попадает в чат по этой сделке
- URL: `/chat/pipeline/`

### Tier 1: chat-first упаковка остальных шагов (2-3 недели)

**T1.1 Track shipment в чате**
- `/chat/order/<id>/` — карточка с прогресс-баром, ETA, документами
- Сейчас buyer лезет в operator's dashboard

**T1.2 Документы заказа в чате**
- Tool `get_order_documents` возвращает список с превью + ссылка на PDF
- ГТД, сертификаты, упаковочные листы, акты

**T1.3 Open claim голосом / в чате**
- «Рекламация по PO-22841: 3 фильтра с трещинами» → создаёт Claim, прикрепляет фото
- Tool `create_claim` уже есть — нужен voice/photo upload

**T1.4 Seller chat-first**
- AI приходит к seller'у: «Новый RFQ #38 от Polyus, 10 позиций, до 15:00»
- Seller отвечает в чате: «4280 за CR5953, лидтайм 14, остальное завтра»
- AI парсит → создаёт RFQResponse
- Это снимет 80% времени seller'а на ответы

### Tier 2: AI superpower (3-4 недели)

**T2.1 Visual search**
- Загрузил фото детали → AI находит в каталоге
- DINOv2 embeddings + pgvector
- MVP: 1000 размеченных фото

**T2.2 OCR чертежей**
- Загрузил скан Komatsu parts book → AI распознал номер позиции и название
- Claude 3.5 Sonnet Vision (нет смысла self-host)

**T2.3 Negotiation Agent**
- «Найди эту цену на 15% дешевле» → Claude пишет 5 поставщикам, торгуется, возвращается с лучшим
- Multi-step tool-use с rate limiting

**T2.4 Email-inbound**
- Buyer форвардит письмо «нужно 50 фильтров для D6R» на rfq@consolidator
- Парсинг вложений (Excel, PDF)
- Создаёт RFQ автоматически
- **Снимает онбординг-трение** — закупщик не меняет привычку

### Tier 3: моат (4-6 недель)

**T3.1 Pricing intelligence**
- Дневной парсинг прайсов поставщиков
- Алерты buyer'у: «Цена на CR5953 выросла на 12% — есть аналог дешевле»
- Прогноз цен на 3 месяца

**T3.2 Predictive sales по парку**
- Telematics integration (Komtrax, Equipment Manager, CareTrack)
- AI знает наработку машин клиента → за 2 недели до ТО создаёт RFQ
- **Это упоминалось в v3 ТЗ как killer feature** — переоценил, не main moat, но важный value-add

**T3.3 Reverse marketplace**
- Seller: «У меня 50 цепей CR5953 со скидкой 30% — у каких 5 buyer'ов это нужно?»
- AI находит совпадения по их parts history → инициирует диалог
- Закрывает stock'и продавцам, экономит buyer'ам

**T3.4 WhatsApp / Telegram bot**
- Поставщик в Китае → в WhatsApp кнопки «Готов / Не могу / Уточню»
- Снимает онбординг для seller-side

### Tier 4: масштабирование (6+ недель)

- Compliance проверки (ФНС/ЕГРЮЛ, санкции, dual-use)
- Multi-currency + FX hedging
- ERP интеграции (1С, SAP)
- Аналитика для admin (cohort, retention, LTV)
- Mobile native (React Native поверх существующего API)

---

## 6. Метрики успеха

### Продуктовые

| Метрика | Цель MVP | Цель Year 1 |
|---------|----------|-------------|
| Time to RFQ (от мысли до отправки) | <60с | <30с |
| Auto-match accuracy (правильный аналог без человека) | 70% | 90% |
| % сделок закрытых внутри платформы (anti-bypass) | 60% | 85% |
| Avg задержка ответа AI (fast-path) | 50мс | 30мс |
| Avg задержка LLM-ответа | 5с | 3с |
| Cost per AI request | $0.003 | $0.0005 (за счёт fast-path и Llama) |

### Бизнес

| Метрика | 3 мес | 6 мес | 12 мес |
|---------|-------|-------|--------|
| Active buyers | 5 | 25 | 100 |
| Active sellers | 20 | 100 | 400 |
| GMV (оборот через платформу) | $500K | $5M | $30M |
| Revenue (5% от GMV) | $25K | $250K | $1.5M |
| NPS buyer | 30 | 50 | 60 |

---

## 7. Принципы разработки

1. **Chat-first для buyer/seller, classic UI для operator/admin.** Не перепиливать operator-кабинеты в чат.
2. **Каждая фича = code-first, LLM как fallback.** Если можно решить регуляркой — не дёргаем Claude.
3. **Никаких form-submit для типичных действий.** Кнопка в чате запускает action → результат в карточке.
4. **Всё через WebSocket, REST как fallback.** Streaming Claude tokens обязателен.
5. **Один UI / один URL на роль.** Buyer не должен помнить «зайти в раздел RFQ → найти кнопку».
6. **AI вызывает БД через tools, не выдумывает.** Любая цифра в ответе — это результат `Part.objects.get(...)`.
7. **Редизайн через chat-first аналог, не через переделку старого.** Старый URL `/rfq/<id>/` → 302 → `/chat/rfq/<id>/`.

---

## 8. Сравнение с v3 (что изменилось)

| Что | v3 (Mansour) | v4 (текущая) | Почему |
|-----|--------------|--------------|--------|
| AI router | 30% Claude / 70% Llama | 95% fast-path / 4% Llama / 1% Claude | Большинство запросов B2B детерминированы |
| Frontend | Next.js 14 + React | Django templates + vanilla JS | Уже работает, переписывать = 4-6 недель в трубу |
| Граф связей | Neo4j | PostgreSQL + JSONB | 50M связей PG держит, на одну БД меньше |
| Visual search | CLIP | DINOv2 + Claude Vision OCR | CLIP устарел, для деталей хуже |
| Llama hosting | Self-hosted Llama 4 | API через Together/Groq | Self-host = $2K/мес GPU, не окупится |
| Self-hosted infra | 3 dedicated сервера | 1 VPS на старте | Бережём $700/мес пока трафик мал |
| Telematics auto-RFQ | Killer feature | Tier 3 (важный, но не main moat) | Главный moat — закрытый end-to-end процесс |
| Email inbound | — | Tier 2 (критично для adoption) | B2B живёт в почте, без интеграции онбординг тяжёлый |
| Обязательный эскроу | — | Tier 0 (критично) | Без него bypass-риск убивает 5% модель |
| Pipeline view (Kanban) | — | Tier 0 (критично) | 30 одновременных RFQ в чате невозможно отследить |

---

## 9. Что делаем прямо сейчас (next sprint)

**Sprint 1 (1 неделя):**
1. Pipeline view для buyer (`/chat/pipeline/`) — Kanban 30 сделок
2. Tool `get_pipeline` + fast-path для «покажи мои сделки в работе»
3. Карточка `pipeline_item` с прогрессом

**Sprint 2 (1 неделя):**
4. Обязательный эскроу-flow: «Подтвердить заказ» в чате → платёж резерва
5. Webhook-обработка ЮKassa/Tinkoff
6. Email уведомления (seller на RFQ, buyer на КП)

**Sprint 3 (1 неделя):**
7. Track shipment в чате (`/chat/order/<id>/`)
8. Документы заказа в чате (tool `get_order_documents`)
9. Photo upload + Vision OCR для номера детали

**Sprint 4 (1 неделя):**
10. Seller chat-first — AI парсит ответ seller'а в RFQResponse
11. Email-inbound rfq@consolidator (парсинг вложений → создание RFQ)

После 4 недель: **end-to-end закрыт в chat-first для buyer, seller обновлён, эскроу обязателен — готовы к платному beta с 3-5 buyer'ами**.

---

## Контакты

**Owner:** Mansour, 1@legacypostco.tech
**Repo:** legacypostco-collab/workspace-app, ветка `chat-first`
**Сервер:** `python manage.py runserver 8001`, demo: `demo_buyer/demo_seller/demo_operator` / `demo12345`
