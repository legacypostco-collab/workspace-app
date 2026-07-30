"""WebSocket consumer for streaming AI responses + realtime notifications."""
import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer

from .models import Conversation
from .permissions import detect_user_role
from .rag import process_query_stream

logger = logging.getLogger(__name__)

MAX_WS_FRAME_CHARS = 16_384
MAX_WS_MESSAGE_CHARS = 4_000
WS_MESSAGES_PER_MINUTE = 60


def push_notification_to_user(user_id: int, payload: dict):
    """Deliver a DB-backed notification to the single assistant channel."""
    try:
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if not layer:
            return
        async_to_sync(layer.group_send)(
            f"notif_user_{user_id}",
            {"type": "notify", "payload": payload},
        )
    except Exception:
        logger.exception("push_notification_to_user failed")


def push_rfq_update_to_user(user_id: int, *, rfq_id: int, event: str = "rfq_update",
                            quote_id: int | None = None):
    """Live-событие изменения RFQ/котировки для открытого chat-first UI.

    Это не уведомление и не увеличивает unread-счётчик. Фронт использует его
    только чтобы перерисовать открытую карточку/список RFQ без F5.
    """
    try:
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if not layer:
            return
        async_to_sync(layer.group_send)(
            f"notif_user_{user_id}",
            {
                "type": "rfq_update",
                "rfq_id": rfq_id,
                "event": event,
                "quote_id": quote_id,
            },
        )
    except Exception:
        logger.exception("push_rfq_update_to_user failed")


class AssistantConsumer(AsyncWebsocketConsumer):
    """ws://host/ws/assistant/[<conversation_id>/]

    Client → Server: {"type":"message", "content":"text"}
    Server → Client:
      {"type":"connected", "conversation_id":"uuid"}
      {"type":"thinking"}
      {"type":"context", "refs":[...]}
      {"type":"stream", "content":"chunk"}
      {"type":"done", "tokens":N, "refs":[...]}
      {"type":"error", "message":"..."}
    """

    async def connect(self):
        self.user = self.scope["user"]
        # P1 (resilience): проверяем is_active на КАЖДОМ (ре)коннекте —
        # деактивированный пользователь с ещё живой сессией не должен держать WS.
        if (not self.user or not self.user.is_authenticated
                or not getattr(self.user, "is_active", False)):
            await self.close(code=4401)
            return

        self.active_role = await self._get_active_role()
        conv_id = self.scope["url_route"]["kwargs"].get("conversation_id")
        self.conversation = await self._get_existing_conversation(conv_id) if conv_id else None

        # Подписка на персональную группу для realtime-уведомлений
        self.notif_group = f"notif_user_{self.user.id}"
        try:
            await self.channel_layer.group_add(self.notif_group, self.channel_name)
        except Exception:
            self.notif_group = None

        await self.accept()
        await self.send_json({
            "type": "connected",
            "conversation_id": str(self.conversation.id) if self.conversation else None,
            "role": self.conversation.role if self.conversation else None,
        })

    async def disconnect(self, code):
        if getattr(self, "notif_group", None):
            try:
                await self.channel_layer.group_discard(self.notif_group, self.channel_name)
            except Exception:
                # P1: не глушим молча — иначе при сбое Redis консьюмер навсегда
                # остаётся в группе (утечка нотификаций на recycled-сокеты).
                logger.warning("WS group_discard failed for %s", self.notif_group,
                               exc_info=True)

    async def notify(self, event):
        """Получено push-уведомление из канала. Шлём клиенту."""
        await self.send_json({
            "type": "notification",
            "payload": event.get("payload") or {},
        })

    async def order_update(self, event):
        """Обновление заказа — клиент перезагрузит timeline в shipment-чате."""
        await self.send_json({
            "type": "order_update",
            "order_id": event.get("order_id"),
            "event": event.get("event"),
            "conversation_id": event.get("conversation_id"),
        })

    async def operator_alert(self, event):
        """Алерт оператору (SLA breach, SEMI overdue и т.д.)."""
        await self.send_json({
            "type": "operator_alert",
            "event": event.get("event"),
            "rfq_id": event.get("rfq_id"),
            "order_id": event.get("order_id"),
            "claim_id": event.get("claim_id"),
        })

    async def rfq_update(self, event):
        """Изменение RFQ/котировки — фронт обновит открытую RFQ-карточку."""
        await self.send_json({
            "type": "rfq_update",
            "event": event.get("event"),
            "rfq_id": event.get("rfq_id"),
            "quote_id": event.get("quote_id"),
        })

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None:
            await self.close(code=1003)
            return
        if not isinstance(text_data, str) or len(text_data) > MAX_WS_FRAME_CHARS:
            await self.close(code=1009)
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json({"type": "error", "message": "Invalid JSON"})
            return
        if not isinstance(data, dict):
            await self.send_json({"type": "error", "message": "Invalid message"})
            return

        if data.get("type") == "ping":
            await self.send_json({"type": "pong"})
            return

        if data.get("type") != "message":
            return

        content = data.get("content")
        if not isinstance(content, str):
            await self.send_json({"type": "error", "message": "Invalid message"})
            return
        msg = content.strip()
        if not msg:
            await self.send_json({"type": "error", "message": "Empty message"})
            return
        if len(msg) > MAX_WS_MESSAGE_CHARS:
            await self.send_json({
                "type": "error",
                "message": "Сообщение слишком длинное.",
            })
            return
        if not await self._message_rate_ok():
            await self.send_json({
                "type": "error",
                "message": "Слишком много сообщений. Повторите через минуту.",
            })
            return

        # Lazy creation: only spawn a new Conversation row when the user
        # actually says something. This prevents empty "Без названия" chats
        # from accumulating on every page reload.
        if not self.conversation:
            self.conversation = await self._create_conversation()
            await self.send_json({
                "type": "connected",
                "conversation_id": str(self.conversation.id),
                "role": self.conversation.role,
            })

        if await self._is_human_support():
            try:
                await self._post_support_message(msg)
                await self.send_json({"type": "support_sent"})
            except PermissionError as exc:
                await self.send_json({"type": "error", "message": str(exc)})
            return

        try:
            async for event in self._stream_response(msg):
                await self.send_json(event)
        except Exception:
            logger.exception("Assistant stream error")
            await self.send_json({
                "type": "error",
                "message": "Не удалось обработать сообщение. Повторите попытку.",
            })

    async def send_json(self, payload):
        await self.send(text_data=json.dumps(payload))

    @database_sync_to_async
    def _get_active_role(self):
        session = self.scope.get("session")
        override = session.get("assistant_role_override") if session else None
        return detect_user_role(self.user, override=override)

    @database_sync_to_async
    def _get_existing_conversation(self, conv_id):
        from .conversation_access import get_accessible_conversation
        try:
            return get_accessible_conversation(self.user, self.active_role, conv_id)
        except Conversation.DoesNotExist:
            return None

    @database_sync_to_async
    def _create_conversation(self):
        return Conversation.objects.create(user=self.user, role=self.active_role)

    @database_sync_to_async
    def _message_rate_ok(self):
        from .ai_credits import rate_ok

        return rate_ok(
            self.user,
            "chat_ws_message",
            WS_MESSAGES_PER_MINUTE,
            60,
        )

    @database_sync_to_async
    def _is_human_support(self):
        from .support_threads import is_human_support
        return is_human_support(self.conversation)

    @database_sync_to_async
    def _post_support_message(self, content):
        from .support_threads import post_support_message
        return post_support_message(
            self.conversation, self.user, self.active_role, content,
        )

    async def _stream_response(self, message):
        """Pull the sync generator one event at a time without buffering it."""
        # i18n keystone: WebSocket НЕ проходит Django LocaleMiddleware, поэтому
        # явно активируем язык пользователя (UserProfile.language) на время
        # генерации ответа — иначе gettext-строки ассистента всегда по-русски.
        def _run():
            from django.conf import settings
            from django.utils import translation
            # Приоритет — у явного выбора в cookie `django_language` (как в
            # UserLanguageMiddleware): профиль может отставать, если POST
            # /api/set-language не сохранил его. WS-scope содержит cookies
            # (AuthMiddlewareStack → CookieMiddleware).
            lang = None
            try:
                allowed = {c for c, _lbl in settings.LANGUAGES}
                cookies = self.scope.get("cookies") or {}
                cl = (cookies.get("django_language") or "").strip().lower()
                if cl in allowed:
                    lang = cl
            except Exception:
                lang = None
            if not lang:
                try:
                    lang = (getattr(self.user, "profile", None)
                            and self.user.profile.language) or "ru"
                except Exception:
                    lang = "ru"
            with translation.override(lang):
                yield from process_query_stream(self.conversation, message, ui_lang=lang)

        def _next_event(gen):
            try:
                return False, next(gen)
            except StopIteration:
                return True, None

        gen = _run()
        while True:
            done, ev = await database_sync_to_async(_next_event)(gen)
            if done:
                break
            # Map internal event → WS protocol
            if ev["type"] == "token":
                yield {"type": "stream", "content": ev["text"]}
            else:
                yield ev
