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
    class Meta:
        model = Message
        fields = ["id", "role", "content", "cards", "actions", "context_refs",
                  "tokens_used", "created_at"]
        read_only_fields = fields


class ConversationSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "role", "category", "title", "is_active", "created_at", "updated_at",
                   "messages", "message_count"]
        read_only_fields = ["id", "created_at", "updated_at", "messages", "message_count"]

    def get_message_count(self, obj) -> int:
        return obj.messages.count()


class ConversationListSerializer(serializers.ModelSerializer):
    """Compact serializer for list view."""
    last_message = serializers.SerializerMethodField()
    title = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "role", "category", "title", "created_at", "updated_at", "last_message"]

    def get_title(self, obj) -> str:
        return _translate_conv_title(obj.title)

    def get_last_message(self, obj) -> dict | None:
        msg = obj.messages.order_by("-created_at").first()
        if not msg:
            return None
        return {"role": msg.role, "content": msg.content[:120], "created_at": msg.created_at}


class ChatRequestSerializer(serializers.Serializer):
    conversation_id = serializers.UUIDField(required=False, allow_null=True)
    message = serializers.CharField(min_length=1, max_length=4000)


class FeedbackSerializer(serializers.Serializer):
    message_id = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=-1, max_value=1)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=2000)
