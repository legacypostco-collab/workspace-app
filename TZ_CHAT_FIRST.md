# ТЗ: Consolidator Parts — Chat-First B2B Marketplace

> **Формат**: Техническое задание для Claude Code
> **Проект**: Consolidator Parts — B2B маркетплейс запчастей для тяжёлой техники
> **Стек**: Python 3.11+, Django 4.2+, PostgreSQL 15+ (pgvector), Redis, Django Channels
> **Подход**: Chat-First — весь интерфейс = один чат с AI, как у Claude/ChatGPT

---

## 1. Философия продукта

### 1.1 Главная идея
Consolidator Parts — это **чат**, а не сайт с каталогом. Пользователь открывает одну страницу, видит одно поле ввода и пишет что ему нужно. AI находит запчасти, сравнивает цены, создаёт заказы, отслеживает отгрузки — всё через диалог.

**Аналогия**: как Claude (claude.ai) для запасных частей тяжёлой техники.

### 1.2 Почему это лучше конкурентов
- **UDT.parts, Mirofish** — классические каталоги с фильтрами, 20+ страниц, требуют обучения
- **Consolidator Chat-First** — 1 экран, 0 обучения, любой язык, AI делает всю работу
- Ни один B2B маркетплейс запчастей в мире не работает через чат
- Барьер входа = 0: снабженец из любой страны начинает работать за 30 секунд

### 1.3 Три роли пользователей
| Роль | Кто это | Что делает через чат |
|------|---------|---------------------|
| **Buyer** | Покупатель (горнодобывающие, строительные компании) | Ищет запчасти, создаёт RFQ, отслеживает заказы, управляет бюджетом |
| **Seller** | Поставщик (производители, дистрибьюторы) | Отвечает на RFQ, управляет каталогом, видит аналитику спроса |
| **Operator** | Оператор платформы (4 подроли) | Логистика, таможня, платежи, продажи — каждый видит своё |

Подроли оператора: `logist`, `customs_broker`, `payment_agent`, `sales_manager`

### 1.4 Языки
Платформа работает на **любом языке автоматически**. AI определяет язык пользователя и отвечает на нём. Приоритетные: русский, английский, китайский, испанский, арабский.

Интерфейс (кнопки, подсказки) — мультиязычный через i18n. Но основной контент генерируется AI на лету.

---

## 2. Архитектура

### 2.1 Высокоуровневая схема

```
┌─────────────────────────────────────────────────┐
│                  FRONTEND                        │
│                                                  │
│  ┌────────────────────────────────────────────┐  │
│  │         Landing = Chat Screen              │  │
│  │                                            │  │
│  │  [Logo]  Consolidator                      │  │
│  │  [Subtitle] B2B Parts from Manufacturers   │  │
│  │                                            │  │
│  │  ┌─ Chat Messages Area ─────────────────┐  │  │
│  │  │ User: Нужны катки для Komatsu PC200  │  │  │
│  │  │ AI: Нашёл 3 варианта...              │  │  │
│  │  │   [Product Card] [Product Card]       │  │  │
│  │  │   [Button: Создать RFQ]              │  │  │
│  │  └──────────────────────────────────────┘  │  │
│  │                                            │  │
│  │  [Suggestion Chips]                        │  │
│  │  ┌──────────────────────────────────────┐  │  │
│  │  │ 📎  📷  [Input field]          🎤   │  │  │
│  │  └──────────────────────────────────────┘  │  │
│  │  [RU] [EN] [中文] [ES] [العربية]           │  │
│  └────────────────────────────────────────────┘  │
│                                                  │
│  Дополнительные панели (slide-out, не отдельные  │
│  страницы):                                      │
│  - История диалогов (sidebar слева)              │
│  - Профиль / настройки (modal)                   │
│  - Уведомления (dropdown)                        │
└──────────────────────┬──────────────────────────┘
                       │ WebSocket + REST API
                       ▼
┌─────────────────────────────────────────────────┐
│                   BACKEND                        │
│                                                  │
│  Django + Channels + DRF                         │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Chat API │  │ RAG      │  │ Action       │  │
│  │ WebSocket│  │ Pipeline │  │ Executor     │  │
│  │ Consumer │──│ embeddings│──│ (RFQ, Order, │  │
│  │          │  │ + search │  │  Shipment)   │  │
│  └──────────┘  └────┬─────┘  └──────────────┘  │
│                     │                            │
│  ┌──────────────────┴───────────────────────┐   │
│  │              Data Layer                   │   │
│  │  PostgreSQL + pgvector │ Redis │ Celery   │   │
│  └───────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

### 2.2 Ключевое отличие от обычного чат-бота
Это **не просто чат-бот поверх каталога**. Это полноценный маркетплейс, где чат — единственный интерфейс. AI не просто отвечает на вопросы — он **выполняет действия**:

- Создаёт RFQ
- Принимает/отклоняет предложения поставщиков
- Формирует заказы
- Показывает трекинг с картой
- Генерирует отчёты
- Отправляет уведомления

Каждое действие — это **кнопка или карточка** внутри сообщения AI, а не отдельная страница.

---

## 3. Frontend — Chat UI

### 3.1 Структура приложения

```
frontend/
├── src/
│   ├── App.jsx                    # Корневой компонент
│   ├── index.js
│   ├── styles/
│   │   └── global.css             # Минимальные глобальные стили
│   ├── components/
│   │   ├── ChatScreen.jsx         # Главный экран = чат
│   │   ├── MessageList.jsx        # Список сообщений
│   │   ├── MessageBubble.jsx      # Одно сообщение (user / assistant)
│   │   ├── InputBar.jsx           # Поле ввода + иконки
│   │   ├── SuggestionChips.jsx    # Подсказки-кнопки
│   │   ├── LanguageSelector.jsx   # Переключатель языка
│   │   ├── ConversationSidebar.jsx # Sidebar с историей диалогов
│   │   ├── UserMenu.jsx           # Профиль, настройки, выход
│   │   ├── NotificationBell.jsx   # Уведомления
│   │   └── cards/                 # Интерактивные карточки в сообщениях
│   │       ├── ProductCard.jsx    # Карточка товара
│   │       ├── RFQCard.jsx        # Карточка RFQ
│   │       ├── OrderCard.jsx      # Карточка заказа
│   │       ├── ShipmentCard.jsx   # Карточка отгрузки с трекингом
│   │       ├── SupplierCard.jsx   # Карточка поставщика с KPI
│   │       ├── ComparisonTable.jsx # Таблица сравнения
│   │       ├── ChartCard.jsx      # Мини-график (бюджет, аналитика)
│   │       ├── ActionButton.jsx   # Кнопка действия (Создать RFQ, Оплатить...)
│   │       └── FileUploadCard.jsx # Карточка загрузки файла
│   ├── hooks/
│   │   ├── useWebSocket.js        # WebSocket подключение
│   │   ├── useChat.js             # Логика чата (send, receive, stream)
│   │   └── useAuth.js             # Авторизация
│   ├── services/
│   │   ├── api.js                 # REST API клиент
│   │   ├── ws.js                  # WebSocket клиент
│   │   └── auth.js                # Auth сервис
│   ├── i18n/
│   │   ├── ru.json
│   │   ├── en.json
│   │   ├── zh.json
│   │   ├── es.json
│   │   └── ar.json
│   └── utils/
│       ├── markdown.js            # Рендер markdown в сообщениях
│       └── formatters.js          # Форматирование цен, дат, валют
```

### 3.2 Главный экран — `ChatScreen.jsx`

Это **единственная страница** приложения. Никаких роутов, никаких переходов.

```
┌──────────────────────────────────────────────────────┐
│ ☰ Consolidator                    🔔 2    👤 Mansour │  ← Header
├──────────────────────────────────────────────────────┤
│                                                      │
│            ┌─ Consolidator ─┐                        │
│            │ B2B Spare Parts │                        │  ← Logo (только
│            └─────────────────┘                        │     при пустом чате)
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │ 💬 Messages area (scrollable)                  │  │
│  │                                                │  │
│  │  [User bubble]  Нужны гусеничные цепи          │  │
│  │                 для CAT D6R, 10 штук            │  │
│  │                                                │  │
│  │  [AI bubble]    Нашёл 3 предложения:            │  │
│  │                 ┌──────────────────────┐        │  │
│  │                 │ ProductCard: CR5953  │        │  │
│  │                 │ Berco · $4,280/шт   │        │  │
│  │                 │ 12 шт · Италия      │        │  │
│  │                 └──────────────────────┘        │  │
│  │                 ┌──────────────────────┐        │  │
│  │                 │ ProductCard: CR5953  │        │  │
│  │                 │ ITM · $2,890/шт     │        │  │
│  │                 │ 20 шт · Китай       │        │  │
│  │                 └──────────────────────┘        │  │
│  │                                                │  │
│  │                 [Создать RFQ] [Сравнить]        │  │ ← ActionButtons
│  │                                                │  │
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  [Статус заказов] [Найти аналог] [Загрузить список]  │  ← SuggestionChips
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │ 📎 📷 │ Напиши что ищешь...              🎤  │  │  ← InputBar
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  [RU] [EN] [中文] [ES] [العربية]                      │  ← LanguageSelector
└──────────────────────────────────────────────────────┘
```

### 3.3 Карточки внутри сообщений (самое важное!)

AI не просто пишет текст — он возвращает **структурированные данные**, которые фронтенд рендерит как интерактивные карточки. Это ключевое отличие от обычного чат-бота.

#### Формат ответа AI (JSON внутри сообщения)

AI возвращает ответ в формате markdown + специальные блоки:

```
Нашёл 3 предложения от производителей для гусеничных цепей CAT D6R:

:::product
{
  "article": "CR5953",
  "brand": "Berco",
  "country": "Италия",
  "price": 4280,
  "currency": "USD",
  "quantity": 12,
  "delivery_days": 45,
  "condition": "new"
}
:::

:::product
{
  "article": "CR5953",
  "brand": "ITM",
  "country": "Китай",
  "price": 2890,
  "currency": "USD",
  "quantity": 20,
  "delivery_days": 60,
  "condition": "new"
}
:::

Berco дороже на 48%, но доставка быстрее на 15 дней. Рекомендую запросить обе котировки.

:::actions
[
  {"label": "Создать RFQ на обе позиции", "action": "create_rfq", "params": {"articles": ["CR5953"], "brands": ["Berco", "ITM"], "quantity": 10}},
  {"label": "Показать ещё варианты", "action": "search_more", "params": {"query": "track chain CAT D6R"}},
  {"label": "Сравнить в таблице", "action": "compare", "params": {"product_ids": ["p1", "p2"]}}
]
:::
```

#### Типы карточек

| Блок | Компонент | Когда используется |
|------|-----------|-------------------|
| `:::product` | `ProductCard.jsx` | Результаты поиска запчастей |
| `:::rfq` | `RFQCard.jsx` | Статус RFQ, предложения поставщиков |
| `:::order` | `OrderCard.jsx` | Информация о заказе, статус, оплата |
| `:::shipment` | `ShipmentCard.jsx` | Трекинг отгрузки, маршрут, ETA |
| `:::supplier` | `SupplierCard.jsx` | Карточка поставщика с KPI |
| `:::comparison` | `ComparisonTable.jsx` | Таблица сравнения товаров/поставщиков |
| `:::chart` | `ChartCard.jsx` | Мини-графики (бюджет, экономия, тренды) |
| `:::actions` | `ActionButton.jsx` | Кнопки действий |
| `:::file` | `FileUploadCard.jsx` | Загрузка/скачивание файлов |
| `:::table` | Таблица | Любые табличные данные |

#### Парсер сообщений — `MessageBubble.jsx`

```javascript
/**
 * Парсит ответ AI и рендерит микс из markdown и карточек.
 *
 * Алгоритм:
 * 1. Разбить текст по блокам :::type ... :::
 * 2. Markdown-части рендерить как текст
 * 3. Блоки с JSON рендерить как соответствующие карточки
 * 4. Кнопки actions привязать к WebSocket (отправляют action в чат)
 */

// Regex для парсинга блоков:
// /:::(product|rfq|order|shipment|supplier|comparison|chart|actions|file|table)\n([\s\S]*?)\n:::/g

// При нажатии на ActionButton:
// 1. Отправить в WebSocket: {"type": "action", "action": "create_rfq", "params": {...}}
// 2. Показать в чате как сообщение пользователя: "Создать RFQ на Berco CR5953 и ITM CR5953, 10 шт"
// 3. AI обработает action и вернёт результат (например, "RFQ #1234 создан")
```

### 3.4 InputBar — поле ввода

```
┌─────────────────────────────────────────────────┐
│ 📎  📷  │  Напиши что ищешь...            🎤  │
└─────────────────────────────────────────────────┘
  │    │                                      │
  │    │                                      └─ Голосовой ввод (Web Speech API)
  │    └─ Загрузить фото детали (для поиска по фото)
  └─ Прикрепить файл (Excel со списком артикулов, PDF чертёж)
```

Функции InputBar:
- **Текстовый ввод** — основной способ взаимодействия
- **Голосовой ввод** — Web Speech API, распознавание на любом языке
- **Загрузка файла** — Excel со списком артикулов → AI парсит и ищет все позиции
- **Загрузка фото** — фото детали или шильдика → AI распознаёт и ищет по каталогу
- **Enter** — отправить, **Shift+Enter** — новая строка
- **Автокомплит** — по мере ввода артикула подсказывает совпадения из каталога

### 3.5 SuggestionChips — подсказки

Контекстные подсказки зависят от роли и состояния:

**Новый пользователь (пустой чат):**
```
[Найти запчасть] [Загрузить список артикулов] [Как это работает?]
```

**Buyer после поиска:**
```
[Создать RFQ] [Найти аналог] [Сравнить с другими] [Показать историю цен]
```

**Buyer с активными заказами:**
```
[Статус заказов] [Трекинг отгрузки] [Мой бюджет] [Отчёт за месяц]
```

**Seller:**
```
[Новые RFQ] [Мои KPI] [Аналитика спроса] [Загрузить прайс-лист]
```

**Operator (logist):**
```
[Отгрузки в пути] [Нарушения SLA] [Статус таможни] [Контейнеры]
```

Подсказки загружаются с сервера: `GET /api/assistant/suggest/?role={role}&context={last_action}`

### 3.6 Sidebar — история диалогов

Открывается по кнопке ☰ в header. Slide-out слева.

```
┌─────────────────────┐
│ ☰ Диалоги       [+] │
├─────────────────────┤
│ 🔍 Поиск...         │
├─────────────────────┤
│ Сегодня             │
│ ● Гусеницы CAT D6R  │
│   Цепи для Komatsu   │
│                     │
│ Вчера               │
│   Статус заказа #89  │
│   Отчёт по бюджету   │
│                     │
│ На прошлой неделе    │
│   RFQ на подшипники  │
│   Сравнение Berco... │
└─────────────────────┘
```

### 3.7 Авторизация

**Страница входа** — минимальная:
```
┌──────────────────────────┐
│      Consolidator        │
│                          │
│  Email:    [          ]  │
│  Пароль:   [          ]  │
│                          │
│  [    Войти    ]         │
│                          │
│  Нет аккаунта? Написать  │
│  нам: info@consolidator  │
└──────────────────────────┘
```

**Регистрация** — НЕ через форму. Покупатель/поставщик пишет на email или в чат поддержки. Оператор создаёт аккаунт вручную. Это B2B — не нужна публичная регистрация.

После входа — сразу чат. Роль определяется из профиля автоматически.

### 3.8 Адаптивность

- **Desktop** (>1024px): чат по центру, max-width 800px, sidebar слева
- **Tablet** (768-1024px): чат на всю ширину, sidebar overlay
- **Mobile** (<768px): полноэкранный чат, sidebar = отдельный экран, chips горизонтальный скролл

### 3.9 Тема оформления

- **Тёмная тема** по умолчанию (как на скриншоте Claude)
- Светлая тема — переключатель в настройках
- Цвета: фон `#0a0a0a`, карточки `rgba(255,255,255,0.08)`, акцент `#6366f1` (indigo)
- Шрифт: системный (`-apple-system, system-ui, Inter`)
- Без градиентов, теней, декоративных элементов — чистый минимализм

---

## 4. Backend — Django Application

### 4.1 Структура приложения

```
assistant/
├── __init__.py
├── apps.py
├── models.py              # Conversation, Message, KnowledgeChunk, Feedback
├── serializers.py         # DRF serializers
├── views.py               # REST API endpoints
├── consumers.py           # WebSocket consumer (стриминг)
├── routing.py             # WebSocket URL routing
├── urls.py                # REST URL routing
├── rag.py                 # RAG pipeline — поиск контекста
├── actions.py             # ★ НОВОЕ: Исполнитель действий (create_rfq, track_order...)
├── card_renderer.py       # ★ НОВОЕ: Формирование карточек для фронтенда
├── embeddings.py          # Работа с vector embeddings
├── prompts.py             # Системные промпты по ролям
├── indexer.py             # Индексация контента в pgvector
├── permissions.py         # Проверка прав по роли
├── tasks.py               # Celery tasks (фоновая индексация)
├── signals.py             # Django signals для автоиндексации
├── admin.py               # Django Admin
├── tests/
│   ├── test_models.py
│   ├── test_rag.py
│   ├── test_actions.py
│   ├── test_consumers.py
│   └── test_indexer.py
└── management/
    └── commands/
        ├── index_catalog.py
        ├── index_documents.py
        └── reindex_all.py
```

### 4.2 Модели данных

```python
# assistant/models.py

import uuid
from django.db import models
from django.conf import settings
from pgvector.django import VectorField


class Conversation(models.Model):
    """Сессия диалога пользователя с AI."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversations'
    )
    role = models.CharField(
        max_length=20,
        choices=[
            ('buyer', 'Покупатель'),
            ('seller', 'Поставщик'),
            ('operator_logist', 'Логист'),
            ('operator_customs', 'Таможенный брокер'),
            ('operator_payment', 'Платёжный агент'),
            ('operator_manager', 'Менеджер по продажам'),
        ],
    )
    title = models.CharField(max_length=200, blank=True)
    language = models.CharField(max_length=5, default='ru')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['user', '-updated_at']),
        ]


class Message(models.Model):
    """Сообщение в диалоге."""

    class Role(models.TextChoices):
        USER = 'user', 'Пользователь'
        ASSISTANT = 'assistant', 'AI'
        SYSTEM = 'system', 'Системное'
        ACTION = 'action', 'Действие'  # ★ Когда пользователь нажимает кнопку

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages'
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField(help_text='Текст сообщения (markdown + card blocks)')
    
    # ★ Структурированные данные для карточек
    cards = models.JSONField(
        default=list, blank=True,
        help_text='Карточки: [{"type": "product", "data": {...}}, ...]'
    )
    actions = models.JSONField(
        default=list, blank=True,
        help_text='Кнопки действий: [{"label": "...", "action": "...", "params": {...}}]'
    )
    
    context_refs = models.JSONField(default=list, blank=True)
    tokens_used = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class KnowledgeChunk(models.Model):
    """Фрагмент знаний для RAG."""

    class SourceType(models.TextChoices):
        PRODUCT = 'product', 'Товар'
        BRAND = 'brand', 'Бренд'
        CATEGORY = 'category', 'Категория'
        ORDER = 'order', 'Заказ'
        RFQ = 'rfq', 'Запрос котировки'
        SHIPMENT = 'shipment', 'Отгрузка'
        DOCUMENT = 'document', 'Документ'
        REGULATION = 'regulation', 'Регламент'
        FAQ = 'faq', 'FAQ'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    source_id = models.CharField(max_length=100)
    title = models.CharField(max_length=300)
    content = models.TextField()
    embedding = VectorField(dimensions=1536)
    metadata = models.JSONField(default=dict)
    language = models.CharField(max_length=5, default='ru')
    access_roles = models.JSONField(
        default=list,
        help_text='Роли с доступом: ["buyer", "seller", ...]'
    )
    # ★ Привязка к конкретному пользователю/компании (для персональных данных)
    owner_id = models.CharField(
        max_length=100, blank=True, default='',
        help_text='ID владельца (user_id или company_id). Пусто = доступно всем с нужной ролью.'
    )
    is_active = models.BooleanField(default=True)
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['source_type', 'source_id']),
            models.Index(fields=['owner_id']),
        ]


class Feedback(models.Model):
    """Оценка ответа AI."""
    message = models.OneToOneField(Message, on_delete=models.CASCADE, related_name='feedback')
    rating = models.SmallIntegerField(choices=[(1, '👍'), (-1, '👎')])
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

### 4.3 Action Executor — исполнитель действий

Это **ключевой компонент** Chat-First подхода. Когда пользователь нажимает кнопку в чате (или пишет "создай RFQ"), AI определяет действие и вызывает Action Executor.

```python
# assistant/actions.py

"""
Исполнитель действий из чата.

Когда AI определяет, что пользователь хочет выполнить действие
(создать RFQ, посмотреть заказ, сгенерировать отчёт), он формирует
action-запрос, который обрабатывается здесь.

Каждый action возвращает:
1. Текстовый ответ для AI (что произошло)
2. Карточки для отображения в чате
3. Новые suggestion chips
"""


class ActionExecutor:
    """
    Маршрутизатор и исполнитель действий.

    Зарегистрированные действия:
    - search_parts      — поиск запчастей по параметрам
    - create_rfq        — создание запроса котировки
    - get_rfq_status    — статус RFQ
    - respond_rfq       — ответ поставщика на RFQ
    - get_orders        — список заказов пользователя
    - get_order_detail  — детали заказа
    - track_shipment    — трекинг отгрузки
    - get_budget        — бюджет и расходы
    - get_analytics     — аналитические отчёты
    - compare_products  — сравнение товаров
    - compare_suppliers — сравнение поставщиков
    - upload_pricelist  — загрузка прайс-листа поставщика
    - upload_parts_list — загрузка списка артикулов покупателя
    - get_claims        — рекламации
    - create_claim      — создать рекламацию
    - get_sla_report    — отчёт по SLA (оператор)
    - get_demand_report — отчёт по спросу (поставщик)
    """

    ACTIONS = {}  # Регистр действий

    @classmethod
    def register(cls, name):
        """Декоратор для регистрации действия."""
        def decorator(func):
            cls.ACTIONS[name] = func
            return func
        return decorator

    async def execute(self, action_name: str, params: dict, user, role: str):
        """
        Выполнить действие.

        Args:
            action_name: имя действия (например, "create_rfq")
            params: параметры от AI или от кнопки
            user: Django User
            role: роль пользователя

        Returns:
            ActionResult: {text, cards, actions, suggestions}
        """
        handler = self.ACTIONS.get(action_name)
        if not handler:
            return ActionResult(
                text=f"Действие '{action_name}' не найдено.",
                cards=[], actions=[], suggestions=[]
            )

        # Проверка прав
        if not self._check_permission(action_name, role):
            return ActionResult(
                text="У вас нет прав для этого действия.",
                cards=[], actions=[], suggestions=[]
            )

        return await handler(params=params, user=user, role=role)

    def _check_permission(self, action_name: str, role: str) -> bool:
        """Проверка прав на действие по роли."""
        ROLE_ACTIONS = {
            'buyer': [
                'search_parts', 'create_rfq', 'get_rfq_status', 'get_orders',
                'get_order_detail', 'track_shipment', 'get_budget', 'get_analytics',
                'compare_products', 'compare_suppliers', 'upload_parts_list',
                'get_claims', 'create_claim',
            ],
            'seller': [
                'search_parts', 'get_rfq_status', 'respond_rfq', 'get_orders',
                'get_demand_report', 'upload_pricelist', 'get_analytics',
            ],
            'operator_logist': [
                'track_shipment', 'get_orders', 'get_sla_report', 'get_analytics',
            ],
            'operator_customs': [
                'track_shipment', 'get_orders', 'get_analytics',
            ],
            'operator_payment': [
                'get_orders', 'get_budget', 'get_analytics',
            ],
            'operator_manager': [
                'search_parts', 'get_orders', 'get_rfq_status', 'get_analytics',
                'get_demand_report', 'get_sla_report', 'compare_suppliers',
            ],
        }
        allowed = ROLE_ACTIONS.get(role, [])
        return action_name in allowed


class ActionResult:
    """Результат выполнения действия."""
    def __init__(self, text: str, cards: list, actions: list, suggestions: list):
        self.text = text          # Текст для AI ответа
        self.cards = cards        # Карточки для отображения
        self.actions = actions    # Кнопки следующих действий
        self.suggestions = suggestions  # Новые подсказки


# --- Примеры действий ---

@ActionExecutor.register('search_parts')
async def search_parts(params, user, role):
    """
    Поиск запчастей.

    params: {
        "query": "гусеничная цепь CAT D6R",  // текстовый запрос
        "article": "CR5953",                   // или конкретный артикул
        "brand": "Berco",                      // фильтр по бренду
        "category": "undercarriage",           // фильтр по категории
        "limit": 5                             // кол-во результатов
    }
    """
    # АДАПТИРОВАТЬ: использовать вашу модель Product и логику поиска
    # from catalog.models import Product
    # products = Product.objects.filter(...)

    # Вернуть карточки товаров
    cards = []
    for product in products:
        cards.append({
            "type": "product",
            "data": {
                "id": str(product.id),
                "article": product.article,
                "brand": product.brand.name,
                "name": product.name,
                "price": float(product.price),
                "currency": product.currency,
                "quantity": product.quantity,
                "condition": product.condition,
                "country": product.brand.country,
                "delivery_days": product.estimated_delivery_days,
                "image_url": product.image.url if product.image else None,
            }
        })

    return ActionResult(
        text=f"Найдено {len(cards)} позиций.",
        cards=cards,
        actions=[
            {"label": "Создать RFQ", "action": "create_rfq",
             "params": {"product_ids": [c['data']['id'] for c in cards]}},
            {"label": "Сравнить", "action": "compare_products",
             "params": {"product_ids": [c['data']['id'] for c in cards]}},
        ],
        suggestions=["Показать аналоги", "Фильтр по бренду", "История цен"],
    )


@ActionExecutor.register('create_rfq')
async def create_rfq(params, user, role):
    """
    Создать RFQ.

    params: {
        "product_ids": ["uuid1", "uuid2"],    // товары
        "articles": ["CR5953"],               // или артикулы
        "brands": ["Berco", "ITM"],           // бренды
        "quantity": 10,                        // количество
        "delivery_location": "Якутск",         // куда доставить
        "notes": "Срочно, до конца месяца"     // примечание
    }
    """
    # АДАПТИРОВАТЬ: использовать вашу модель RFQ
    # from rfq.models import RFQ, RFQItem
    # rfq = RFQ.objects.create(buyer=user, ...)

    return ActionResult(
        text=f"RFQ #{rfq.number} создан. Отправлен {supplier_count} поставщикам.",
        cards=[{
            "type": "rfq",
            "data": {
                "id": str(rfq.id),
                "number": rfq.number,
                "status": "active",
                "items_count": len(params.get('product_ids', [])),
                "suppliers_count": supplier_count,
                "created_at": rfq.created_at.isoformat(),
            }
        }],
        actions=[
            {"label": "Статус RFQ", "action": "get_rfq_status",
             "params": {"rfq_id": str(rfq.id)}},
        ],
        suggestions=["Мои RFQ", "Создать ещё RFQ", "Статус заказов"],
    )


@ActionExecutor.register('track_shipment')
async def track_shipment(params, user, role):
    """
    Трекинг отгрузки.

    params: {
        "shipment_id": "uuid",          // ID отгрузки
        "order_id": "uuid",             // или ID заказа
        "container": "MSKU1234567"      // или номер контейнера
    }
    """
    # АДАПТИРОВАТЬ: использовать вашу модель Shipment

    return ActionResult(
        text=f"Отгрузка #{shipment.number}: {shipment.get_status_display()}",
        cards=[{
            "type": "shipment",
            "data": {
                "id": str(shipment.id),
                "number": shipment.number,
                "status": shipment.status,
                "container": shipment.container_number,
                "carrier": shipment.carrier.name,
                "origin": shipment.origin_city,
                "destination": shipment.destination_city,
                "eta": shipment.eta.isoformat(),
                "current_location": shipment.current_location,
                "timeline": [
                    {"date": "2025-01-15", "event": "Отгрузка со склада", "done": True},
                    {"date": "2025-01-20", "event": "Прибытие в порт", "done": True},
                    {"date": "2025-02-10", "event": "Таможня", "done": False},
                    {"date": "2025-02-25", "event": "Доставка", "done": False},
                ],
            }
        }],
        actions=[],
        suggestions=["Все мои отгрузки", "Связаться с логистом"],
    )
```

### 4.4 RAG Pipeline (обновлённый)

```python
# assistant/rag.py

"""
RAG Pipeline для Chat-First маркетплейса.

Отличие от обычного RAG:
1. AI не просто отвечает текстом — он решает, нужно ли выполнить действие
2. Если нужно действие → вызывает ActionExecutor → возвращает карточки
3. Если нужна информация → ищет в pgvector → отвечает текстом с контекстом
4. Всегда формирует suggestion chips для следующего шага
"""

import anthropic
from .embeddings import get_embedding, search_similar_chunks
from .prompts import get_system_prompt
from .actions import ActionExecutor, ActionResult
from .models import Conversation, Message


class RAGPipeline:

    MAX_HISTORY_MESSAGES = 20
    MAX_CONTEXT_CHUNKS = 5
    MIN_SIMILARITY_SCORE = 0.7
    MAX_RESPONSE_TOKENS = 2048

    def __init__(self):
        self.client = anthropic.Anthropic()
        self.action_executor = ActionExecutor()

    async def process_query(
        self,
        conversation: Conversation,
        user_message: str,
        action: dict = None,  # ★ Если пришло от кнопки, а не от текста
    ):
        """
        Обработка запроса. Возвращает генератор для стриминга.

        Два режима:
        1. action != None → пользователь нажал кнопку → выполнить действие
        2. action == None → текстовый запрос → RAG + AI

        Yields:
            dict: {"type": "stream|cards|actions|suggestions|done", "data": ...}
        """

        # --- Режим 1: Действие от кнопки ---
        if action:
            result = await self.action_executor.execute(
                action_name=action['action'],
                params=action.get('params', {}),
                user=conversation.user,
                role=conversation.role,
            )
            # Сохранить сообщение-действие
            await Message.objects.acreate(
                conversation=conversation,
                role=Message.Role.ACTION,
                content=action.get('label', action['action']),
            )
            # Сохранить ответ с карточками
            await Message.objects.acreate(
                conversation=conversation,
                role=Message.Role.ASSISTANT,
                content=result.text,
                cards=result.cards,
                actions=result.actions,
            )
            yield {"type": "text", "data": result.text}
            if result.cards:
                yield {"type": "cards", "data": result.cards}
            if result.actions:
                yield {"type": "actions", "data": result.actions}
            if result.suggestions:
                yield {"type": "suggestions", "data": result.suggestions}
            yield {"type": "done"}
            return

        # --- Режим 2: Текстовый запрос через AI ---

        # Сохранить сообщение пользователя
        await Message.objects.acreate(
            conversation=conversation,
            role=Message.Role.USER,
            content=user_message,
        )

        # Получить историю
        history = await self._get_history(conversation)

        # Поиск контекста
        context_chunks = await self._search_context(
            query=user_message,
            role=conversation.role,
            user_id=str(conversation.user_id),
        )

        # Системный промпт
        system_prompt = get_system_prompt(
            role=conversation.role,
            context_chunks=context_chunks,
        )

        # Вызов Claude API
        messages = history + [{"role": "user", "content": user_message}]
        full_response = ""

        async with self.client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=self.MAX_RESPONSE_TOKENS,
            system=system_prompt,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                full_response += text
                yield {"type": "stream", "data": text}

        # Парсить ответ AI на предмет карточек и действий
        cards, actions, clean_text = self._parse_ai_response(full_response)

        # Сохранить ответ
        await Message.objects.acreate(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=full_response,
            cards=cards,
            actions=actions,
            context_refs=[{
                "type": c.source_type, "id": str(c.source_id),
                "title": c.title, "score": c.similarity_score,
            } for c in context_chunks],
        )

        if cards:
            yield {"type": "cards", "data": cards}
        if actions:
            yield {"type": "actions", "data": actions}
        yield {"type": "done"}

    async def _search_context(self, query, role, user_id):
        """Поиск контекста с фильтрацией по роли И по владельцу."""
        query_embedding = await get_embedding(query)
        return await search_similar_chunks(
            embedding=query_embedding,
            role=role,
            owner_id=user_id,  # ★ Персональные данные только свои
            limit=self.MAX_CONTEXT_CHUNKS,
            min_score=self.MIN_SIMILARITY_SCORE,
        )

    async def _get_history(self, conversation):
        messages = []
        async for msg in (
            conversation.messages
            .filter(role__in=['user', 'assistant'])
            .order_by('-created_at')[:self.MAX_HISTORY_MESSAGES]
        ):
            messages.append({"role": msg.role, "content": msg.content})
        messages.reverse()
        return messages

    def _parse_ai_response(self, text):
        """
        Парсить ответ AI: извлечь блоки :::type ... ::: в карточки.

        Returns:
            (cards: list, actions: list, clean_text: str)
        """
        import re
        cards = []
        actions = []
        clean_text = text

        # Извлечь карточки :::product, :::rfq, etc.
        card_pattern = r':::(product|rfq|order|shipment|supplier|comparison|chart|table)\n(.*?)\n:::'
        for match in re.finditer(card_pattern, text, re.DOTALL):
            card_type = match.group(1)
            try:
                import json
                card_data = json.loads(match.group(2))
                cards.append({"type": card_type, "data": card_data})
            except json.JSONDecodeError:
                pass
            clean_text = clean_text.replace(match.group(0), '')

        # Извлечь действия :::actions
        action_pattern = r':::actions\n(.*?)\n:::'
        for match in re.finditer(action_pattern, text, re.DOTALL):
            try:
                import json
                actions = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
            clean_text = clean_text.replace(match.group(0), '')

        return cards, actions, clean_text.strip()
```

### 4.5 WebSocket Consumer (обновлённый)

```python
# assistant/consumers.py

import json
from channels.generic.websocket import AsyncWebSocketConsumer
from .rag import RAGPipeline
from .models import Conversation


class ChatConsumer(AsyncWebSocketConsumer):
    """
    WebSocket consumer для Chat-First интерфейса.

    Протокол (клиент → сервер):
    - {"type": "message", "content": "текст"}           — текстовое сообщение
    - {"type": "action", "action": "create_rfq", "params": {...}} — действие от кнопки
    - {"type": "upload", "file_type": "excel", "data": "base64..."} — файл

    Протокол (сервер → клиент):
    - {"type": "stream", "data": "фрагмент текста"}     — стриминг ответа
    - {"type": "cards", "data": [{type, data}]}          — карточки
    - {"type": "actions", "data": [{label, action}]}     — кнопки действий
    - {"type": "suggestions", "data": ["подсказка1"]}    — suggestion chips
    - {"type": "done"}                                    — конец ответа
    - {"type": "error", "message": "текст"}              — ошибка
    """

    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            await self.close()
            return

        self.pipeline = RAGPipeline()
        self.conversation_id = self.scope["url_route"]["kwargs"].get("conversation_id")

        if self.conversation_id:
            try:
                self.conversation = await Conversation.objects.aget(
                    id=self.conversation_id, user=self.user, is_active=True
                )
            except Conversation.DoesNotExist:
                await self.close()
                return
        else:
            role = await self._get_user_role()
            self.conversation = await Conversation.objects.acreate(
                user=self.user, role=role
            )

        await self.accept()
        await self.send(json.dumps({
            "type": "connected",
            "conversation_id": str(self.conversation.id),
            "role": self.conversation.role,
        }))

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get("type")

            if msg_type == "message":
                # Текстовый запрос
                content = data.get("content", "").strip()
                if not content:
                    return
                async for chunk in self.pipeline.process_query(
                    conversation=self.conversation,
                    user_message=content,
                ):
                    await self.send(json.dumps(chunk))

            elif msg_type == "action":
                # Действие от кнопки
                async for chunk in self.pipeline.process_query(
                    conversation=self.conversation,
                    user_message="",
                    action=data,
                ):
                    await self.send(json.dumps(chunk))

        except Exception as e:
            await self.send(json.dumps({
                "type": "error",
                "message": str(e),
            }))

    async def _get_user_role(self):
        from channels.db import database_sync_to_async
        @database_sync_to_async
        def get_role():
            profile = getattr(self.user, 'profile', None)
            return getattr(profile, 'role', 'buyer') if profile else 'buyer'
        return await get_role()
```

### 4.6 Системные промпты (обновлённые для Chat-First)

```python
# assistant/prompts.py

BASE_SYSTEM_PROMPT = """Ты — AI-ассистент B2B маркетплейса Consolidator Parts. 
Платформа — чат-первый интерфейс для оптовых закупок запчастей тяжёлой техники напрямую от производителей.

ВАЖНО — формат ответа:
1. Пиши кратко и по делу. Это чат, не статья.
2. Когда показываешь товары — используй блоки :::product с JSON внутри.
3. Когда предлагаешь действия — используй блоки :::actions с JSON массивом.
4. Всегда предлагай следующий шаг (кнопки действий).
5. Отвечай на языке пользователя. Определяй язык автоматически.
6. Никогда не выдумывай данные. Только из контекста.
7. Валюты: USD, CNY, RUB, EUR.

Формат карточек:

:::product
{"article": "...", "brand": "...", "name": "...", "price": 0, "currency": "USD", "quantity": 0, "condition": "new", "country": "...", "delivery_days": 0}
:::

:::actions
[{"label": "Текст кнопки", "action": "action_name", "params": {"key": "value"}}]
:::

Доступные действия для кнопок:
- search_parts — поиск запчастей
- create_rfq — создать запрос котировки
- get_rfq_status — статус RFQ
- get_orders — список заказов
- get_order_detail — детали заказа
- track_shipment — трекинг отгрузки
- get_budget — бюджет
- get_analytics — аналитика
- compare_products — сравнение товаров
- compare_suppliers — сравнение поставщиков
"""

ROLE_PROMPTS = {
    'buyer': """Ты помогаешь покупателю запчастей.
Можешь: искать детали, создавать RFQ, показывать заказы и отгрузки, сравнивать цены, анализировать бюджет.
Если покупатель ищет деталь — сразу покажи карточки товаров и кнопку "Создать RFQ".
Если деталь не найдена — предложи: поиск аналога, создать RFQ вслепую, загрузить фото детали.""",

    'seller': """Ты помогаешь поставщику запчастей.
Можешь: показывать входящие RFQ, помогать с ценообразованием, анализировать спрос, управлять каталогом.
Приоритет — помочь быстрее ответить на RFQ. Показывай сколько RFQ ждут ответа.""",

    'operator_logist': """Ты помогаешь логисту.
Можешь: показывать отгрузки в пути, нарушения SLA, статус таможни, контейнеры.
Начинай с дашборда: сколько отгрузок, сколько нарушений, что требует внимания.""",

    'operator_customs': """Ты помогаешь таможенному брокеру.
Можешь: показывать грузы на таможне, помогать с классификацией, отслеживать документы.""",

    'operator_payment': """Ты помогаешь платёжному агенту.
Можешь: показывать неоплаченные инвойсы, статусы платежей, курсы валют.""",

    'operator_manager': """Ты помогаешь менеджеру по продажам.
Можешь: показывать воронку RFQ→заказ, выручку, новых клиентов, неактивных клиентов.""",
}


def get_system_prompt(role: str, context_chunks: list) -> str:
    prompt = BASE_SYSTEM_PROMPT + "\n\n" + ROLE_PROMPTS.get(role, "")

    if context_chunks:
        prompt += "\n\n--- КОНТЕКСТ ---\n"
        for i, chunk in enumerate(context_chunks, 1):
            prompt += f"\n[{i}. {chunk.get_source_type_display()}: {chunk.title}]\n"
            prompt += chunk.content + "\n"
            if chunk.metadata:
                meta = ", ".join(f"{k}: {v}" for k, v in chunk.metadata.items())
                prompt += f"({meta})\n"
        prompt += "\n--- КОНЕЦ КОНТЕКСТА ---\n"

    return prompt
```

### 4.7 REST API

```python
# assistant/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'conversations', views.ConversationViewSet, basename='conversation')

urlpatterns = [
    path('', include(router.urls)),
    path('chat/', views.ChatView.as_view(), name='chat'),
    path('feedback/', views.FeedbackView.as_view(), name='feedback'),
    path('suggest/', views.SuggestView.as_view(), name='suggest'),
]

# В основном urls.py:
# path('api/assistant/', include('assistant.urls')),

# WebSocket routing (assistant/routing.py):
# re_path(r'ws/chat/(?P<conversation_id>[0-9a-f-]+)?/?$', ChatConsumer.as_asgi())
```

### 4.8 Embeddings и индексация

```python
# assistant/embeddings.py

import httpx
from django.conf import settings
from pgvector.django import CosineDistance
from .models import KnowledgeChunk

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536


async def get_embedding(text: str) -> list[float]:
    """Получить vector embedding через OpenAI API."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={"model": EMBEDDING_MODEL, "input": text[:8000], "dimensions": EMBEDDING_DIMENSIONS},
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]


async def search_similar_chunks(
    embedding: list[float],
    role: str,
    owner_id: str = None,
    limit: int = 5,
    min_score: float = 0.7,
) -> list[KnowledgeChunk]:
    """Семантический поиск с фильтрацией по роли и владельцу."""
    from django.db.models import Q

    queryset = (
        KnowledgeChunk.objects
        .filter(is_active=True)
        .filter(access_roles__contains=[role])
        .annotate(distance=CosineDistance('embedding', embedding))
        .filter(distance__lt=(1 - min_score))
        .order_by('distance')
    )

    # Персональные данные: показать только свои + общедоступные
    if owner_id:
        queryset = queryset.filter(
            Q(owner_id='') | Q(owner_id=owner_id)
        )

    chunks = []
    async for chunk in queryset[:limit]:
        chunk.similarity_score = round(1 - chunk.distance, 3)
        chunks.append(chunk)
    return chunks
```

```python
# assistant/indexer.py

"""
Индексатор контента для vector store.

ВАЖНО: Все классы содержат комментарии # АДАПТИРОВАТЬ
в местах, где нужно подставить реальные модели проекта.
"""

import logging
from .models import KnowledgeChunk
from .embeddings import get_embedding

logger = logging.getLogger(__name__)


class CatalogIndexer:
    """Индексация товарного каталога."""

    def index_product(self, product):
        content = self._product_to_text(product)
        embedding = get_embedding(content)  # sync

        chunk, created = KnowledgeChunk.objects.update_or_create(
            source_type=KnowledgeChunk.SourceType.PRODUCT,
            source_id=str(product.id),
            defaults={
                'title': f"{product.article} — {product.name}",
                'content': content,
                'embedding': embedding,
                'metadata': {
                    'article': product.article,
                    'brand': getattr(product.brand, 'name', ''),
                    'category': getattr(product.category, 'name', ''),
                    'price': str(product.price) if product.price else None,
                    'currency': product.currency,
                    'in_stock': product.quantity > 0,
                },
                'language': 'ru',
                'access_roles': ['buyer', 'seller', 'operator_manager'],
                'owner_id': '',  # Каталог доступен всем
            }
        )
        return chunk

    def index_all_products(self, batch_size=100):
        # АДАПТИРОВАТЬ: from catalog.models import Product
        from catalog.models import Product

        total = Product.objects.count()
        indexed = 0
        for i in range(0, total, batch_size):
            products = Product.objects.select_related('brand', 'category')[i:i+batch_size]
            for product in products:
                try:
                    self.index_product(product)
                    indexed += 1
                except Exception as e:
                    logger.error(f"Ошибка: {product.article}: {e}")
            logger.info(f"Проиндексировано {indexed}/{total}")
        return indexed

    def _product_to_text(self, product):
        parts = [f"{product.article} {getattr(product.brand, 'name', '')} {product.name}"]
        if product.category:
            parts.append(f"Категория: {product.category.name}")
        if product.price:
            parts.append(f"Цена: {product.price} {product.currency}")
        if hasattr(product, 'quantity'):
            parts.append(f"Наличие: {product.quantity} шт")
        if product.weight:
            parts.append(f"Вес: {product.weight} кг")
        if hasattr(product, 'compatibility') and product.compatibility:
            parts.append(f"Применимость: {product.compatibility}")
        if product.description:
            parts.append(f"Описание: {product.description[:500]}")
        return "\n".join(parts)


class OrderIndexer:
    """Индексация заказов (персональные данные)."""

    def index_order(self, order):
        content = (
            f"Заказ #{order.number}\n"
            f"Дата: {order.created_at.strftime('%d.%m.%Y')}\n"
            f"Статус: {order.get_status_display()}\n"
            f"Сумма: {order.total} {order.currency}\n"
        )
        embedding = get_embedding(content)

        KnowledgeChunk.objects.update_or_create(
            source_type=KnowledgeChunk.SourceType.ORDER,
            source_id=str(order.id),
            defaults={
                'title': f"Заказ #{order.number}",
                'content': content,
                'embedding': embedding,
                'metadata': {'status': order.status, 'total': str(order.total)},
                'language': 'ru',
                'access_roles': ['buyer', 'seller', 'operator_logist', 'operator_manager', 'operator_payment'],
                'owner_id': str(order.buyer_id),  # ★ Только владелец видит
            }
        )
```

### 4.9 Настройки Django

```python
# settings.py — добавить:

INSTALLED_APPS += ['channels', 'assistant']

ANTHROPIC_API_KEY = env('ANTHROPIC_API_KEY')
OPENAI_API_KEY = env('OPENAI_API_KEY')

ASGI_APPLICATION = 'config.asgi.application'
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {'hosts': [env('REDIS_URL', default='redis://localhost:6379/0')]},
    },
}

# .env:
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# REDIS_URL=redis://localhost:6379/0
```

### 4.10 Зависимости

```
# requirements.txt — добавить:
anthropic>=0.25.0
django-pgvector>=0.1.0
pgvector>=0.2.0
channels>=4.0.0
channels-redis>=4.1.0
httpx>=0.25.0
```

### 4.11 Миграции pgvector

```sql
-- Выполнить в PostgreSQL:
CREATE EXTENSION IF NOT EXISTS vector;

-- После первичной индексации (>1000 записей):
CREATE INDEX assistant_knowledgechunk_embedding_idx
ON assistant_knowledgechunk
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);
```

---

## 5. Celery Tasks и авто-индексация

```python
# assistant/tasks.py
from celery import shared_task
from .indexer import CatalogIndexer, OrderIndexer

@shared_task
def index_product_task(product_id):
    from catalog.models import Product  # АДАПТИРОВАТЬ
    product = Product.objects.get(id=product_id)
    CatalogIndexer().index_product(product)

@shared_task
def index_order_task(order_id):
    from orders.models import Order  # АДАПТИРОВАТЬ
    order = Order.objects.get(id=order_id)
    OrderIndexer().index_order(order)

@shared_task
def reindex_all_task():
    CatalogIndexer().index_all_products()
```

```python
# assistant/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from .tasks import index_product_task, index_order_task

# АДАПТИРОВАТЬ: раскомментировать и указать реальные модели
# @receiver(post_save, sender='catalog.Product')
# def auto_index_product(sender, instance, **kwargs):
#     index_product_task.delay(instance.id)

# @receiver(post_save, sender='orders.Order')
# def auto_index_order(sender, instance, **kwargs):
#     index_order_task.delay(instance.id)
```

---

## 6. Контрольный список реализации

### Фаза 1: Chat MVP (3 недели)

**Backend:**
- [ ] `CREATE EXTENSION vector;` в PostgreSQL
- [ ] Добавить зависимости в requirements.txt
- [ ] `python manage.py startapp assistant`
- [ ] Модели: Conversation, Message, KnowledgeChunk, Feedback → миграции
- [ ] `embeddings.py` — получение и поиск эмбеддингов
- [ ] `prompts.py` — системные промпты по ролям
- [ ] `rag.py` — RAG pipeline с парсингом карточек
- [ ] `actions.py` — ActionExecutor + search_parts, create_rfq
- [ ] `consumers.py` — WebSocket consumer
- [ ] `views.py` — REST API (conversations, chat, feedback, suggest)
- [ ] Django Channels + Redis настройка
- [ ] `indexer.py` + management command `index_catalog`
- [ ] Проиндексировать каталог
- [ ] Тесты для RAG и actions

**Frontend:**
- [ ] ChatScreen — главный (и единственный) экран
- [ ] MessageBubble с парсером карточек (:::product, :::actions)
- [ ] ProductCard, RFQCard, ActionButton компоненты
- [ ] InputBar с текстовым вводом
- [ ] WebSocket подключение + стриминг
- [ ] SuggestionChips
- [ ] LanguageSelector
- [ ] Тёмная тема
- [ ] Адаптивность (mobile/tablet/desktop)
- [ ] Страница входа (минимальная)

### Фаза 2: Полный функционал (4 недели)

- [ ] OrderCard, ShipmentCard, SupplierCard, ComparisonTable, ChartCard
- [ ] Все actions: track_shipment, get_orders, get_budget, compare_*
- [ ] ConversationSidebar — история диалогов
- [ ] NotificationBell — уведомления
- [ ] Загрузка файлов (Excel → парсинг артикулов, PDF)
- [ ] Голосовой ввод (Web Speech API)
- [ ] Индексация заказов, RFQ, отгрузок
- [ ] Авто-индексация через signals + Celery
- [ ] Персональная фильтрация (мои заказы, мои RFQ)

### Фаза 3: AI-автоматизация (4 недели)

- [ ] Проактивные уведомления ("У вас 3 просроченных RFQ")
- [ ] Мультимодальность: фото детали → поиск по каталогу
- [ ] Аналитические отчёты по запросу с графиками
- [ ] Email-интеграция (уведомления о новых RFQ/ответах)
- [ ] Автогенерация заголовков диалогов через AI

---

## 7. Важные замечания

### 7.1 Маркеры адаптации
В коде есть комментарии `# АДАПТИРОВАТЬ` — места, где нужно заменить примеры на реальные модели проекта. Найти все:
```bash
grep -rn "АДАПТИРОВАТЬ" assistant/
```

### 7.2 Безопасность
- API ключи только через переменные окружения
- Фильтрация по `access_roles` обязательна для каждого запроса
- Фильтрация по `owner_id` для персональных данных (заказы, бюджет)
- Rate limiting: 50 запросов/час на пользователя
- Мониторинг расхода токенов Claude API

### 7.3 Архитектурный принцип
**Не делать отдельных страниц.** Всё — через чат. Если нужна новая функция — это новый action + новая карточка, а не новая страница. Каталог, RFQ, заказы, отгрузки, аналитика — всё отображается как карточки внутри сообщений.
