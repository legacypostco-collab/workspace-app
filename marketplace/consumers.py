"""WebSocket consumers — replaces 30s polling for notifications."""
from __future__ import annotations

import json
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """Per-user notification stream.

    Connect: ws://host/ws/notifications/
    Auth: session-based (Django auth middleware required in ASGI)
    Server pushes: {"type":"notification","data":{...}}
    Client pings: {"type":"ping"} → server replies {"type":"pong"}
    Client marks read: {"type":"mark_read","id":42}
    """
    group_name = None

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.group_name = f"user_{user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        # On connect, send unread count snapshot
        unread = await self._unread_count(user.id)
        await self.send_json({"type": "unread_count", "count": unread})

    async def disconnect(self, code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")
        if msg_type == "ping":
            await self.send_json({"type": "pong"})
        elif msg_type == "mark_read":
            notif_id = content.get("id")
            user = self.scope.get("user")
            if user and notif_id:
                await self._mark_read(user.id, notif_id)
                unread = await self._unread_count(user.id)
                await self.send_json({"type": "unread_count", "count": unread})
        elif msg_type == "mark_all_read":
            user = self.scope.get("user")
            if user:
                await self._mark_all_read(user.id)
                await self.send_json({"type": "unread_count", "count": 0})

    # Channel layer event handler — called when group receives message
    async def notification_message(self, event):
        await self.send_json({"type": "notification", "data": event["data"]})
        # Refresh unread count
        user = self.scope.get("user")
        if user:
            unread = await self._unread_count(user.id)
            await self.send_json({"type": "unread_count", "count": unread})

    @staticmethod
    async def _unread_count(user_id):
        from channels.db import database_sync_to_async
        from .models import Notification
        return await database_sync_to_async(
            lambda: Notification.objects.filter(user_id=user_id, is_read=False).count()
        )()

    @staticmethod
    async def _mark_read(user_id, notif_id):
        from channels.db import database_sync_to_async
        from .models import Notification
        await database_sync_to_async(
            lambda: Notification.objects.filter(user_id=user_id, id=notif_id).update(is_read=True)
        )()

    @staticmethod
    async def _mark_all_read(user_id):
        from channels.db import database_sync_to_async
        from .models import Notification
        await database_sync_to_async(
            lambda: Notification.objects.filter(user_id=user_id, is_read=False).update(is_read=True)
        )()
