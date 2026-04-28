from rest_framework import serializers

from .models import Conversation, Feedback, Message


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ["id", "role", "content", "context_refs", "tokens_used", "created_at"]
        read_only_fields = fields


class ConversationSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "role", "title", "is_active", "created_at", "updated_at",
                   "messages", "message_count"]
        read_only_fields = ["id", "created_at", "updated_at", "messages", "message_count"]

    def get_message_count(self, obj):
        return obj.messages.count()


class ConversationListSerializer(serializers.ModelSerializer):
    """Compact serializer for list view."""
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "role", "title", "created_at", "updated_at", "last_message"]

    def get_last_message(self, obj):
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
