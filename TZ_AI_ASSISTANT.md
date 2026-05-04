# Техническое задание: AI-ассистент для Consolidator Parts

## 1. Общее описание

### 1.1 Продукт
**Consolidator Parts** — B2B маркетплейс запасных частей для тяжёлой техники. Платформа связывает покупателей (горнодобывающие, строительные компании), поставщиков (производители и дистрибьюторы запчастей) и операторов (логисты, таможенные брокеры, платёжные агенты, менеджеры по продажам).

### 1.2 Задача
Реализовать AI-ассистента ("Второй мозг") — интеллектуального помощника, встроенного в платформу. Ассистент использует RAG (Retrieval-Augmented Generation) для ответов на вопросы пользователей с опорой на реальные данные каталога, историю заказов, документацию и внутренние регламенты.

### 1.3 Стек
- **Backend**: Python 3.11+, Django 4.2+, Django REST Framework
- **Database**: PostgreSQL 15+ с расширением pgvector
- **Real-time**: Django Channels (WebSocket)
- **AI**: Claude API (Anthropic) — модель claude-sonnet-4-20250514
- **Embeddings**: Voyage AI (voyage-3) или OpenAI (text-embedding-3-small), размерность 1536
- **Очереди**: Celery + Redis (для фоновой индексации)
- **Фронтенд**: существующий фронтенд проекта (добавить виджет чата)

### 1.4 Три роли пользователей
| Роль | Описание | Что видит ассистент |
|------|----------|-------------------|
| **Buyer** | Покупатель запчастей | Каталог, цены, наличие, свои заказы, RFQ, отгрузки, рекламации, бюджет |
| **Seller** | Поставщик запчастей | Входящие RFQ, свои товары, заказы, аналитику спроса, KPI |
| **Operator** | Оператор платформы (4 подроли: logist, customs_broker, payment_agent, sales_manager) | Все данные по своей зоне ответственности, SLA, метрики, задачи |

---

## 2. Архитектура

### 2.1 Высокоуровневая схема
```
Пользователь
    │
    ▼
[Chat Widget] ──WebSocket──▶ [Django Channels Consumer]
                                      │
                                      ▼
                              [RAG Pipeline]
                              ┌───────────────┐
                              │ 1. Parse query │
                              │ 2. Detect role │
                              │ 3. Embed query │
                              │ 4. Vector search│
                              │ 5. Build prompt│
                              │ 6. Call Claude │
                              │ 7. Stream resp │
                              └───────────────┘
                                 │         │
                          ┌──────┘         └──────┐
                          ▼                       ▼
                   [pgvector DB]          [Claude API]
                   KnowledgeChunk          Streaming
                   + metadata              response
```

### 2.2 Структура Django-приложения
```
assistant/
├── __init__.py
├── apps.py
├── models.py           # Conversation, Message, KnowledgeChunk, Feedback
├── serializers.py      # DRF serializers
├── views.py            # REST API endpoints
├── consumers.py        # WebSocket consumer для стриминга
├── routing.py          # WebSocket URL routing
├── urls.py             # REST URL routing
├── rag.py              # RAG pipeline — основная логика
├── embeddings.py       # Работа с embeddings (создание, поиск)
├── prompts.py          # Системные промпты для каждой роли
├── indexer.py          # Индексация контента в vector store
├── permissions.py      # Проверка прав доступа по роли
├── tasks.py            # Celery tasks (фоновая индексация)
├── signals.py          # Django signals для автоиндексации
├── admin.py            # Django Admin для управления
├── tests/
│   ├── __init__.py
│   ├── test_models.py
│   ├── test_rag.py
│   ├── test_views.py
│   ├── test_consumers.py
│   └── test_indexer.py
└── management/
    └── commands/
        ├── index_catalog.py      # python manage.py index_catalog
        ├── index_documents.py    # python manage.py index_documents
        └── reindex_all.py        # python manage.py reindex_all
```

---

## 3. Модели данных

### 3.1 Conversation
```python
class Conversation(models.Model):
    """Сессия диалога пользователя с ассистентом."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='assistant_conversations'
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
        help_text='Роль пользователя на момент создания диалога'
    )
    title = models.CharField(
        max_length=200,
        blank=True,
        help_text='Автогенерируемый заголовок на основе первого сообщения'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['user', '-updated_at']),
        ]
```

### 3.2 Message
```python
class Message(models.Model):
    """Одно сообщение в диалоге."""

    class Role(models.TextChoices):
        USER = 'user', 'Пользователь'
        ASSISTANT = 'assistant', 'Ассистент'
        SYSTEM = 'system', 'Системное'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField()
    context_refs = models.JSONField(
        default=list,
        blank=True,
        help_text='Ссылки на источники: [{type, id, title, relevance_score}]'
    )
    tokens_used = models.IntegerField(default=0, help_text='Токены Claude API')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
```

### 3.3 KnowledgeChunk
```python
from pgvector.django import VectorField

class KnowledgeChunk(models.Model):
    """Фрагмент знаний, проиндексированный для RAG."""

    class SourceType(models.TextChoices):
        PRODUCT = 'product', 'Товар из каталога'
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
    source_id = models.CharField(
        max_length=100,
        help_text='ID исходного объекта (product_id, order_id и т.д.)'
    )
    title = models.CharField(max_length=300)
    content = models.TextField(help_text='Текстовое содержимое чанка')
    embedding = VectorField(
        dimensions=1536,
        help_text='Vector embedding для семантического поиска'
    )
    metadata = models.JSONField(
        default=dict,
        help_text='Дополнительные данные: brand, category, price, currency и т.д.'
    )
    language = models.CharField(
        max_length=5,
        default='ru',
        choices=[('ru', 'Русский'), ('en', 'English'), ('zh', '中文')]
    )
    access_roles = models.JSONField(
        default=list,
        help_text='Роли с доступом: ["buyer", "seller", "operator_logist"]'
    )
    is_active = models.BooleanField(default=True)
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['source_type', 'source_id']),
            models.Index(fields=['language']),
        ]
        # pgvector index создаётся миграцией:
        # CREATE INDEX ON assistant_knowledgechunk
        # USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### 3.4 Feedback
```python
class Feedback(models.Model):
    """Оценка ответа ассистента пользователем."""

    message = models.OneToOneField(
        Message,
        on_delete=models.CASCADE,
        related_name='feedback'
    )
    rating = models.SmallIntegerField(
        choices=[(1, '👍'), (-1, '👎')],
    )
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

---

## 4. RAG Pipeline

### 4.1 Файл `rag.py` — основная логика

```python
# assistant/rag.py

import anthropic
from .embeddings import get_embedding, search_similar_chunks
from .prompts import get_system_prompt
from .models import Conversation, Message, KnowledgeChunk


class RAGPipeline:
    """
    Основной pipeline обработки запросов к AI-ассистенту.

    Порядок:
    1. Определить роль пользователя
    2. Получить историю диалога (последние N сообщений)
    3. Создать embedding запроса
    4. Найти релевантные чанки в pgvector (top-K, с фильтрацией по роли)
    5. Собрать системный промпт (роль + контекст + правила)
    6. Вызвать Claude API со стримингом
    7. Сохранить ответ и ссылки на источники
    """

    MAX_HISTORY_MESSAGES = 20       # Последних сообщений из диалога
    MAX_CONTEXT_CHUNKS = 5          # Чанков контекста из vector store
    MIN_SIMILARITY_SCORE = 0.7      # Минимальный cosine similarity
    MAX_RESPONSE_TOKENS = 2048      # Лимит токенов ответа

    def __init__(self):
        self.client = anthropic.Anthropic()  # API key из env ANTHROPIC_API_KEY

    async def process_query(
        self,
        conversation: Conversation,
        user_message: str,
    ) -> AsyncGenerator[str, None]:
        """
        Обработка запроса пользователя. Возвращает генератор для стриминга.

        Args:
            conversation: текущая сессия диалога
            user_message: текст вопроса пользователя

        Yields:
            str: фрагменты ответа для стриминга через WebSocket
        """

        # 1. Сохранить сообщение пользователя
        user_msg = await Message.objects.acreate(
            conversation=conversation,
            role=Message.Role.USER,
            content=user_message,
        )

        # 2. Получить историю
        history = await self._get_history(conversation)

        # 3. Найти релевантный контекст
        context_chunks = await self._search_context(
            query=user_message,
            role=conversation.role,
            language=self._detect_language(user_message),
        )

        # 4. Собрать промпт
        system_prompt = get_system_prompt(
            role=conversation.role,
            context_chunks=context_chunks,
        )

        # 5. Вызвать Claude API со стримингом
        messages = history + [{"role": "user", "content": user_message}]
        full_response = ""
        context_refs = []

        async with self.client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=self.MAX_RESPONSE_TOKENS,
            system=system_prompt,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                full_response += text
                yield text  # Стриминг в WebSocket

        # 6. Сформировать ссылки на источники
        for chunk in context_chunks:
            context_refs.append({
                "type": chunk.source_type,
                "id": str(chunk.source_id),
                "title": chunk.title,
                "score": chunk.similarity_score,
            })

        # 7. Сохранить ответ
        await Message.objects.acreate(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=full_response,
            context_refs=context_refs,
            tokens_used=stream.usage.output_tokens if hasattr(stream, 'usage') else 0,
        )

        # 8. Обновить заголовок диалога если первое сообщение
        if not conversation.title:
            conversation.title = user_message[:100]
            await conversation.asave(update_fields=['title'])

    async def _search_context(self, query: str, role: str, language: str):
        """Поиск релевантного контекста через pgvector."""

        query_embedding = await get_embedding(query)

        chunks = await search_similar_chunks(
            embedding=query_embedding,
            role=role,
            language=language,
            limit=self.MAX_CONTEXT_CHUNKS,
            min_score=self.MIN_SIMILARITY_SCORE,
        )
        return chunks

    async def _get_history(self, conversation: Conversation) -> list[dict]:
        """Получить последние N сообщений диалога в формате Claude API."""

        messages = []
        async for msg in (
            conversation.messages
            .filter(role__in=['user', 'assistant'])
            .order_by('-created_at')[:self.MAX_HISTORY_MESSAGES]
        ):
            messages.append({
                "role": msg.role,
                "content": msg.content,
            })
        messages.reverse()
        return messages

    def _detect_language(self, text: str) -> str:
        """Простое определение языка по символам."""
        if any('一' <= c <= '鿿' for c in text):
            return 'zh'
        if any('Ѐ' <= c <= 'ӿ' for c in text):
            return 'ru'
        return 'en'
```

### 4.2 Файл `embeddings.py`

```python
# assistant/embeddings.py

import httpx
from django.conf import settings
from pgvector.django import CosineDistance
from .models import KnowledgeChunk


EMBEDDING_MODEL = "text-embedding-3-small"  # или voyage-3
EMBEDDING_DIMENSIONS = 1536


async def get_embedding(text: str) -> list[float]:
    """
    Получить vector embedding для текста.

    Использует OpenAI API (text-embedding-3-small).
    Можно заменить на Voyage AI или другой провайдер.

    Args:
        text: текст для эмбеддинга (до 8191 токенов)

    Returns:
        list[float]: вектор размерности 1536
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": EMBEDDING_MODEL,
                "input": text[:8000],  # Обрезка до лимита
                "dimensions": EMBEDDING_DIMENSIONS,
            },
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]


async def search_similar_chunks(
    embedding: list[float],
    role: str,
    language: str = None,
    limit: int = 5,
    min_score: float = 0.7,
) -> list[KnowledgeChunk]:
    """
    Семантический поиск по pgvector.

    Ищет top-K чанков с фильтрацией по:
    - роли пользователя (access_roles содержит role)
    - языку (если указан)
    - минимальному cosine similarity

    Args:
        embedding: вектор запроса
        role: роль пользователя для фильтрации доступа
        language: язык запроса (ru/en/zh)
        limit: максимум результатов
        min_score: минимальный порог схожести

    Returns:
        list[KnowledgeChunk]: отсортированные по релевантности чанки
    """
    queryset = (
        KnowledgeChunk.objects
        .filter(is_active=True)
        .filter(access_roles__contains=[role])
        .annotate(distance=CosineDistance('embedding', embedding))
        .filter(distance__lt=(1 - min_score))  # cosine distance < 0.3 = similarity > 0.7
        .order_by('distance')
    )

    if language:
        queryset = queryset.filter(language=language)

    chunks = []
    async for chunk in queryset[:limit]:
        chunk.similarity_score = round(1 - chunk.distance, 3)
        chunks.append(chunk)

    return chunks
```

### 4.3 Файл `prompts.py`

```python
# assistant/prompts.py

from .models import KnowledgeChunk


# Базовый системный промпт для всех ролей
BASE_SYSTEM_PROMPT = """Ты — AI-ассистент платформы Consolidator Parts, B2B маркетплейса запасных частей для тяжёлой техники.

Правила:
1. Отвечай на основе предоставленного контекста. Если информации нет — честно скажи об этом.
2. Всегда указывай конкретные данные: артикулы, цены, сроки, статусы.
3. Поддерживай три языка: русский, английский, китайский. Отвечай на языке вопроса.
4. Будь кратким и по делу. Формат ответа — структурированный, с числами.
5. Если пользователь спрашивает о товаре — предлагай создать RFQ.
6. Никогда не выдумывай артикулы, цены или наличие. Только реальные данные.
7. Валюты: USD, CNY, RUB, EUR — указывай как в контексте.
"""

ROLE_PROMPTS = {
    'buyer': """Ты помогаешь покупателю запчастей для тяжёлой техники.

Ты можешь:
- Искать запчасти по артикулу, названию, бренду, категории
- Показывать цены, наличие, сроки поставки
- Помогать с RFQ (запросами котировок) — объяснять статусы, сравнивать предложения
- Информировать о статусе заказов и отгрузок
- Показывать аналитику: бюджет, экономия, рейтинги поставщиков
- Помогать с рекламациями: как подать, сроки рассмотрения

Если покупатель ищет товар, которого нет в контексте — предложи создать RFQ.
При сравнении товаров — формируй таблицу (артикул, бренд, цена, наличие, срок).
""",

    'seller': """Ты помогаешь поставщику запасных частей.

Ты можешь:
- Показывать входящие RFQ и рекомендовать ценообразование
- Анализировать спрос: какие запчасти ищут чаще всего
- Информировать о KPI: скорость ответа, качество, SLA
- Помогать с управлением каталогом товаров
- Показывать статистику заказов и выручки
- Предупреждать о просроченных RFQ и требующих внимания заказах

Приоритет — помочь поставщику быстрее отвечать на RFQ и увеличить конверсию.
""",

    'operator_logist': """Ты помогаешь логисту платформы.

Ты можешь:
- Показывать статус отгрузок: в пути, на таможне, доставлены
- Отслеживать SLA по срокам доставки
- Информировать о нарушениях SLA и задачах, требующих действий
- Помогать с маршрутами и выбором перевозчиков
- Показывать контейнеры, трекинг-номера, ETA
""",

    'operator_customs': """Ты помогаешь таможенному брокеру.

Ты можешь:
- Информировать о грузах на таможне
- Помогать с классификацией ТН ВЭД
- Показывать требуемые документы для растаможки
- Отслеживать статусы таможенного оформления
""",

    'operator_payment': """Ты помогаешь платёжному агенту.

Ты можешь:
- Показывать неоплаченные инвойсы и сроки оплаты
- Информировать о статусе платежей (ожидание, в обработке, оплачено)
- Помогать с валютными конвертациями (USD, CNY, RUB, EUR)
- Предупреждать о просроченных платежах
""",

    'operator_manager': """Ты помогаешь менеджеру по продажам.

Ты можешь:
- Показывать воронку продаж: конверсия RFQ → заказ
- Информировать о новых покупателях и поставщиках
- Анализировать выручку и объёмы по периодам
- Предупреждать о неактивных клиентах
- Показывать рейтинги и KPI команды
""",
}


def get_system_prompt(role: str, context_chunks: list[KnowledgeChunk]) -> str:
    """
    Собрать полный системный промпт для Claude API.

    Args:
        role: роль пользователя
        context_chunks: найденные релевантные чанки из vector store

    Returns:
        str: полный системный промпт с контекстом
    """
    # Базовый промпт + промпт роли
    prompt = BASE_SYSTEM_PROMPT + "\n\n" + ROLE_PROMPTS.get(role, "")

    # Добавить контекст из RAG
    if context_chunks:
        prompt += "\n\n--- КОНТЕКСТ ИЗ БАЗЫ ДАННЫХ ---\n"
        for i, chunk in enumerate(context_chunks, 1):
            prompt += f"\n[Источник {i}: {chunk.get_source_type_display()} — {chunk.title}]\n"
            prompt += chunk.content + "\n"

            # Метаданные
            if chunk.metadata:
                meta_parts = []
                for key, value in chunk.metadata.items():
                    meta_parts.append(f"{key}: {value}")
                if meta_parts:
                    prompt += f"Метаданные: {', '.join(meta_parts)}\n"

        prompt += "\n--- КОНЕЦ КОНТЕКСТА ---\n"
        prompt += "\nОтвечай строго на основе предоставленного контекста."
    else:
        prompt += "\n\nКонтекст не найден. Если не можешь ответить на вопрос без данных — так и скажи."

    return prompt
```

---

## 5. WebSocket Consumer

### 5.1 Файл `consumers.py`

```python
# assistant/consumers.py

import json
from channels.generic.websocket import AsyncWebSocketConsumer
from channels.db import database_sync_to_async
from .rag import RAGPipeline
from .models import Conversation


class AssistantConsumer(AsyncWebSocketConsumer):
    """
    WebSocket consumer для real-time чата с AI-ассистентом.

    Протокол:
    - Клиент → Сервер: {"type": "message", "content": "текст вопроса"}
    - Сервер → Клиент: {"type": "stream", "content": "фрагмент ответа"}
    - Сервер → Клиент: {"type": "done", "context_refs": [...]}
    - Сервер → Клиент: {"type": "error", "message": "текст ошибки"}
    """

    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            await self.close()
            return

        self.conversation_id = self.scope["url_route"]["kwargs"].get("conversation_id")
        self.pipeline = RAGPipeline()

        # Получить или создать диалог
        if self.conversation_id:
            try:
                self.conversation = await Conversation.objects.aget(
                    id=self.conversation_id,
                    user=self.user,
                    is_active=True,
                )
            except Conversation.DoesNotExist:
                await self.close()
                return
        else:
            self.conversation = await Conversation.objects.acreate(
                user=self.user,
                role=await self._get_user_role(),
            )

        await self.accept()

        # Отправить ID диалога клиенту
        await self.send(text_data=json.dumps({
            "type": "connected",
            "conversation_id": str(self.conversation.id),
        }))

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            if data.get("type") != "message" or not data.get("content", "").strip():
                return

            user_message = data["content"].strip()

            # Стриминг ответа
            async for chunk in self.pipeline.process_query(
                conversation=self.conversation,
                user_message=user_message,
            ):
                await self.send(text_data=json.dumps({
                    "type": "stream",
                    "content": chunk,
                }))

            # Завершение
            await self.send(text_data=json.dumps({
                "type": "done",
            }))

        except Exception as e:
            await self.send(text_data=json.dumps({
                "type": "error",
                "message": f"Ошибка обработки: {str(e)}",
            }))

    async def _get_user_role(self) -> str:
        """Определить роль пользователя из профиля."""
        # Адаптировать под вашу модель User/Profile
        profile = await database_sync_to_async(
            lambda: getattr(self.user, 'profile', None)
        )()
        if profile:
            return getattr(profile, 'role', 'buyer')
        return 'buyer'
```

### 5.2 Файл `routing.py`

```python
# assistant/routing.py

from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(
        r'ws/assistant/(?P<conversation_id>[0-9a-f-]+)?/?$',
        consumers.AssistantConsumer.as_asgi()
    ),
]
```

---

## 6. REST API

### 6.1 Файл `views.py`

```python
# assistant/views.py

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Conversation, Message, Feedback
from .serializers import (
    ConversationSerializer,
    MessageSerializer,
    FeedbackSerializer,
    ChatRequestSerializer,
)
from .rag import RAGPipeline


class ConversationViewSet(viewsets.ModelViewSet):
    """
    API для управления диалогами.

    GET    /api/assistant/conversations/          — список диалогов
    POST   /api/assistant/conversations/          — создать новый диалог
    GET    /api/assistant/conversations/{id}/      — детали диалога с сообщениями
    DELETE /api/assistant/conversations/{id}/      — удалить (деактивировать) диалог
    """
    serializer_class = ConversationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Conversation.objects.filter(
            user=self.request.user,
            is_active=True,
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])


class ChatView(APIView):
    """
    POST /api/assistant/chat/

    Синхронный эндпоинт для чата (без стриминга).
    Для стриминга использовать WebSocket.

    Body: {"conversation_id": "uuid", "message": "текст"}
    Response: {"response": "текст", "context_refs": [...]}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Получить или создать диалог
        conv_id = serializer.validated_data.get('conversation_id')
        if conv_id:
            conversation = Conversation.objects.get(
                id=conv_id, user=request.user, is_active=True
            )
        else:
            conversation = Conversation.objects.create(
                user=request.user,
                role=request.user.profile.role,
            )

        # Обработать запрос (синхронная версия)
        pipeline = RAGPipeline()
        response_text, context_refs = pipeline.process_query_sync(
            conversation=conversation,
            user_message=serializer.validated_data['message'],
        )

        return Response({
            "conversation_id": str(conversation.id),
            "response": response_text,
            "context_refs": context_refs,
        })


class FeedbackView(APIView):
    """
    POST /api/assistant/feedback/

    Body: {"message_id": "uuid", "rating": 1, "comment": "текст"}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message = Message.objects.get(
            id=serializer.validated_data['message_id'],
            conversation__user=request.user,
        )

        feedback, created = Feedback.objects.update_or_create(
            message=message,
            defaults={
                'rating': serializer.validated_data['rating'],
                'comment': serializer.validated_data.get('comment', ''),
            }
        )

        return Response({"status": "ok"}, status=status.HTTP_201_CREATED)


class SuggestView(APIView):
    """
    GET /api/assistant/suggest/?role=buyer

    Возвращает подсказки вопросов в зависимости от роли.
    """
    permission_classes = [IsAuthenticated]

    SUGGESTIONS = {
        'buyer': [
            "Какие гусеничные цепи есть для CAT D6?",
            "Покажи статус моих заказов",
            "Сравни поставщиков по SLA",
            "Сколько осталось бюджета на Q2?",
        ],
        'seller': [
            "Покажи новые RFQ за сегодня",
            "Какие запчасти ищут чаще всего?",
            "Мой KPI за этот месяц",
            "Просроченные RFQ",
        ],
        'operator_logist': [
            "Какие отгрузки сейчас в пути?",
            "Есть нарушения SLA?",
            "Статус контейнера MSKU1234567",
        ],
        'operator_manager': [
            "Конверсия RFQ → заказ за месяц",
            "Топ покупатели по выручке",
            "Неактивные клиенты",
        ],
    }

    def get(self, request):
        role = request.query_params.get('role', 'buyer')
        return Response({
            "suggestions": self.SUGGESTIONS.get(role, self.SUGGESTIONS['buyer'])
        })
```

### 6.2 Файл `urls.py`

```python
# assistant/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'conversations', views.ConversationViewSet, basename='conversation')

urlpatterns = [
    path('', include(router.urls)),
    path('chat/', views.ChatView.as_view(), name='assistant-chat'),
    path('feedback/', views.FeedbackView.as_view(), name='assistant-feedback'),
    path('suggest/', views.SuggestView.as_view(), name='assistant-suggest'),
]

# В основном urls.py проекта добавить:
# path('api/assistant/', include('assistant.urls')),
```

---

## 7. Индексация контента

### 7.1 Файл `indexer.py`

```python
# assistant/indexer.py

import logging
from django.db import transaction
from .models import KnowledgeChunk
from .embeddings import get_embedding

logger = logging.getLogger(__name__)


class CatalogIndexer:
    """
    Индексация товарного каталога в vector store.

    Формат чанка для товара:
    "[Артикул] [Бренд] [Название]
     Категория: [категория]
     Состояние: [новый/восстановленный]
     Цена: [цена] [валюта]
     Наличие: [кол-во] шт, склад [город]
     Вес: [вес] кг
     Применимость: [модели техники]"
    """

    def index_product(self, product) -> KnowledgeChunk:
        """
        Индексировать один товар.

        Args:
            product: объект модели Product (адаптировать под вашу модель)

        Returns:
            KnowledgeChunk: созданный/обновлённый чанк
        """
        # Сформировать текстовое представление
        content = self._product_to_text(product)

        # Получить embedding
        embedding = get_embedding(content)  # sync версия

        # Создать/обновить чанк
        chunk, created = KnowledgeChunk.objects.update_or_create(
            source_type=KnowledgeChunk.SourceType.PRODUCT,
            source_id=str(product.id),
            defaults={
                'title': f"{product.article} — {product.name}",
                'content': content,
                'embedding': embedding,
                'metadata': {
                    'article': product.article,
                    'brand': product.brand.name if product.brand else None,
                    'category': product.category.name if product.category else None,
                    'price': str(product.price) if product.price else None,
                    'currency': product.currency,
                    'in_stock': product.quantity > 0,
                },
                'language': 'ru',  # или определять из product
                'access_roles': ['buyer', 'seller', 'operator_manager'],
            }
        )

        action = "Создан" if created else "Обновлён"
        logger.info(f"{action} чанк для товара {product.article}")
        return chunk

    def index_all_products(self, batch_size=100):
        """
        Индексация всего каталога батчами.

        ВАЖНО: Адаптировать Product.objects.all() под вашу модель товаров.
        """
        from catalog.models import Product  # <-- АДАПТИРОВАТЬ ПОД ВАШУ МОДЕЛЬ

        total = Product.objects.count()
        indexed = 0

        for i in range(0, total, batch_size):
            products = Product.objects.select_related(
                'brand', 'category'
            )[i:i + batch_size]

            for product in products:
                try:
                    self.index_product(product)
                    indexed += 1
                except Exception as e:
                    logger.error(f"Ошибка индексации {product.article}: {e}")

            logger.info(f"Проиндексировано {indexed}/{total}")

        return indexed

    def _product_to_text(self, product) -> str:
        """Конвертировать товар в текст для embedding."""
        parts = [
            f"{product.article} {product.brand.name if product.brand else ''} {product.name}",
        ]
        if product.category:
            parts.append(f"Категория: {product.category.name}")
        if product.condition:
            parts.append(f"Состояние: {product.condition}")
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
    """Индексация заказов — адаптировать под модель Order."""

    def index_order(self, order) -> KnowledgeChunk:
        content = (
            f"Заказ #{order.number}\n"
            f"Дата: {order.created_at.strftime('%d.%m.%Y')}\n"
            f"Статус: {order.get_status_display()}\n"
            f"Сумма: {order.total} {order.currency}\n"
            f"Поставщик: {order.supplier.company_name}\n"
            f"Покупатель: {order.buyer.company_name}\n"
            f"Позиции: {order.items.count()} шт\n"
        )
        for item in order.items.all():
            content += f"  - {item.article} x{item.quantity} = {item.total}\n"

        embedding = get_embedding(content)

        # Заказ доступен покупателю, поставщику и операторам
        access_roles = ['buyer', 'seller', 'operator_logist', 'operator_manager', 'operator_payment']

        chunk, _ = KnowledgeChunk.objects.update_or_create(
            source_type=KnowledgeChunk.SourceType.ORDER,
            source_id=str(order.id),
            defaults={
                'title': f"Заказ #{order.number} — {order.get_status_display()}",
                'content': content,
                'embedding': embedding,
                'metadata': {
                    'order_number': order.number,
                    'status': order.status,
                    'total': str(order.total),
                    'buyer_id': str(order.buyer_id),
                    'supplier_id': str(order.supplier_id),
                },
                'language': 'ru',
                'access_roles': access_roles,
            }
        )
        return chunk
```

### 7.2 Management command

```python
# assistant/management/commands/index_catalog.py

from django.core.management.base import BaseCommand
from assistant.indexer import CatalogIndexer


class Command(BaseCommand):
    help = 'Индексация товарного каталога в vector store для AI-ассистента'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            help='Размер батча (по умолчанию 100)',
        )

    def handle(self, *args, **options):
        indexer = CatalogIndexer()
        self.stdout.write('Начинаю индексацию каталога...')

        count = indexer.index_all_products(
            batch_size=options['batch_size']
        )

        self.stdout.write(
            self.style.SUCCESS(f'Проиндексировано {count} товаров')
        )
```

---

## 8. Настройки Django

### 8.1 Добавить в `settings.py`

```python
# --- AI Assistant ---
INSTALLED_APPS += [
    'channels',
    'assistant',
]

# Claude API
ANTHROPIC_API_KEY = env('ANTHROPIC_API_KEY')  # обязательно

# Embeddings (OpenAI или Voyage AI)
OPENAI_API_KEY = env('OPENAI_API_KEY', default='')
VOYAGE_API_KEY = env('VOYAGE_API_KEY', default='')

# Django Channels (WebSocket)
ASGI_APPLICATION = 'config.asgi.application'
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [env('REDIS_URL', default='redis://localhost:6379/0')],
        },
    },
}

# pgvector
# PostgreSQL должен иметь расширение pgvector:
# CREATE EXTENSION IF NOT EXISTS vector;
```

### 8.2 Переменные окружения (`.env`)

```env
# Обязательные
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...          # для embeddings

# Опциональные
REDIS_URL=redis://localhost:6379/0
```

### 8.3 Файл `requirements.txt` (добавить)

```
anthropic>=0.25.0
django-pgvector>=0.1.0
pgvector>=0.2.0
channels>=4.0.0
channels-redis>=4.1.0
httpx>=0.25.0
```

---

## 9. Миграция БД для pgvector

```python
# assistant/migrations/0001_initial.py
# После makemigrations добавить в начало:

from django.db import migrations

class Migration(migrations.Migration):
    # В начало operations добавить:
    operations = [
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql="DROP EXTENSION IF EXISTS vector;",
        ),
        # ... остальные миграции моделей ...
    ]


# Отдельная миграция для ivfflat индекса (после заполнения данных):
# assistant/migrations/0002_vector_index.py

class Migration(migrations.Migration):
    dependencies = [('assistant', '0001_initial')]

    operations = [
        migrations.RunSQL(
            """
            CREATE INDEX IF NOT EXISTS assistant_knowledgechunk_embedding_idx
            ON assistant_knowledgechunk
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100);
            """,
            reverse_sql="DROP INDEX IF EXISTS assistant_knowledgechunk_embedding_idx;",
        ),
    ]
```

---

## 10. Сигналы для автоиндексации

### 10.1 Файл `signals.py`

```python
# assistant/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver
from .tasks import index_product_task, index_order_task


# АДАПТИРОВАТЬ: импортировать ваши модели
# from catalog.models import Product
# from orders.models import Order


# @receiver(post_save, sender=Product)
# def auto_index_product(sender, instance, **kwargs):
#     """Автоматически переиндексировать товар при изменении."""
#     index_product_task.delay(instance.id)


# @receiver(post_save, sender=Order)
# def auto_index_order(sender, instance, **kwargs):
#     """Автоматически переиндексировать заказ при изменении статуса."""
#     index_order_task.delay(instance.id)
```

### 10.2 Файл `tasks.py` (Celery)

```python
# assistant/tasks.py

from celery import shared_task
from .indexer import CatalogIndexer, OrderIndexer


@shared_task
def index_product_task(product_id):
    """Фоновая индексация товара."""
    from catalog.models import Product  # АДАПТИРОВАТЬ
    product = Product.objects.get(id=product_id)
    CatalogIndexer().index_product(product)


@shared_task
def index_order_task(order_id):
    """Фоновая индексация заказа."""
    from orders.models import Order  # АДАПТИРОВАТЬ
    order = Order.objects.get(id=order_id)
    OrderIndexer().index_order(order)


@shared_task
def reindex_all_task():
    """Полная переиндексация (запускать по cron раз в сутки)."""
    CatalogIndexer().index_all_products()
    # OrderIndexer().index_all_orders()
```

---

## 11. Фронтенд: Chat Widget

### 11.1 Минимальный виджет (встраивается в каждый кабинет)

Создать компонент чат-виджета, который:

1. **Плавающая кнопка** в правом нижнем углу (иконка AI / робот)
2. **Развернутое окно чата** (400x600px) с:
   - Заголовок "AI Ассистент" + кнопка закрытия
   - Область сообщений (scroll, разделение user/assistant)
   - Подсказки вопросов (chips) — загружать из `/api/assistant/suggest/`
   - Поле ввода + кнопка отправки
   - Индикатор "печатает..." при стриминге
   - Кнопки 👍/👎 под каждым ответом ассистента
3. **WebSocket** подключение для real-time стриминга ответов
4. **Markdown рендеринг** в ответах ассистента (таблицы, списки, код)
5. **Адаптивность**: на мобильных — полноэкранный режим

### 11.2 WebSocket клиент (JavaScript)

```javascript
// Пример подключения (адаптировать под ваш фреймворк)

class AssistantChat {
    constructor(conversationId = null) {
        const wsScheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
        const path = conversationId
            ? `/ws/assistant/${conversationId}/`
            : '/ws/assistant/';

        this.ws = new WebSocket(`${wsScheme}://${window.location.host}${path}`);
        this.responseBuffer = '';

        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);

            switch (data.type) {
                case 'connected':
                    this.conversationId = data.conversation_id;
                    break;
                case 'stream':
                    this.responseBuffer += data.content;
                    this.onStream(this.responseBuffer);  // обновить UI
                    break;
                case 'done':
                    this.onComplete(this.responseBuffer);
                    this.responseBuffer = '';
                    break;
                case 'error':
                    this.onError(data.message);
                    break;
            }
        };
    }

    send(message) {
        this.ws.send(JSON.stringify({
            type: 'message',
            content: message,
        }));
    }

    // Callbacks — переопределить в UI
    onStream(text) {}
    onComplete(text) {}
    onError(message) {}
}
```

---

## 12. Контрольный список реализации

### Фаза 1: MVP (2–3 недели)

- [ ] Установить pgvector в PostgreSQL: `CREATE EXTENSION vector;`
- [ ] Добавить зависимости в requirements.txt
- [ ] Создать Django app: `python manage.py startapp assistant`
- [ ] Реализовать модели: Conversation, Message, KnowledgeChunk, Feedback
- [ ] Выполнить миграции: `python manage.py makemigrations assistant && migrate`
- [ ] Реализовать `embeddings.py` — получение и поиск эмбеддингов
- [ ] Реализовать `prompts.py` — системные промпты по ролям
- [ ] Реализовать `rag.py` — RAG pipeline
- [ ] Реализовать `views.py` — REST API эндпоинты
- [ ] Реализовать `consumers.py` — WebSocket consumer
- [ ] Настроить Django Channels + Redis
- [ ] Реализовать `indexer.py` — индексация каталога
- [ ] Создать management command `index_catalog`
- [ ] Проиндексировать каталог товаров
- [ ] Добавить chat widget в фронтенд (плавающая кнопка + окно чата)
- [ ] Написать тесты для RAG pipeline и API
- [ ] Проверить работу на всех трёх ролях (buyer, seller, operator)

### Фаза 2: RAG + История (3–4 недели)

- [ ] Индексация заказов, RFQ, отгрузок
- [ ] Индексация документов (PDF, чертежи, сертификаты)
- [ ] Автоиндексация через Django signals
- [ ] Celery tasks для фоновой индексации
- [ ] Персональный контекст: "мои заказы", "мои RFQ"
- [ ] История диалогов в UI (sidebar со списком чатов)
- [ ] Фильтрация контекста по компании пользователя

### Фаза 3: Автоматизация (4–6 недель)

- [ ] Действия из чата: "Создай RFQ на эту деталь" → создаёт RFQ
- [ ] Проактивные уведомления: "У вас 3 просроченных RFQ"
- [ ] Мультимодальность: загрузка фото детали → поиск по каталогу
- [ ] Аналитические отчёты по запросу: "Отчёт по закупкам за Q1"
- [ ] Интеграция с email-уведомлениями

---

## 13. Важные замечания для разработчика

### 13.1 Адаптация под существующие модели
В коде есть комментарии `# АДАПТИРОВАТЬ` — это места, где нужно заменить примеры на реальные модели проекта (Product, Order, User Profile и т.д.). Найти все:
```bash
grep -rn "АДАПТИРОВАТЬ" assistant/
```

### 13.2 Безопасность
- **API ключи** — только через переменные окружения, никогда в коде
- **Фильтрация по роли** — каждый чанк имеет `access_roles`, проверка обязательна
- **Персональные данные** — фильтровать чанки заказов по `buyer_id`/`supplier_id`, чтобы покупатель не видел чужие заказы
- **Rate limiting** — ограничить количество запросов к AI (например, 50/час на пользователя)
- **Токены** — мониторить расход токенов Claude API (поле `tokens_used` в Message)

### 13.3 Производительность
- **Batch embeddings** — при индексации отправлять батчами, не по одному
- **ivfflat индекс** — создавать ПОСЛЕ первичной загрузки данных (нужно минимум 1000 записей для lists=100)
- **Кэширование** — кэшировать подсказки (suggest) и часто запрашиваемые ответы
- **Async** — все операции с Claude API и pgvector делать асинхронно

### 13.4 Мониторинг
- Логировать каждый запрос: время ответа, количество токенов, количество чанков контекста
- Отслеживать feedback (👍/👎) для улучшения промптов
- Алерт при высоком расходе токенов или ошибках API
