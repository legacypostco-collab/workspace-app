"""WebSocket consumer for streaming AI responses."""
import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .models import Conversation
from .permissions import detect_user_role
from .rag import process_query_stream

logger = logging.getLogger(__name__)


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
        self.conversation = await self._get_or_create_conversation(conv_id)
        # If specific conv_id was requested but doesn't belong to user,
        # silently fall back to creating new conversation instead of disconnecting
        if not self.conversation:
            self.conversation = await self._get_or_create_conversation(None)

        await self.accept()
        await self.send_json({
            "type": "connected",
            "conversation_id": str(self.conversation.id),
            "role": self.conversation.role,
        })

    async def disconnect(self, code):
        pass

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

        try:
            async for event in self._stream_response(msg):
                await self.send_json(event)
        except Exception as e:
            logger.exception("Assistant stream error")
            await self.send_json({"type": "error", "message": str(e)})

    async def send_json(self, payload):
        await self.send(text_data=json.dumps(payload))

    @database_sync_to_async
    def _get_or_create_conversation(self, conv_id):
        if conv_id:
            try:
                return Conversation.objects.get(id=conv_id, user=self.user, is_active=True)
            except Conversation.DoesNotExist:
                return None
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
