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


def push_notification_to_user(user_id: int, payload: dict):
    """Sync helper — отправляет нотификацию по WS всем сессиям пользователя.

    Channel group: notif_user_<id>. Если channel-layer не настроен (нет
    in-memory или Redis), функция тихо игнорирует — основной flow не сломается.
    """
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
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4401)
            return

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
                pass

    async def notify(self, event):
        """Получено push-уведомление из канала. Шлём клиенту."""
        await self.send_json({
            "type": "notification",
            "payload": event.get("payload") or {},
        })

    async def receive(self, text_data=None, bytes_data=None):
        try:
            data = json.loads(text_data or "{}")
        except json.JSONDecodeError:
            await self.send_json({"type": "error", "message": "Invalid JSON"})
            return

        if data.get("type") == "ping":
            await self.send_json({"type": "pong"})
            return

        if data.get("type") != "message":
            return

        msg = (data.get("content") or "").strip()
        if not msg:
            await self.send_json({"type": "error", "message": "Empty message"})
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

        try:
            async for event in self._stream_response(msg):
                await self.send_json(event)
        except Exception as e:
            logger.exception("Assistant stream error")
            await self.send_json({"type": "error", "message": str(e)})

    async def send_json(self, payload):
        await self.send(text_data=json.dumps(payload))

    @database_sync_to_async
    def _get_existing_conversation(self, conv_id):
        try:
            return Conversation.objects.get(id=conv_id, user=self.user, is_active=True)
        except Conversation.DoesNotExist:
            return None

    @database_sync_to_async
    def _create_conversation(self):
        return Conversation.objects.create(user=self.user, role=detect_user_role(self.user))

    async def _stream_response(self, message):
        """Wrap sync generator process_query_stream into async."""
        # Convert sync generator → async via database_sync_to_async pulls
        gen = await database_sync_to_async(lambda: list(process_query_stream(self.conversation, message)))()
        for ev in gen:
            # Map internal event → WS protocol
            if ev["type"] == "token":
                yield {"type": "stream", "content": ev["text"]}
            else:
                yield ev
