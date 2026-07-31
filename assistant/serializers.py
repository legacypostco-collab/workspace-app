import re

from django.utils.translation import gettext as _t
from rest_framework import serializers

from .conv_category import clean_action_label, humanize_action_title
from .models import Conversation, Message


_DISPLAY_TEXT_KEYS = {
    "title",
    "subtitle",
    "label",
    "text",
    "hint",
    "body",
    "description",
    "sub",
}


def _humanize_legacy_text(value: str) -> str:
    """Скрыть старые внутренние обозначения в сохранённой истории чата."""
    result = re.sub(
        r"\bRFQ\s*[#№-]?\s*(\d+)\b",
        lambda match: f"{_t('Заявка')} №{match.group(1)}",
        value,
        flags=re.IGNORECASE,
    )
    replacements = (
        (r"\bRFQ\b", _t("заявка")),
        (r"\bКП\b", _t("предложение")),
        (r"\bAUTO\b", _t("Автоподбор")),
        (r"\bSEMI\b", _t("Нужно подтвердить")),
        (r"\bMANUAL\b", _t("Ручной подбор")),
        (r"approve\s+КП", _t("утвердить предложение")),
        (r"\bSLA\s+breach\b", _t("нарушение срока")),
        (r"\bSLA\b", _t("Срок")),
        (r"\bbuyer\b", _t("покупатель")),
        (r"\bseller\b", _t("поставщик")),
        (r"\boperator\b", _t("оператор")),
        (r"\bGMV\b", _t("оборот")),
        (r"\bKYB\b", _t("проверка компании")),
        (r"\bPENDING\b", _t("ожидает проверки")),
        (r"\bVERIFIED\b", _t("проверено")),
        (r"\bNeeds\s+Review\b", _t("требует проверки")),
        (r"\bQuoted\b", _t("есть предложения")),
        (r"\bCancelled\b", _t("отменён")),
        (r"\bNew\b", _t("новый")),
    )
    for pattern, replacement in replacements:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    result = re.sub(
        r"(?m)^[ \t]*(?:[\u2190-\u21ff\u2600-\u27bf\U0001f300-\U0001faff]+[ \t]*)+",
        "",
        result,
    )
    return result


def _humanize_legacy_payload(value, key: str | None = None):
    """Обработать только отображаемые поля, не меняя action/params/mode."""
    if isinstance(value, list):
        return [_humanize_legacy_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            item_key: _humanize_legacy_payload(item_value, item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, str) and key in _DISPLAY_TEXT_KEYS:
        return _humanize_legacy_text(value)
    return value


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
    cleaned = clean_action_label(title)
    for old, new in (
        ("Статусы RFQ", _t("Заявки")),
        ("Создать RFQ", _t("Создать заявку")),
        ("Детали RFQ", _t("Детали заявки")),
    ):
        cleaned = cleaned.replace(old, str(new))
    m = re.match(r"^(Сделка)\s+(ORD-\d+)(.*)$", cleaned)
    if m:
        return f"{_t('Сделка')} {m.group(2)}{m.group(3)}"

    # Старые заголовки могли хранить внутреннее имя действия целиком или
    # после названия раздела: «Покупки · create_rfq».
    parts = [part.strip() for part in cleaned.split("·")]
    translated = []
    for part in parts:
        if re.fullmatch(r"[a-z][a-z0-9_]*", part, flags=re.IGNORECASE):
            translated.append(humanize_action_title(part))
        else:
            translated.append(part)
    return " · ".join(part for part in translated if part)


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

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["content"] = _humanize_legacy_text(data.get("content") or "")
        for field in ("cards", "actions", "contextual_actions", "suggestions"):
            data[field] = _humanize_legacy_payload(data.get(field) or [])
        return data


class ConversationSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)
    message_count = serializers.SerializerMethodField()
    title = serializers.SerializerMethodField()

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

    def get_title(self, obj) -> str:
        return _translate_conv_title(obj.title)


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
        return {
            "role": role,
            "content": _humanize_legacy_text(msg.content[:120]),
            "created_at": msg.created_at,
        }


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
