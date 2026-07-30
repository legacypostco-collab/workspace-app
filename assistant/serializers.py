import re

from django.utils.translation import gettext as _t
from rest_framework import serializers

from .models import Conversation, Message


def _translate_conv_title(title: str) -> str:
    """
    Перевод хранимых в БД названий conv.title под текущий язык.
    Поддерживаемые шаблоны:
      "Сделка ORD-{N}"       → tr + ORD-N
      "Сделка ORD-{N} — ..."  → tr + ORD-N + остаток
    Если префикс не совпал — возвращаем title как есть.
    """
    if not title:
        return title
    m = re.match(r"^(Сделка)\s+(ORD-\d+)(.*)$", title)
    if m:
        return f"{_t('Сделка')} {m.group(2)}{m.group(3)}"
    return title


class MessageSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ["id", "role", "content", "cards", "actions", "context_refs",
                  "contextual_actions", "suggestions", "tokens_used", "created_at"]
        read_only_fields = fields

    def get_role(self, obj) -> str:
        request = self.context.get("request")
        if obj.sender_id and request and getattr(request, "user", None):
            return (
                Message.Role.USER
                if obj.sender_id == request.user.id
                else Message.Role.ASSISTANT
            )
        return obj.role


class ConversationSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "role", "category", "title", "is_active", "support_status", "created_at", "updated_at",
                   "messages", "message_count"]
        read_only_fields = [
            "id", "role", "category", "support_status", "created_at",
            "updated_at", "messages", "message_count",
        ]

    def get_message_count(self, obj) -> int:
        return obj.messages.count()


class ConversationListSerializer(serializers.ModelSerializer):
    """Compact serializer for list view."""
    last_message = serializers.SerializerMethodField()
    title = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "role", "category", "title", "support_status", "created_at", "updated_at", "last_message"]

    def get_title(self, obj) -> str:
        return _translate_conv_title(obj.title)

    def get_last_message(self, obj) -> dict | None:
        msg = obj.messages.order_by("-created_at").first()
        if not msg:
            return None
        role = MessageSerializer(context=self.context).get_role(msg)
        return {"role": role, "content": msg.content[:120], "created_at": msg.created_at}


class ChatRequestSerializer(serializers.Serializer):
    conversation_id = serializers.UUIDField(required=False, allow_null=True)
    message = serializers.CharField(min_length=1, max_length=4000)


class ActionRequestSerializer(serializers.Serializer):
    conversation_id = serializers.UUIDField(required=False, allow_null=True)
    operation_id = serializers.UUIDField(required=False, allow_null=True)
    action = serializers.CharField(min_length=1, max_length=128)
    params = serializers.DictField(required=False, default=dict)


class FeedbackSerializer(serializers.Serializer):
    message_id = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=-1, max_value=1)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=2000)
