# Consolidator Parts
## Технический и бизнес-спецификационный документ

**Версия:** 6.0 (FINAL — own LLM strategy)
**Дата:** 30 апреля 2026
**Owner:** Mansour | 1@legacypostco.tech
**Статус:** Утверждено к реализации
**Главное изменение vs v5:** LLM в проде — собственная fine-tuned модель, не Claude. Claude только bootstrap.

---

## Содержание

1. [Executive Summary](#1-executive-summary)
2. [Видение продукта](#2-видение-продукта)
3. [Позиционирование и конкуренция](#3-позиционирование-и-конкуренция)
4. [Целевая аудитория](#4-целевая-аудитория)
5. [Бизнес-модель и юнит-экономика](#5-бизнес-модель-и-юнит-экономика)
6. [Use cases — три способа работы](#6-use-cases--три-способа-работы)
7. [Архитектура продукта](#7-архитектура-продукта)
8. [Технологический стек (окончательный)](#8-технологический-стек-окончательный)
9. [AI архитектура](#9-ai-архитектура)
10. [Доменная модель](#10-доменная-модель)
11. [Что уже реализовано](#11-что-уже-реализовано)
12. [Roadmap (приоритезированный)](#12-roadmap-приоритезированный)
13. [UI/UX принципы](#13-uiux-принципы)
14. [Безопасность и compliance](#14-безопасность-и-compliance)
15. [Метрики и аналитика](#15-метрики-и-аналитика)
16. [Риски и митигации](#16-риски-и-митигации)
17. [Принципы разработки](#17-принципы-разработки)
18. [Что НЕ делаем (явный антибэклог)](#18-что-не-делаем-явный-антибэклог)
19. [Глоссарий](#19-глоссарий)

---

## 1. Executive Summary

**Consolidator Parts** — это end-to-end B2B procurement platform для запчастей тяжёлой техники (горнодобыча, строительство, дорожная техника), упакованная в единый AI-чат.

**Аналог по бизнес-модели:** Flexport (логистика) + Stripe (платежи) для запчастей.
**Аналог по UX:** Cursor (AI-управляемый workflow) + Linear (Kanban для сделок).

**Главное отличие от конкурентов:**
- Конкуренты дают только прайс по списку — закупщик дальше всё делает руками (звонки, Excel, банк, таможня, документы)
- Мы закрываем **весь цикл** в одном интерфейсе: поиск → RFQ → контракт → эскроу → производство → логистика → таможня → документы → приёмка → закрытие
- AI ведёт сделку как **профессиональный снабженец** с многолетним опытом

**Бизнес-модель:** 5% с оборота (3% buyer + 2% seller). Юнит-экономика: ~$180K/год с одного активного buyer'а.

**Команда:** 1 Founder/Owner + Claude Code как dev-resource.
**Бюджет инфраструктуры на старте:** ~$80/мес (1 VPS + Claude API).
**Целевой запуск beta:** через 6 недель от текущей даты.

---

## 2. Видение продукта

### 2.1 Проблема

Закупщик в B2B сегменте тяжёлой техники сегодня живёт в **аду фрагментации**:

| Что делает | Где делает | Время |
|------------|-----------|-------|
| Подбор аналогов | Excel + 5 каталогов производителей | 2-3 часа |
| Запрос цен | Email/WhatsApp 5-10 поставщикам | 30-60 мин на запрос |
| Сравнение КП | Excel вручную | 30 мин на сделку |
| Согласование с финансами | Скайп/звонки/документы туда-сюда | 1-3 дня |
| Оплата | Банк-клиент, реквизиты, валютный контроль | 30-60 мин |
| Контроль производства | WhatsApp с поставщиком | постоянно |
| Логистика | Звонки экспедиторам | 1-2 дня |
| Таможня | Брокер по почте | 1-2 дня |
| Документы | Excel списки актов, ГТД, сертификатов | 1-2 часа на партию |
| Приёмка | На складе вручную, рекламации голосом | 1 день |

**Итого:** на одну сделку уходит ~10 рабочих дней времени снабженца. При 30 одновременных сделках — это full-time job для целой команды.

### 2.2 Решение

**Один чат, в котором снабженец:**

1. **Управляет сделками командами:** «сделай RFQ на 10 цепей CR5953» → готово за 30мс
2. **Советуется с AI как с экспертом:** «Berco vs ITM для D6R в шахте −40°C?» → расчёт TCO за 5с
3. **Контролирует поставку:** «где PO-22841?» → актуальный статус, риски, варианты действий
4. **Получает проактивные алерты:** AI пишет первым: «по PO-22829 риск задержки на таможне, делаю Х»

**Платформа закрывает все 10 шагов цикла**, забирая 5% от сделки (вместо найма команды снабжения за $5-15K/мес).

### 2.3 North Star Metric

**Time from Intent to Confirmed Order** — время от мысли «нужны эти детали» до подписанного контракта с эскроу.

| Сегодня (без платформы) | Цель MVP | Цель Year 1 |
|--------------------------|----------|-------------|
| 5-10 рабочих дней | 1 рабочий день | 1-2 часа |

---

## 3. Позиционирование и конкуренция

### 3.1 Сегмент рынка

**Вертикаль:** Heavy equipment spare parts — горнодобывающая, строительная, дорожная техника.
**География:** Россия, СНГ, ЕАЭС (расширение в Юго-Восточную Азию через 12-18 мес).
**Размер рынка (TAM):** ~$8B/год оборота запчастей в РФ для тяжёлой техники.

### 3.2 Конкурентный ландшафт

| Категория | Примеры | Что дают | Чего не дают |
|-----------|---------|----------|--------------|
| **Прайс-агрегаторы** | СтройПартс, Aftermarket.ru | Цены по артикулу | Эскроу, логистика, таможня, документы |
| **Маркетплейсы AliExpress-style** | Alibaba, 1688 | Поставщики из Китая | Доверие, локальная логистика, документы под РФ |
| **Прямые отношения** | WhatsApp с «своим» поставщиком | Цена ниже, скорость | Прозрачность, защита от кидалова, аналитика |
| **Внутренние команды снабжения** | Найм 2-5 снабженцев | Полный контроль | $80-300K/год FOT, отпуска, ошибки, текучка |

### 3.3 Почему мы выигрываем

**Главное:** мы единственные предлагаем **закрытый end-to-end процесс** в одной системе.

| Capability | Конкуренты | Consolidator |
|------------|------------|--------------|
| Поиск аналогов | Только OEM номер | OEM + аналоги + кросс-референсы |
| RFQ автомат | Нет | Да, рассылка 5+ поставщикам |
| Сравнение КП | Excel | Карточка с TCO, SLA, рейтингом |
| Эскроу | Нет | Встроен через ЮKassa |
| Логистика | Нет | Operator-команда + интеграции |
| Таможня | Нет | Operator-команда + автоматизация ГТД |
| AI-консультант | Нет | Claude/Llama/fast-path |
| Документы | Excel | Автогенерация под РФ-законодательство |
| Аналитика | Нет | Per-buyer cohort, KPI, бюджеты |

### 3.4 Защитный ров (moats)

1. **Network effect:** больше buyers → больше RFQ → выгоднее sellers быть в системе → больше choice для buyers
2. **Knowledge accumulation:** каждая сделка обучает AI (analogue map, supplier ratings, price history, demand patterns)
3. **Data lock-in:** история закупок, KPI поставщиков, аналитика — мигрировать сложно
4. **Operational moat:** logist-customs-payments команда + интеграции с ЮKassa/банками — конкуренту построить = 2 года
5. **Bypass-protection:** обязательный эскроу + документы под РФ-таможню = off-platform = риск

---

## 4. Целевая аудитория

### 4.1 Buyer (главный плательщик)

**Профиль:**
- Снабженец / руководитель снабжения / директор по закупкам
- Работает в горнодобывающей, строительной, дорожной компании
- 30+ одновременных RFQ в работе
- KPI: экономия бюджета, минимизация простоев техники, скорость поставки

**Размер:**
- Малые торговые компании: 1-3 снабженца, оборот $1-10M/год
- Средние стройхолдинги: 5-15 снабженцев, оборот $10-100M/год
- Крупные горнодобывающие: 20-100 снабженцев, оборот $100M-1B/год

**Pricing target:** малые и средние (sweet spot $10-100M оборота → $500K-5M через платформу).

### 4.2 Seller (вторая сторона маркетплейса)

**Профиль:**
- OEM-производители (CAT, Komatsu, Volvo дилеры в РФ)
- Distributors-импортёры (российские склады запчастей)
- Aftermarket-производители (Berco, ITM, Xuzhou, китайские фабрики)

**Что хотят:**
- Качественные RFQ (не нагрузка на менеджера)
- Гарантия оплаты (через эскроу)
- Быстрый ответ (1-2 клика)
- Аналитика спроса (что чаще ищут)

### 4.3 Operator (платформенная команда)

**4 роли:**

| Роль | Что делает | KPI |
|------|------------|-----|
| **Manager** | Sales, переговоры, escalations | GMV, conversion |
| **Logist** | Организация доставки, маршруты, ETA | On-time delivery rate |
| **Customs** | Таможенное оформление, ГТД, сертификаты | Avg customs time, 0 incidents |
| **Payments** | Эскроу, платежи, сверка | Cash flow, 0 disputes |

### 4.4 Admin (founder)

**Что нужно:**
- Метрики платформы (GMV, retention, NPS)
- Биллинг и payouts
- Управление пользователями
- Health monitoring

---

## 5. Бизнес-модель и юнит-экономика

### 5.1 Источники выручки

**Основной:** 5% с GMV сделок прошедших через платформу.

**Распределение:**
- 3% с buyer — за consolidation (логистика+таможня+документооборот+AI)
- 2% с seller — за access to demand + платёжные гарантии

**Дополнительные источники (Tier 3):**
- Premium subscription для buyer'ов с >$1M оборота: $500/мес за расширенную аналитику + telematics
- White-label для крупных холдингов: $5-20K/мес
- API access для seller'ов с автоматизацией: $200-1000/мес

### 5.2 Юнит-экономика на одного buyer'а

**Допущения:**
- Средний чек сделки: $50,000
- Одновременных активных сделок: 30
- Цикл сделки: 6 недель average
- Конверсия RFQ → Order: 60%

**Расчёт:**
```
Активных RFQ × Цикл за квартал = 30 × (1 квартал / 1.5 цикла) ≈ 20 закрытых сделок/квартал
Closing rate 60% → 12 завершённых сделок/квартал
12 × $50,000 = $600,000 GMV/квартал
$600,000 × 5% = $30,000 platform revenue/квартал
$30,000 × 4 = $120,000/год с одного buyer'а
```

**При 100 активных buyer'ах через 12 мес:** $12M/год revenue.
**При 1000 buyers через 36 мес:** $120M/год revenue.

### 5.3 CAC и payback

**Customer Acquisition Cost** (целевой):
- Direct sales (cold outreach): ~$2,000 (1 sales person × 2 недели работы / closing rate)
- Inbound (после года развития контента): ~$500

**Payback period:** при $120K LTV первого года и $2K CAC = **6 дней** (т.е. одна крупная сделка покрывает CAC).

### 5.4 Главный экзистенциальный риск: bypass

Если buyer использует платформу для просчёта/аналогов, а потом закрывает сделку **off-platform** напрямую с поставщиком — у нас **0 выручки за всю работу**.

**Митигация (встраивается в продукт с Tier 0):**

1. **Обязательный эскроу:** Order не подтверждается без оплаты резерва (10%). Без подтверждённого Order — нет seller-side action.
2. **Эксклюзивные цены:** seller'ы соглашаются давать в платформу цены на 3-5% ниже своих обычных, в обмен на гарантию оплаты и снижение CAC.
3. **Документы под РФ-таможню:** автогенерация ГТД-готовой документации, сертификатов ТР ТС — то что buyer сам не сделает.
4. **Защита от брака:** Claim-flow с возвратом денег через эскроу — недоступно при off-platform сделке.
5. **Tracking + analytics:** AI помнит всё про buyer'а — его историю, любимых поставщиков, KPI. Off-platform = терять контекст.
6. **Logist-team под ключ:** платформа организует доставку, у buyer'а нет необходимости искать перевозчика.

---

## 6. Use cases — три способа работы

Все три должны быть равно сильными в продукте.

### 6.1 Команды (как с подчинённым)

```
Buyer: "сделай RFQ на 10 цепей CR5953 для нашего парка"

AI:    [Tool call: create_rfq(query="CR5953", quantity=10)]
       [Tool call: dispatch_to_suppliers(rfq_id=38, top_n=5)]

       "RFQ #38 создан, отправлен 5 поставщикам с лучшим SLA по этой
        категории. Дедлайн ответа — завтра 18:00."

       [Карточка RFQ #38]
       [Кнопки: Открыть RFQ | Изменить поставщиков | Отменить]
```

**Реализация:** fast-path или Claude tool-use → `create_rfq` action → возврат `ActionResult` с карточкой и кнопками.

### 6.2 Консультации (как с экспертом)

```
Buyer: "что лучше для D6R XL — Berco на $4280 или ITM на $2890?
        у меня шахта в -40, машина работает 18 часов в сутки"

AI:    [Tool call: get_part_specs(part="Berco CR5953")]
       [Tool call: get_part_specs(part="ITM CR5953")]
       [Tool call: get_historical_lifecycle(category="track_chain",
                                            climate="arctic",
                                            usage="18h_day")]

       "В таких условиях Berco — лучший выбор, несмотря на +48% цены:
        - Закалка цепей под низкие температуры (-50°C, у ITM до -30°C)
        - Ресурс ~6500 моточасов vs ITM ~4200 в твоём режиме
        - На 18ч/сутки = ITM придётся менять каждые 8 месяцев,
          Berco — раз в 12 месяцев

        TCO за 3 года:
        Berco: $4280 × 3 замены = $12,840
        ITM:   $2890 × 5 замен  = $14,450 + 2 простоя (×$8K) = $30,450

        Berco экономит ~$17K за 3 года. Открыть RFQ на Berco?"

       [Кнопка: Создать RFQ на Berco CR5953]
```

**Реализация:** Claude tool-use с tools для specs, lifecycle data, TCO calculation. **Knowledge accumulation layer** (Tier 3) обогащает базу реальными данными с каждой завершённой сделки.

### 6.3 Контроль поставки (как с командой)

#### 6.3.1 Запрос статуса

```
Buyer: "где моя поставка PO-22841?"

AI:    [Tool call: get_order_detail(order_id=22841)]
       [Tool call: get_shipment_status(order_id=22841)]
       [Tool call: get_pending_actions(order_id=22841)]

       "PO-22841 на таможне в Маньчжурии, день 3 из ~5.
        ETA в твоём складе — 8 мая (через 7 дней).

        ⚠️ Таможенный брокер запросил уточнение по позиции 4:
        номер CR5953 пробит без HS-кода. Нужен ответ до завтра 12:00."

       [Кнопки: Ответить брокеру | Связать с logist Сергеем |
        Видео-звонок в чат]
```

#### 6.3.2 Срочная ситуация (active management)

```
Buyer: "у меня машина встала, нужны эти 3 детали ВЧЕРА"

AI:    [Tool call: search_parts(articles=[...])]
       [Tool call: check_inventory_local(articles=[...])]
       [Tool call: estimate_express_delivery(...)]

       "Понял, делаю срочно. Текущий статус:
        - 2 из 3 есть на складе у поставщика в Шанхае
        - 1 деталь — только под заказ, минимум 2 недели

        Варианты для срочной:
        А) Найти аналог из РФ-склада (есть Berco, +30% цена, готов сейчас)
        Б) Авиа-доставка из Шанхая (5 дней, +$1200 к фрахту)

        Что делаем?"

       [Кнопки: Авиа-доставка | Аналог из РФ |
        Связать с менеджером сейчас]
```

#### 6.3.3 Проактивный алерт

```
[7:30 утра, AI пишет первым:]

AI:    "Доброе утро. За ночь:
        - Получено 4 ответа на RFQ #38 (лучший — Caterpillar, $48,420,
          на $3K дешевле бенчмарка)
        - PO-22829 прошла таможню, прибудет 3 мая
        - Поставщик Heavy Equipment не ответил на RFQ #41 — дедлайн
          через 2 часа. Написать им напоминание?
        - На складе у тебя выходит ресурс по 12 деталям до конца
          квартала (анализ по парку техники), подготовить
          превентивный RFQ?"

       [Кнопки: Принять CAT $48,420 | Напомнить Heavy Equipment |
        Подготовить превентивный RFQ | Подробнее]
```

**Реализация:** background scheduler (Celery beat) формирует daily brief, AI пишет в чат через WebSocket push. Tier 1.

---

## 7. Архитектура продукта

### 7.1 Высокоуровневая диаграмма

```
                    ┌─────────────────────────────┐
                    │      Browser (Vanilla JS)    │
                    │   Chat-First UI + WebSocket  │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │        Nginx (TLS)            │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │   Django + DRF + Channels     │
                    │  ┌─────────────────────────┐  │
                    │  │  REST API (DRF)          │  │
                    │  │  WebSocket (Channels)    │  │
                    │  │  Background (Celery)     │  │
                    │  └─────────────────────────┘  │
                    └─────────────┬───────────────┘
                                  │
              ┌───────────────────┼─────────────────────┐
              │                   │                     │
              ▼                   ▼                     ▼
     ┌────────────────┐  ┌──────────────────┐  ┌─────────────────┐
     │  PostgreSQL     │  │      Redis        │  │  AI Brain        │
     │  + pgvector     │  │  (cache + queue) │  │                  │
     │                 │  │                  │  │  ┌────────────┐  │
     │  - Users        │  │  - Sessions      │  │  │ Fast-path  │  │
     │  - Parts (7M)   │  │  - Celery tasks  │  │  │  (regex)   │  │
     │  - RFQ/Orders   │  │  - WebSocket pub │  │  └─────┬──────┘  │
     │  - Conversations│  │                  │  │        │         │
     │  - Embeddings   │  │                  │  │  ┌─────▼──────┐  │
     │                 │  │                  │  │  │  Claude    │  │
     └────────────────┘  └──────────────────┘  │  │  Sonnet    │  │
                                                │  └────────────┘  │
                                                │                  │
                                                │  ┌────────────┐  │
                                                │  │ Llama API  │  │
                                                │  │ (Tier 2+)  │  │
                                                │  └────────────┘  │
                                                └─────────────────┘
                                                          │
                                                ┌─────────▼──────┐
                                                │  External APIs  │
                                                │  - ЮKassa       │
                                                │  - SendGrid     │
                                                │  - Telematics   │
                                                │  - SDN/санкции  │
                                                └─────────────────┘
```

### 7.2 Слои приложения

#### Слой 1: Frontend (один thin client)

- HTML templates рендерятся Django
- Vanilla JS для динамики
- WebSocket для real-time
- REST API как fallback

#### Слой 2: API & Realtime (Django/DRF/Channels)

- REST endpoints для CRUD и actions (`/api/v1/...`, `/api/assistant/...`)
- WebSocket consumers для чата (`/ws/assistant/...`)
- Authorization через DRF permissions + role checks

#### Слой 3: Domain logic

- Models в `marketplace/models.py`, `assistant/models.py`
- Business logic в views (RFQ flow, Order flow)
- Actions в `assistant/actions.py` — единая точка для всех операций доступных через AI

#### Слой 4: AI Brain

- `assistant/fast_path.py` — детерминированный routing
- `assistant/rag.py` — context retrieval + Claude tool-use loop
- `assistant/prompts.py` — system prompts по ролям

#### Слой 5: Persistence

- PostgreSQL — основные данные
- pgvector — embeddings для семантического поиска
- Redis — кеш, очереди, WebSocket pub/sub
- File storage — Django default → S3-совместимый когда понадобится

#### Слой 6: External integrations

- **ЮKassa** — платежи и эскроу
- **SendGrid** → **Yandex 360 Business** — email
- **Telematics APIs** (Komtrax, Caterpillar, Volvo) — Tier 3
- **SDN/Санкции APIs** — compliance, Tier 4

---

## 8. Технологический стек (окончательный)

### 8.1 Backend

| Компонент | Выбор | Версия |
|-----------|-------|--------|
| Language | Python | 3.11+ |
| Framework | Django | 5.1 |
| API | Django REST Framework | 3.15+ |
| Realtime | Django Channels | 4.x |
| ASGI server | Daphne | latest |
| WSGI server (REST) | Gunicorn | latest |
| Background jobs | Celery + Redis | 5.x / 7.x |
| Cache | Redis | 7.x |
| Database | PostgreSQL + pgvector | 15+ |

**Обоснование:** Django стек уже работает в проекте, переписывать = терять 6 недель на рефакторинг ради нулевого выигрыша.

### 8.2 Frontend

| Компонент | Выбор |
|-----------|-------|
| Templating | Django templates |
| JS | Vanilla ES2020+ |
| CSS | Inline + static files |
| Realtime | Native WebSocket |
| Build | NONE (no bundler, no npm) |

**Когда добавим (триггеры):**
- htmx + Alpine.js — когда фронт > 3000 строк JS или появляется hypermedia-логика
- React (только для cложных карточек как graphs/charts) — когда таких карточек > 5

**НЕ добавляем никогда:** Next.js, full SPA, TypeScript на фронте, Tailwind.

### 8.3 AI

| Компонент | Выбор |
|-----------|-------|
| LLM в проде (target) | **Своя fine-tuned модель** на базе Qwen 2.5 14B / Llama 3.3 70B |
| LLM bootstrap (Phase 1) | Claude Sonnet 4 через Anthropic API — для сбора training data |
| Inference серверы (Phase 3) | vLLM на 1× A100 GPU (или Together AI per-token) |
| Training infrastructure | 1× A100 80GB для LoRA fine-tuning, ~$300-500 за прогон |
| Base models | Qwen 2.5 14B (RU/ZH сильна) или Llama 3.3 70B (EN сильнее) |
| Fine-tuning toolkit | **axolotl** или **unsloth** (для LoRA на A100) |
| Vision (OCR/visual search) | Claude 3.5 Sonnet Vision (Phase 1) → fine-tuned LLaVA (Phase 3) |
| Routing | Custom Python (`assistant/fast_path.py`) |
| Embeddings | Sentence-Transformers (multilingual) |
| Vector store | pgvector в PostgreSQL |
| Data collection | Логи Claude + operator labels + sделки (см. 9.3) |

### 8.4 Платежи

| Компонент | Выбор | Почему |
|-----------|-------|--------|
| Primary | **ЮKassa (Yookassa)** | Эскроу из коробки, 54-ФЗ, юр.лица |
| Backup | Tinkoff Business | Прямые счёт-на-счёт |
| Multi-currency | TBD (Rapyd / Wise Business) | Tier 3 |

### 8.5 Email

| Компонент | Выбор |
|-----------|-------|
| До 1000 писем/день | SendGrid SMTP |
| После 1000 писем/день | Yandex 360 Business |
| Транзакционные шаблоны | Django templates → text + html |

### 8.6 Storage

| Компонент | Выбор |
|-----------|-------|
| Local dev | Django filesystem |
| Production до 50GB | Selectel Object Storage / Yandex Object Storage |
| Production > 50GB | MinIO self-hosted на отдельном VPS |

### 8.7 Monitoring

| Компонент | Выбор |
|-----------|-------|
| Errors | Sentry (free tier 5K events/мес) |
| Logs | Django logging → файлы → **journalctl** |
| Metrics (Tier 1) | Grafana + Prometheus (после 100 active users) |
| Uptime | UptimeRobot (free) |

### 8.8 Infrastructure

| Компонент | Выбор |
|-----------|-------|
| Hosting | Hetzner (Germany) или Timeweb (РФ) |
| Server (start) | CCX23 / 4 vCPU / 16GB RAM / ~€30/мес |
| Container | Docker + docker-compose |
| Reverse proxy | Nginx |
| TLS | Let's Encrypt |
| CI/CD | GitHub Actions → SSH deploy |
| Backup | Daily PostgreSQL dumps → S3 |

**Когда масштабироваться:**
- > 5K active users → выносим PostgreSQL на отдельный managed instance
- > 50K active users → load balancer + 2-3 app servers
- > 500K active users → Kubernetes

### 8.9 Аутентификация

| Компонент | Выбор |
|-----------|-------|
| Web | Django sessions (cookie-based) |
| API/Mobile | DRF Token Authentication |
| OAuth | НЕ делаем (B2B context) |

### 8.10 Полный список зависимостей (Python requirements)

```
Django==5.1
djangorestframework==3.15
channels==4.0
daphne==4.0
celery==5.4
redis==5.0
psycopg[binary]==3.2
pgvector==0.3
anthropic==0.40
sentence-transformers==3.0
yookassa==3.0
sendgrid==6.11
sentry-sdk==2.0
django-cors-headers==4.5
drf-spectacular==0.27
weasyprint==62.0  # PDF generation
pillow==10.4
python-magic==0.4
```

---

## 9. AI архитектура

### 9.1 Главный архитектурный принцип

**Где нельзя обманывать — там нет LLM. Где нужен живой язык — там собственная LLM, обученная на нашем опыте.**

Жёсткое разделение ответственности:

```
ACTIONS + DATA              TALK + ADVICE
(нельзя ошибаться)          (нужен живой язык)

• Поиск артикулов            • Объяснения
• Создание RFQ               • Консультации
• Цены товаров               • Smalltalk / Help
• Рейтинги SLA               • Переговорная помощь
• Сроки доставки             • Reasoning «почему»
• Статусы заказов            • Парсинг свободных формулировок
• Платежи / эскроу           • Onboarding
• Документы

         ↓                            ↓
    ДЕТЕРМИНИРОВАННЫЙ КОД        СОБСТВЕННАЯ LLM
    (fast-path Python)            (fine-tuned on own data)

    Источник истины:              Источник знаний:
    PostgreSQL                    опыт сотрудников +
                                   user feedback +
    0% галлюцинаций                история сделок
    100% воспроизводимо
```

Ни одна цифра, цена, рейтинг, срок — не приходит из LLM. Они только из БД через код. LLM работает только там где допустима вариативность языка: советы, объяснения, парсинг свободных формулировок пользователя.

### 9.2 Three-stage routing

```
        Запрос пользователя
                │
                ▼
        ┌───────────────┐
        │  Fast-path    │  ← 95% запросов, 30мс, $0
        │  (regex/rules)│
        └───────┬───────┘
                │ no match
                ▼
        ┌───────────────┐
        │  Own LLM      │  ← 5% запросов, 1-2с, ~$0.0001
        │  (fine-tuned  │     (Claude используется только для bootstrap
        │   Qwen / Llama)     и сбора training data на старте)
        └───────────────┘
```

### 9.3 Собственная LLM (own fine-tuned model)

LLM в продакшне — **наша**, обученная на нашем опыте. Anthropic Claude используется временно на этапе bootstrap для сбора training data, потом отключается.

**Почему собственная:**

| | Claude в проде | Своя LLM |
|--|----------------|----------|
| Стоимость 1 запроса | $0.003 | ~$0.0001 (только GPU) |
| Latency | 5 сек (vendor API) | 1-2 сек (свой сервер) |
| Vendor lock-in | Высокий | Нулевой |
| Privacy | Данные → США | 100% on-premise |
| Знание вертикали | Общее | Эксклюзивное |
| IP / patent moat | Нет | Да — наша модель |
| Регуляторика РФ | Риск | Безопасно |

**Phase 1 — Bootstrap (Claude как teacher):**
- Claude временно обрабатывает «conversation» запросы в проде
- Каждый его ответ + контекст + действия пользователя = training example
- Operator-команда (manager/logist/customs/payments) комментирует ответы — это золотые метки
- Цель: 100K-500K качественных примеров «вопрос → правильный ответ → обоснование»

**Phase 2 — Fine-tuning:**
- База: **Qwen 2.5 14B** (отлично для русского + китайского) или Llama 3.3 70B
- LoRA fine-tuning на нашем датасете (не full retraining)
- Training infrastructure: 1× A100 GPU
- Несколько итераций → выбираем лучшую по eval-метрикам
- Размещение: 1 GPU-сервер или Together AI per-token

**Phase 3 — Production:**
- Своя LLM в проде, Claude отключаем (или fallback на edge cases)
- Каждая новая сделка → новые training examples
- Регулярный re-fine-tune → модель умнеет с каждой неделей эксплуатации

**Источники training data (для Phase 1):**

| Источник | Что копим | Качество |
|----------|-----------|----------|
| Логи Claude в Phase 1 | Ответы AI на реальные запросы | Высокое (после ревью operator) |
| Опыт операторов | Manager/logist/customs answers на типовые вопросы | Золотая метка |
| История сделок | RFQ → ответ → результат → feedback | Лучшее для consult-режима |
| Документация маркетплейса | Help статьи, onboarding flows | Для help-режима |
| Synthetic data | Claude генерит варианты формулировок одного intent | Расширение словаря |

**Никогда не обучаем модель на:**
- Ценах (всегда из БД)
- Рейтингах поставщиков (всегда из БД)
- Сроках доставки (всегда из БД)
- Статусах заказов (всегда из БД)
- Любых fact-data

LLM учится **формулировать** — не **знать факты**.

### 9.4 Fast-path (`assistant/fast_path.py`)

**10+ детерминированных правил:**

| Rule | Триггеры | Action |
|------|----------|--------|
| `multi_article_paste` | 2+ OEM-номера в сообщении | `search_parts` со списком |
| `show_rfqs` | "мои rfq", "активные котировки", "show rfq" | `get_rfq_status` |
| `show_orders` | "мои заказы", "статус заказов" | `get_orders` |
| `generate_proposal` | "сформируй кп", "make proposal" | `generate_proposal` |
| `budget` | "бюджет", "расходы", "сколько потратили" | `get_budget` |
| `analytics` | "аналитика", "kpi", "дашборд" | `get_analytics` |
| `sla_report` | "sla", "просрочки" | `get_sla_report` |
| `claims` | "рекламация", "брак", "претензия" | `get_claims` |
| `track_shipment` | "трекинг", "где заказ" | `track_shipment` |
| `demand_report` | "спрос", "что ищут" | `get_demand_report` |
| `top_suppliers` | "топ поставщики", "сравни поставщиков" | `top_suppliers` |
| `pipeline` | "мои сделки", "что в работе" | `get_pipeline` (Tier 0) |

**Расширение:** добавлять новое правило за 5 минут. Триггер → action mapping → fast-path автоматически использует `action_executor`.

### 9.5 Tool-use (для актуальной LLM — Claude в Phase 1, своя в Phase 3)

Все LLM ответы только через `tools=[...]`. Никаких ответов свободным текстом для действий.

**Tools:** 21+ (см. список в `assistant/actions.py`)

Каждый tool:
- Имеет JSON schema (`TOOL_SCHEMAS` dict)
- Возвращает `ActionResult(text, cards, actions, suggestions)`
- Запускается через `action_executor.execute(name, params, user, role)`
- Подчиняется role permissions (`ROLE_ACTIONS` dict)

**Agentic loop** (`_run_claude_with_tools` в `rag.py`):
1. Send messages + tools to Claude
2. Получаем `tool_use` blocks
3. Выполняем tools параллельно где возможно
4. Возвращаем `tool_result` обратно в Claude
5. Повторяем пока Claude не вернёт `end_turn`
6. Финальный текст — короткий human label

### 9.6 ActionResult — единый product primitive

```python
@dataclass
class ActionResult:
    text: str = ""                # короткое сообщение для пользователя
    cards: list[dict] = []        # типизированные карточки данных
    actions: list[dict] = []      # интерактивные кнопки
    suggestions: list[str] = []   # follow-up suggestions
```

**Frontend знает только эту структуру.** Любая новая фича = новый tool возвращающий ActionResult = автоматически работает.

### 9.7 Карточки (типы)

Фиксированная schema для каждого типа:

| Type | Что показывает |
|------|---------------|
| `product` | Один товар (артикул, бренд, цена, склад) |
| `rfq` | Запрос котировки (id, статус, описание, дата) |
| `order` | Заказ (номер, статус, сумма, customer) |
| `shipment` | Отгрузка (трекинг, прогресс по этапам) |
| `supplier` | Поставщик (название, KPI, рейтинг) |
| `comparison` | Сравнительная таблица (headers + rows) |
| `chart` | Простой график (заголовок + items) |
| `spec_results` | Многопозиционный результат поиска (KPI + таблица) |
| `supplier_top` | Топ-N поставщиков ранжированных |
| `pipeline_item` | Сделка в pipeline-вьюхе (Tier 0) |
| `payment` | Платёж (сумма, статус, кнопка оплатить) (Tier 0) |
| `claim` | Рекламация (статус, описание) |
| `document` | Документ (название, тип, ссылка) |

### 9.8 Permissions matrix

| Role | Allowed actions |
|------|----------------|
| `buyer` | search_parts, create_rfq, get_rfq_status, get_orders, get_order_detail, track_shipment, get_budget, get_analytics, compare_products, compare_suppliers, upload_parts_list, get_claims, create_claim, analyze_spec, top_suppliers, generate_proposal, open_url, get_pipeline |
| `seller` | search_parts, get_rfq_status, respond_rfq, get_orders, get_demand_report, upload_pricelist, get_analytics, analyze_spec, top_suppliers, create_rfq, generate_proposal, open_url |
| `operator_logist` | track_shipment, get_orders, get_sla_report, get_analytics |
| `operator_customs` | track_shipment, get_orders, get_analytics |
| `operator_payment` | get_orders, get_budget, get_analytics |
| `operator_manager` | search_parts, get_orders, get_rfq_status, get_analytics, get_demand_report, get_sla_report, compare_suppliers |
| `admin` | * (все) |

### 9.9 Knowledge accumulation (питает training data)

Каждая завершённая сделка обогащает 5 layers:

| Layer | Что копится | Источник |
|-------|-------------|----------|
| **Analogue map** | Part A = Part B cross-reference | Каталоги + manual confirmations |
| **Supplier ratings** | SLA, quality, delivery time | Каждая Order completion |
| **Logistics XP** | Реальные времена доставки по маршрутам | Shipment tracking |
| **Price history** | Тренды цен per part/supplier/period | Каждая RFQResponse |
| **Demand patterns** | Что/когда/где/кем ищут | Search queries + RFQs |

Эти данные доступны Claude через дополнительные tools (`get_lifecycle_data`, `get_price_trends`, `get_supplier_rating`).

---

## 10. Доменная модель

### 10.1 Основные сущности

#### User
```
id (UUID), email, password, first_name, last_name,
profile (UserProfile: role, company, phone)
```

#### Company
```
id, name, inn, kpp, address, bank_details, logo
```

#### Project
```
id (UUID), name, code, customer, owner (User),
tags, deadline, dot_color, created_at
```

#### Part (запчасть)
```
id (UUID), oem_number, title, description,
brand (FK Brand), category (FK Category),
price, currency, stock_qty, weight,
embedding (vector(384)),  # pgvector
specs (JSONB)
```

#### Brand, Category
```
id, name, slug
```

#### PartAnalogue
```
part_a (FK Part), part_b (FK Part),
confidence (0.0-1.0), source ("manual", "auto", "user_confirmed")
```

#### RFQ
```
id, created_by (User), customer_name, customer_email,
mode (auto/semi/manual_oem), urgency (standard/urgent/critical),
status (new/quoted/needs_review/cancelled),
notes, discount_percent, created_at, project (FK Project, nullable)
```

#### RFQItem
```
id, rfq (FK RFQ), query, quantity,
matched_part (FK Part, nullable),
state (new/auto_matched/needs_review/oem_manual),
confidence, decision_reason
```

#### RFQResponse
```
id, rfq_item (FK RFQItem), supplier (FK Company),
price, lead_time_days, conditions, created_at,
status (pending/accepted/rejected)
```

#### Order
```
id, rfq (FK RFQ), buyer (FK User), seller (FK Company),
status (12 states from pending → completed),
payment_status, payment_scheme (10/90 or 10/50/40),
total_amount, currency,
reserve_paid_at, mid_paid_at, final_paid_at,
created_at, updated_at
```

#### OrderItem
```
id, order (FK Order), part (FK Part), quantity, unit_price
```

#### OrderStatusHistory
```
id, order, from_status, to_status, changed_by, changed_at, note
```

#### OrderDocument
```
id, order, doc_type (gtd/cert_tr_ts/invoice/packing_list/...),
file, uploaded_by, uploaded_at
```

#### Claim (рекламация)
```
id, order (FK Order), opened_by (User),
description, status (open/investigating/approved/rejected/resolved),
amount_disputed, opened_at, resolved_at
```

#### Conversation
```
id (UUID), user (FK User), role, project (FK Project, nullable),
title, is_active, created_at, updated_at
```

#### Message
```
id, conversation, role (user/assistant/system),
content, cards (JSONB), actions (JSONB), context_refs (JSONB),
tokens_used, created_at
```

#### KnowledgeChunk (RAG)
```
id, source_type, title, content,
embedding (vector(384)), metadata (JSONB),
created_at, updated_at
```

### 10.2 Order status machine (12 состояний)

```
   pending
      ▼ (резерв оплачен)
   reserve_paid
      ▼ (продавец подтвердил)
   confirmed
      ▼
   in_production
      ▼
   ready_to_ship
      ▼
   transit_abroad
      ▼
   customs
      ▼
   transit_rf
      ▼
   issuing (на выдаче)
      ▼
   shipped
      ▼
   delivered
      ▼
   completed
```

Параллельные/исключения: `cancelled` (из любого состояния до `shipped`), `claim_pending` (после `delivered`).

### 10.3 Payment status

```
   awaiting_reserve
      ▼
   reserve_paid (10%)
      ▼
   mid_paid (50% — для scheme 10/50/40, опционально)
      ▼
   customs_paid (опционально)
      ▼
   paid (100%)
```

Отдельные состояния: `refund_pending`, `refunded`.

---

## 11. Что уже реализовано

### 11.1 Backend / модели

- ✅ User + UserProfile с ролями (buyer/seller/operator_*/admin)
- ✅ Company с реквизитами
- ✅ Part, Brand, Category, PartAnalogue
- ✅ Project + ProjectDocument
- ✅ RFQ, RFQItem, RFQResponse
- ✅ Order, OrderItem, OrderStatusHistory, OrderDocument
- ✅ Claim, ClaimStatus
- ✅ Conversation, Message
- ✅ KnowledgeChunk (RAG)

### 11.2 Backend / business logic

- ✅ RFQ flow (create from chat, auto-match, response)
- ✅ Order flow (12 статусов, status history)
- ✅ Payment scheme (10/90, 10/50/40, mark_paid actions)
- ✅ Document management (upload, download, generate PDF)
- ✅ Claim flow (open, status updates)
- ✅ Search (text + semantic via pgvector)

### 11.3 Operator-кабинеты (классический UI)

- ✅ `operator/manager` — orders, negotiations, analytics, dashboard
- ✅ `operator/logist` — documents, ports, dashboard, analytics
- ✅ `operator/customs` — documents, dashboard, analytics
- ✅ `operator/payments` — invoices, escrow, reconciliation, dashboard, analytics
- ✅ Кликабельные стат-блоки с фильтрами таблиц
- ✅ Тосты, активные подсветки, JS-фильтры

### 11.4 Chat-first слой

- ✅ `templates/chat/index.html` — главный чат
- ✅ `templates/chat/project.html` — страница проекта
- ✅ `templates/chat/rfq.html` — детали RFQ
- ✅ `templates/chat/proposal.html` — коммерческое предложение
- ✅ `static/js/chat-first.js` — WebSocket + streaming + cards + history
- ✅ `static/js/project-page.js`, `rfq-page.js`, `proposal-page.js`
- ✅ Sidebar с проектами и недавними чатами
- ✅ History persistence через localStorage
- ✅ Карточки: product, rfq, order, supplier, comparison, chart, spec_results, supplier_top, actions
- ✅ Редиректы со старого UI: `/rfq/<id>/` → `/chat/rfq/<id>/`, `/rfq/<id>/proposal/` → `/chat/proposal/<id>/`

### 11.5 AI слой

- ✅ `assistant/rag.py` — sync + streaming pipeline
- ✅ `assistant/actions.py` — 21+ tool с JSON schemas
- ✅ `assistant/fast_path.py` — 11 правил, 30мс latency
- ✅ `assistant/consumers.py` — WebSocket с lazy conversation creation
- ✅ `assistant/prompts.py` — system prompts по ролям
- ✅ Гибридное выполнение: fast-path → Claude tool-use → stub fallback
- ✅ Cost tracking: tokens_used в каждом Message

### 11.6 Multi-language

- ✅ Django i18n (RU/EN/ZH)
- ✅ Переключатель языка в UI
- ✅ Переводы основных страниц
- ✅ Locale persistence через cookie

### 11.7 Demo

- ✅ Аккаунты: `demo_buyer`, `demo_seller`, `demo_operator` / `demo12345`
- ✅ Сидер `manage.py seed --all-demo` создаёт проекты, RFQ, заказы, документы

### 11.8 Что НЕ работает / есть баги

- ⚠️ Эскроу не обязательный (можно создать Order без оплаты)
- ⚠️ Email уведомления не настроены
- ⚠️ Pipeline view отсутствует (30 одновременных RFQ невозможно отслеживать)
- ⚠️ Seller chat-first не реализован (seller использует классический seller_request_detail)
- ⚠️ Visual search не реализован (Tier 2)
- ⚠️ Email-inbound отсутствует (Tier 2)
- ⚠️ Telematics не подключён (Tier 3)
- ⚠️ Knowledge accumulation отсутствует (Tier 3)

---

## 12. Roadmap (приоритезированный)

### Tier 0: Critical (без этого продукт не работает)

**Sprint 1: Pipeline view (1 неделя)**
- Backend: `GET /api/assistant/pipeline/` — все RFQ + Orders сгруппированные по этапам
- Tool `get_pipeline` для Claude
- Fast-path правила: «мои сделки», «pipeline», «что в работе»
- Frontend: `templates/chat/pipeline.html` — Kanban с 8 столбцами
- Карточка `pipeline_item` с прогрессом

**Sprint 2: Обязательный эскроу (1 неделя)**
- Backend: Order не подтверждается без `reserve_paid`
- Интеграция ЮKassa: создание платежа, webhook, callback
- Tool `pay_reserve` — создаёт платёжную ссылку через ЮKassa
- Карточка `payment` в чате с кнопкой "Подтвердить оплату"
- Email/WS notification при изменении payment_status

**Sprint 3: Email уведомления (1 неделя)**
- Backend: SendGrid SMTP setup
- Шаблоны: новый RFQ (seller), КП получено (buyer), статус заказа изменён, дедлайн
- Очередь через Celery
- Settings: подписки пользователя на типы notifications

### Tier 1: Polish (UX без этого ущербный)

**Sprint 4: Track shipment в чате (1 неделя)**
- `templates/chat/order.html` — карточка заказа с прогрессом
- Tool `get_order_documents` — список документов с превью
- Hand-off mechanism: tool `escalate_to_operator` создаёт задачу logist'у/customs

**Sprint 5: Seller chat-first (1 неделя)**
- AI приходит к seller'у с RFQ как карточка с inline ответом
- Tool `respond_rfq_smart` парсит свободный текст seller'а в RFQResponse
- Pipeline view для seller'ов (свои RFQ)

**Sprint 6: Проактивные алерты (1 неделя)**
- Background scheduler (Celery beat): daily brief, deadlines, risks
- AI brief tool — собирает summary за период
- WebSocket push уведомлений
- Карточка `daily_brief`

### Tier 2: AI superpowers (3-4 недели)

**Sprint 7: Visual search (1-2 недели)**
- DINOv2 self-hosted на отдельном GPU instance
- Pipeline: image upload → embedding → pgvector search → top-N parts
- Tool `search_by_image`
- Карточка product с подсвеченным matched

**Sprint 8: OCR чертежей (1 неделя)**
- Claude 3.5 Sonnet Vision API
- Tool `extract_part_from_drawing`
- Workflow: upload PDF/image → AI извлекает номера и названия → создаёт RFQ

**Sprint 9: Email inbound (1-2 недели)**
- IMAP подключение к rfq@consolidator
- AI parser: извлекает RFQ items из email body + attachments (Excel, PDF)
- Auto-create RFQ + reply-to-sender с подтверждением
- Tool `import_rfq_from_email`

### Tier 3: Moat (4-6 недель)

**Sprint 10: Pricing intelligence (1-2 недели)**
- Daily scrape прайсов поставщиков (Celery beat)
- Анализ изменений → алерты buyer'ам
- Tool `get_price_trends`, `predict_price`
- Карточка `price_alert`

**Sprint 11: Negotiation Agent (1-2 недели)**
- Tool `negotiate_with_supplier(rfq_id, target_price)` — Claude пишет 5 поставщикам с benchmark
- Multi-day flow с rate limiting
- Карточка `negotiation_status`

**Sprint 12: Telematics integration (2-3 недели)**
- Komtrax / Caterpillar Equipment Manager API adapters
- Модель `Equipment` (машина клиента) + `EquipmentTelemetry`
- Predictive sales: за 2 недели до ТО — auto-RFQ
- Tool `get_fleet_status`, `predict_maintenance`

**Sprint 13: Reverse marketplace (1 неделя)**
- Seller posts available stock → AI matches с buyer's history
- Push notification buyer'у с предложением
- Карточка `stock_offer`

### Tier 4: Own LLM training pipeline

**Sprint A: Data collection infrastructure**
- Логирование каждого Claude-ответа в проде с metadata (контекст, action, user reaction)
- Operator-интерфейс для разметки: «правильный ответ / неправильный / нужно уточнить»
- Schema: `(user_query, context_snapshot, claude_response, operator_feedback, outcome)`
- Storage: PostgreSQL → еженедельный экспорт в JSONL для тренинга

**Sprint B: Training infrastructure setup**
- Аренда A100 (Lambda Labs / RunPod / Vast.ai) или покупка
- Setup axolotl/unsloth для LoRA fine-tuning
- Eval harness: набор тестовых запросов с golden answers
- CI для автоматического fine-tuning по cron (еженедельно)

**Sprint C: First fine-tuned model**
- Базовая модель: Qwen 2.5 14B (RU/ZH сильна для нашей вертикали)
- Dataset: первые 10K-50K labeled examples
- LoRA fine-tune: 3-5 epochs, eval на golden set
- Deploy через vLLM на собственный GPU-сервер
- A/B test: своя LLM vs Claude на 10% трафика

**Sprint D: Production cutover**
- Когда своя модель ≥ 90% качества Claude на eval-set
- Постепенный rollout: 10% → 50% → 100%
- Claude остаётся как fallback на edge cases
- Continuous training: re-fine-tune еженедельно на новых данных

### Tier 5: Scale & enterprise

- Compliance: ФНС/ЕГРЮЛ проверки контрагентов, SDN/санкции, dual-use
- Multi-currency + FX hedging
- ERP integrations (1С, SAP)
- Mobile native (React Native поверх API)
- White-label для крупных холдингов
- Admin dashboard с метриками платформы

---

## 13. UI/UX принципы

### 13.1 Дизайн-система (утверждено)

**Цветовая палитра:**

```
Gradient (фоновый):
  #a8b8d8 (lavender top, "Dawn")
  #cda9d6 (mid)
  #f0a59a (coral)
  #f4956a (deep orange bottom, "Sun")

Surfaces (карточки):
  rgba(255, 250, 240, 0.85) — warm cream
  backdrop-filter: blur(20px)

Text:
  #1A1A1A — основной (заголовки, цены)
  #6B6B6B — вторичный (subtitles)
  #888888 — muted (labels)

Status:
  Зелёный #16A34A — Found, цены, оплачено
  Оранжевый #D97706 — Analogue, в работе
  Красный #DC2626 — Not found, ошибки
```

**Шрифты:**
- Inter (основной) — все UI элементы
- Golos Text (заголовки) — крупные заголовки, цены, KPI

**Спейсинг:** 8px / 12px / 16px / 20px / 24px (Material-style 4px-grid).

**Border-radius:**
- Карточки: 14px
- Кнопки: 24px (pill)
- Бейджи: 5-6px

### 13.2 Компоненты UI

| Компонент | Использование |
|-----------|---------------|
| **Sidebar** | Слева, проекты + недавние чаты, скрывается на мобиле |
| **Topbar** | Burger + brand + avatar |
| **Chat thread** | Карточки сообщений (user / assistant / action) |
| **Input bar** | Sticky bottom, autosize, voice + file upload |
| **Cards** | Все типы из 9.5 |
| **Buttons (.act-btn)** | Под карточками, dark pill style |
| **Pipeline columns** | Kanban для buyer/seller (Tier 0) |

### 13.3 Mobile responsive

- < 768px: sidebar становится overlay
- < 480px: упрощённые карточки (скрываем неважные колонки)
- Минимальная клавиатурная высота input bar 60px

### 13.4 Accessibility

- Контраст текста ≥ 4.5:1 (WCAG AA)
- Все интерактивные элементы доступны с клавиатуры
- ARIA labels для иконок
- Скрин-ридеры могут читать карточки (semantic HTML)

### 13.5 Языки

- RU (default)
- EN
- ZH (для китайских поставщиков)

Переключатель в topbar. Сохраняется в cookie `django_language`.

---

## 14. Безопасность и compliance

### 14.1 Аутентификация

- Пароли: bcrypt (Django default)
- Минимальная длина: 8 символов
- Сессии: 14 дней
- Login throttling: 5 попыток за 5 минут per IP

### 14.2 Авторизация

- Permission decorators в DRF (`IsAuthenticated`, custom role checks)
- ROLE_ACTIONS matrix в `assistant/actions.py` — кто что может делать в чате
- Object-level permissions (buyer не видит чужие RFQ, seller — только относящиеся к нему)

### 14.3 CSRF

- Django CSRF middleware включён
- WebSocket — token-based authentication через query parameter

### 14.4 Защита данных

- HTTPS only в production (HSTS)
- Secure + HttpOnly cookies
- SQL injection: только через Django ORM (никаких raw queries)
- XSS: автоэскейпинг в шаблонах, `esc()` функция в JS

### 14.5 Платёжные данные

- НИКОГДА не храним номера карт
- ЮKassa hosted payment page (PCI-DSS compliance на их стороне)
- Webhook validation через signature

### 14.6 Compliance (РФ)

- 152-ФЗ (персональные данные): согласие при регистрации, possibility удалить аккаунт
- 54-ФЗ (онлайн-кассы): чеки через ЮKassa автоматически
- Валютный контроль: для международных платежей — через банк-партнёр (Tinkoff Business)
- Санкционные списки (Tier 3): SDN check для seller'ов

### 14.7 Backup и disaster recovery

- PostgreSQL daily dumps → S3 (encrypted, 30-day retention)
- Media files: replicated across 2 storage regions
- RTO (recovery time objective): 4 часа
- RPO (recovery point objective): 24 часа

### 14.8 Аудит

- Все действия пользователя логируются в `Message` (для AI) и `OrderStatusHistory` (для бизнеса)
- Admin-логи через Django admin + кастомный middleware
- Sensitive операции (изменение цены seller, отмена ордера) — отдельная audit table

---

## 15. Метрики и аналитика

### 15.1 Продуктовые метрики

| Метрика | Цель MVP | Цель Year 1 |
|---------|----------|-------------|
| Time to RFQ (от мысли до отправки) | < 60с | < 30с |
| Auto-match accuracy | 70% | 90% |
| % сделок closed in-platform (anti-bypass) | 60% | 85% |
| Avg ответ AI (fast-path) | 50мс | 30мс |
| Avg ответ LLM | 5с | 3с |
| Cost per AI request | $0.003 | $0.0005 |
| Bug rate в production | < 5/неделю | < 1/неделю |

### 15.2 Бизнес-метрики

| Метрика | 3 мес | 6 мес | 12 мес |
|---------|-------|-------|--------|
| Active buyers (>1 RFQ/мес) | 5 | 25 | 100 |
| Active sellers | 20 | 100 | 400 |
| GMV (оборот через платформу) | $500K | $5M | $30M |
| Revenue (5% от GMV) | $25K | $250K | $1.5M |
| NPS buyer | 30 | 50 | 60 |
| Cohort retention 3-mo | 40% | 60% | 75% |
| LTV / CAC | 5x | 20x | 50x |

### 15.3 AI метрики

| Метрика | Что показывает |
|---------|---------------|
| **Fast-path hit rate** | % запросов закрытых без LLM |
| **Tool call success rate** | % успешных tool executions (не error) |
| **Avg tool calls per query** | Сложность типичного запроса |
| **LLM cost per buyer/month** | Юнит-экономика AI |
| **LLM error rate** | API timeouts, invalid tool args |
| **Hallucination rate** | Manual review sample, target < 1% |

### 15.4 Operational метрики

| Метрика | Цель |
|---------|------|
| Uptime | 99.5% |
| p95 latency API | < 500мс |
| p95 latency WebSocket | < 100мс |
| Database connections | < 80% от max |
| Disk usage | < 70% |
| Email delivery rate | > 98% |

### 15.5 Dashboard для admin

- GMV график по дням/неделям/месяцам
- Active users (DAU, WAU, MAU)
- Funnel: registration → first RFQ → first order → repeat buyer
- Top suppliers / top buyers
- AI cost tracking per role
- Error rate по типам tools
- Pipeline distribution (сколько сделок на каком этапе)

---

## 16. Риски и митигации

### 16.1 Бизнес-риски

| Риск | Вероятность | Импакт | Митигация |
|------|-------------|--------|-----------|
| **Bypass off-platform** | Высокая | Критический | Обязательный эскроу + эксклюзивные цены (Tier 0) |
| **Отказ buyer'ов от чата** ("дайте таблицу") | Средняя | Высокий | Pipeline view (Kanban) + classic UI как escape hatch |
| **Seller'ы не пишут цены в систему** | Высокая | Высокий | Email-inbound RFQ + WhatsApp бот (Tier 2-3) |
| **Конкурент копирует AI-чат** | Средняя | Средний | Moat через end-to-end процесс + knowledge accumulation |
| **Регулирование санкций ужесточится** | Низкая | Высокий | Compliance check встроен, dual-use фильтр |
| **Падение спроса на запчасти (рецессия)** | Низкая | Высокий | Расширение в смежные вертикали (с/х техника, морская) |

### 16.2 Технические риски

| Риск | Вероятность | Импакт | Митигация |
|------|-------------|--------|-----------|
| **Anthropic API outage** | Средняя | Средний (Phase 1) → Низкий (после Phase 3) | Fast-path покрывает 95%; своя LLM в Phase 3 убирает зависимость |
| **Anthropic блокировка РФ** | Низкая | Средний (Phase 1) → Нулевой (Phase 3) | Своя LLM hosted on-premise — vendor-independent |
| **Качество своей LLM ниже Claude** | Средняя | Высокий | A/B testing перед cutover; Claude как fallback на edge cases |
| **Недостаточно training data** | Средняя | Высокий | Bootstrap через Claude + synthetic data + operator labels |
| **DDoS на VPS** | Низкая | Высокий | Cloudflare proxy + rate limiting |
| **Утечка данных** | Низкая | Критический | Шифрование at-rest, audit, security review pre-launch |
| **PostgreSQL deadlocks при росте** | Средняя | Средний | Connection pooling + read replicas (Tier 3) |
| **AI hallucination на критическом ответе** | Средняя | Высокий | Tool-use only (нет генерации цен), human-in-loop для крупных сделок |

### 16.3 Operational риски

| Риск | Митигация |
|------|-----------|
| Founder burnout | Roadmap с tier'ами, не делать всё сразу. Найм после 50 active buyers |
| Кадровый дефицит operator-команды | Автоматизация: AI закрывает 80% задач operator |
| Отзывы 1 негативный buyer'а в beta | Активный customer success, 1-on-1 calls с первыми 10 |
| Юр.споры с buyer/seller | Чёткий ToS, встроенный arbitration через эскроу |

---

## 17. Принципы разработки

### 17.1 Архитектурные принципы

1. **Один способ делать каждую вещь** — не два UI для buyer, не два API для одного действия, не две очереди задач.
2. **Каждый tool возвращает ActionResult** — не text, не HTML, не None. Всегда `ActionResult(text, cards, actions, suggestions)`.
3. **Каждое денежное действие проходит через эскроу** — никаких "давайте упростим, без эскроу пока".
4. **Каждая цифра в ответе AI — из БД** — если AI говорит "$4280", это `Part.objects.get(id=X).price`.
5. **Tier 0 → Tier 1 → Tier 2 → Tier 3** — не прыгаем по tier'ам. Tier 0 не закрыт = Tier 2 не делаем.
6. **Никаких form-submit в chat-first** — действие пользователя = текст в чат или клик кнопки в карточке.
7. **Operator UI ≠ Buyer UI ≠ Seller UI** — не пытаемся унифицировать. Они для разных задач.
8. **Migrations forward-only** — не делаем "вернуть как было", делаем новую миграцию которая исправляет.

### 17.2 Code style

- Python: PEP 8, type hints где имеет смысл (не для всего)
- JS: ES2020+, async/await, no jQuery
- SQL: Django ORM only (no raw queries except agreed-upon edge cases)
- HTML: semantic, accessible, no inline event handlers (use addEventListener)
- CSS: BEM-ish или утилитарные классы, никаких !important

### 17.3 Git workflow

- Main branch: `main` (production)
- Working: `chat-first` (current development)
- Feature branches: `feat/<name>` от `chat-first`, мержим в `chat-first`
- Hotfix: `fix/<name>` от `main`, мержим в обе

### 17.4 Code review

- Self-review для small changes (< 100 строк)
- Pre-commit checks: lint, типы, миграции apply
- Production deploy только после успешного staging deploy

### 17.5 Testing

**Покрытие приоритезировано:**
1. **Critical paths** (must have unit tests): action_executor, ActionResult, fast_path matching, RFQ creation, payment webhooks
2. **Important** (should have): tool handlers, model methods
3. **Nice to have**: UI components

Запуск: `python manage.py test`. CI запускает перед merge.

### 17.6 Deployment

```
Local dev → Push to chat-first → CI tests pass → Manual deploy script
                                                  ↓
                                          SSH → docker compose up
                                                  ↓
                                          Migrations + collectstatic
                                                  ↓
                                          Health check → done
```

---

## 18. Что НЕ делаем (явный антибэклог)

Это решения **закрытые** — обсуждение этих опций тратит время.

| ❌ | Почему |
|---|--------|
| **Next.js / React (полный SPA)** | Уже есть рабочий Django, переписать = 6 недель ноль фич |
| **TypeScript на фронте** | Чат состоит из карточек от backend, типизировать нечего |
| **Tailwind CSS** | Наш дизайн (gradients + glass) выглядит на Tailwind хуже |
| **Neo4j** | 50M связей PG держит, на одну БД меньше |
| **MongoDB / DynamoDB** | PG JSONB покрывает все NoSQL-сценарии |
| **GraphQL** | DRF REST — все знают, тулинг готовый |
| **Self-hosted LLM (Llama 70B)** | $2K/мес GPU не окупится до 100K запросов/день |
| **Несколько LLM-вендоров одновременно** | Усложняет dev, dedup ответов нужен. Один вендор → переключение по триггеру cost |
| **LangChain / LangGraph** | Наш agentic loop — 50 строк, проще и дебажится |
| **Микросервисы** | Один Django покрывает 100K users |
| **Kubernetes** | Один VPS + docker compose до 5K users |
| **CLIP** для visual search | DINOv2 + Claude Vision лучше, когда дойдём до Tier 2 |
| **Stripe** | Не работает с РФ юр.лицами в 2026 |
| **Mailgun / AWS SES** | SendGrid → Yandex дешевле и проще |
| **OAuth (Google/Apple sign-in)** | B2B, не нужно |
| **Mobile native приложения** | До Tier 4. Mobile web responsive покрывает 95% юзкейсов |
| **Custom auth provider (Auth0, Clerk)** | Django auth работает, $50/мес ни к чему |
| **Agile ceremonies (standups, retros, planning)** | Команда из 1-2 человек. Tier-based roadmap = весь процесс |
| **Слишком ранняя оптимизация** | Не оптимизируем то, чего нет (база, нагрузка, кейсы) |

---

## 19. Глоссарий

| Термин | Определение |
|--------|-------------|
| **RFQ** | Request for Quotation — запрос на котировку у поставщиков. Buyer отправляет, sellers отвечают КП |
| **КП** | Коммерческое Предложение — ответ seller'а с ценой, сроком, условиями. PDF + структурированные данные |
| **Order / PO** | Purchase Order — подтверждённая сделка с payment scheme и доставкой |
| **OEM** | Original Equipment Manufacturer — оригинальная запчасть от производителя техники (CAT, Komatsu) |
| **Aftermarket** | Запчасти не от OEM, обычно дешевле (Berco, ITM, китайские) |
| **GMV** | Gross Merchandise Value — общий оборот сделок через платформу |
| **TCO** | Total Cost of Ownership — полная стоимость владения (закупка + эксплуатация + обслуживание) |
| **SLA** | Service Level Agreement — обязательства по срокам, качеству |
| **ГТД** | Грузовая Таможенная Декларация |
| **ТР ТС** | Технический Регламент Таможенного Союза (ЕАЭС сертификация) |
| **EAC** | Eurasian Conformity (значок сертификации) |
| **Эскроу** | Условное депонирование — деньги buyer'а заморожены на счёте платформы до подтверждения поставки |
| **Pipeline** | Воронка сделок Kanban-стиля (просчёт → RFQ → КП → контракт → производство → ...) |
| **Fast-path** | Детерминированный (regex/правила) обработчик запросов без LLM |
| **Tool-use** | Способ работы LLM где модель вызывает функции (tools) вместо генерации текста |
| **ActionResult** | Стандартизированный ответ tool'а: text + cards + actions + suggestions |
| **Bypass** | Когда buyer и seller заключают сделку off-platform после получения котировок через нас |
| **Knowledge accumulation** | Layer данных, обогащающихся с каждой сделкой (analogues, ratings, prices, demand) |
| **Telematics** | API телеметрии техники (моточасы, наработка, GPS) от производителей (Komtrax, Equipment Manager) |
| **Predictive sales** | Подход когда AI предлагает закупку ДО того как клиент попросил, основываясь на данных парка |

---

## Приложение A: Repository structure

```
workspace-app/
├── consolidator_site/           # Django project settings
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py                  # Channels routing
│   └── wsgi.py
├── marketplace/                  # Core domain (Parts, Orders, RFQ, Operator)
│   ├── models.py
│   ├── views.py                  # Views для всех ролей
│   ├── urls.py
│   ├── api_urls.py               # /api/v1/...
│   └── templates/marketplace/    # Classic UI templates
├── assistant/                    # AI brain
│   ├── models.py                 # Conversation, Message, Project, KnowledgeChunk
│   ├── actions.py                # 21+ tools для AI
│   ├── fast_path.py              # Detereministic intent router
│   ├── rag.py                    # AI pipeline (sync + stream)
│   ├── consumers.py              # WebSocket
│   ├── prompts.py                # System prompts по ролям
│   ├── views.py                  # REST API endpoints
│   ├── urls.py
│   └── permissions.py
├── templates/
│   ├── chat/                     # Chat-first UI (Tier 0+)
│   │   ├── index.html
│   │   ├── project.html
│   │   ├── rfq.html
│   │   ├── proposal.html
│   │   └── pipeline.html (Tier 0, planned)
│   └── marketplace/              # Classic UI (operator, legacy)
├── static/
│   ├── js/
│   │   ├── chat-first.js         # Главный JS chat-first
│   │   ├── project-page.js
│   │   ├── rfq-page.js
│   │   ├── proposal-page.js
│   │   └── pipeline-page.js (Tier 0)
│   ├── css/
│   └── brand/                    # Logos, icons
├── docs/
│   ├── SPEC_FINAL.md             # Этот документ
│   └── (other docs)
├── locale/                       # i18n переводы
├── manage.py
├── requirements.txt
├── .env.example
└── docker-compose.yml
```

## Приложение B: Environment variables

```
# Django
DEBUG_MODE=False
SECRET_KEY=<random 50 chars>
ALLOWED_HOSTS=app.consolidator-parts.ru,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://app.consolidator-parts.ru

# Database
DATABASE_URL=postgresql://user:pass@localhost:5432/consolidator
REDIS_URL=redis://localhost:6379/0

# AI
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-20250514
TOGETHER_API_KEY=  # пусто пока, добавим в Tier 1+

# Payments
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=
YOOKASSA_WEBHOOK_SECRET=

# Email
SENDGRID_API_KEY=
EMAIL_FROM=noreply@consolidator-parts.ru

# Storage
USE_S3=False
AWS_S3_BUCKET=  # для S3-storage когда понадобится
AWS_S3_ENDPOINT=

# Monitoring
SENTRY_DSN=
```

## Приложение C: Demo data

После `python manage.py seed --all-demo` доступны:

- **demo_buyer** / demo12345 (роль buyer) — компания "Polyus Olimpiada"
- **demo_seller** / demo12345 (роль seller) — компания "Heavy Equipment Spares"
- **demo_operator** / demo12345 (роль operator_manager)
- **admin** / admin12345 (Django superuser)

Сид содержит:
- 3 проекта (Polyus Olimpiada, SUEK Borodino, EuroChem Kovdor)
- ~50 запчастей разных брендов (CAT, Komatsu, Berco, ITM, Xuzhou)
- 5 RFQ в разных статусах
- 3 заказа в разных payment_status
- 5+ чатов с примерами действий

---

**Конец документа.**

Версия 5.0 финальная. Изменения вносить только через PR с обоснованием в commit message. Каждое изменение обновляет дату и ставит подпись.

**Last edited:** 30 апреля 2026, Mansour + Claude Code session.
