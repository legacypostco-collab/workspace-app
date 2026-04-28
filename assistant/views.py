from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Conversation, Feedback, Message
from .permissions import detect_user_role
from .rag import process_query_sync
from .serializers import (
    ChatRequestSerializer,
    ConversationListSerializer,
    ConversationSerializer,
    FeedbackSerializer,
)


class ConversationViewSet(viewsets.ModelViewSet):
    """CRUD for chat sessions.

    GET    /api/assistant/conversations/        — list
    POST   /api/assistant/conversations/        — create new
    GET    /api/assistant/conversations/{id}/   — detail with messages
    DELETE /api/assistant/conversations/{id}/   — soft delete (is_active=False)
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Conversation.objects.filter(user=self.request.user, is_active=True)

    def get_serializer_class(self):
        if self.action == "list":
            return ConversationListSerializer
        return ConversationSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, role=detect_user_role(self.request.user))

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class ChatView(APIView):
    """Synchronous chat endpoint (use WebSocket for streaming).

    POST /api/assistant/chat/
    Body: {"conversation_id": "uuid"|null, "message": "text"}
    Resp: {"conversation_id": "uuid", "response": "...", "context_refs": [...]}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = ChatRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        conv_id = ser.validated_data.get("conversation_id")

        if conv_id:
            conv = get_object_or_404(
                Conversation, id=conv_id, user=request.user, is_active=True
            )
        else:
            conv = Conversation.objects.create(
                user=request.user, role=detect_user_role(request.user)
            )

        try:
            response, refs = process_query_sync(conv, ser.validated_data["message"])
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "conversation_id": str(conv.id),
            "response": response,
            "context_refs": refs,
        })


class FeedbackView(APIView):
    """Rate an assistant message (👍/👎).

    POST /api/assistant/feedback/
    Body: {"message_id": "uuid", "rating": 1|-1, "comment": "..."}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = FeedbackSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        msg = get_object_or_404(
            Message,
            id=ser.validated_data["message_id"],
            conversation__user=request.user,
        )
        Feedback.objects.update_or_create(
            message=msg,
            defaults={
                "rating": ser.validated_data["rating"],
                "comment": ser.validated_data.get("comment", ""),
            },
        )
        return Response({"status": "ok"}, status=status.HTTP_201_CREATED)


class SuggestView(APIView):
    """Suggested questions per role.

    GET /api/assistant/suggest/?role=buyer
    """
    permission_classes = [IsAuthenticated]

    SUGGESTIONS = {
        "buyer": [
            "Покажи мои активные RFQ",
            "Какие гусеничные цепи есть для Komatsu?",
            "Статус моих заказов за последний месяц",
            "Сравни поставщиков по SLA",
        ],
        "seller": [
            "Новые RFQ за сегодня",
            "Какие запчасти ищут чаще всего?",
            "Мои просроченные заказы",
            "KPI за этот месяц",
        ],
        "operator_logist": [
            "Какие отгрузки сейчас в пути?",
            "Есть ли нарушения SLA?",
            "Контейнеры на таможне",
        ],
        "operator_customs": [
            "Грузы ожидающие растаможки",
            "Документы для контейнера",
            "Просроченные декларации",
        ],
        "operator_payment": [
            "Неоплаченные инвойсы",
            "Просроченные платежи",
            "Эскроу-счета по заказам",
        ],
        "operator_manager": [
            "Конверсия RFQ → заказ за месяц",
            "Топ покупатели по выручке",
            "Неактивные клиенты",
        ],
        "admin": [
            "Метрики платформы за неделю",
            "Поставщики на верификации",
            "Просроченные SLA",
        ],
    }

    def get(self, request):
        role = request.query_params.get("role") or detect_user_role(request.user)
        return Response({
            "role": role,
            "suggestions": self.SUGGESTIONS.get(role, self.SUGGESTIONS["buyer"]),
        })


class WidgetConfigView(APIView):
    """Initial config for the chat widget — role, suggestions, latest conv."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        role = detect_user_role(request.user)
        latest = Conversation.objects.filter(
            user=request.user, is_active=True
        ).order_by("-updated_at").first()
        return Response({
            "role": role,
            "user_name": request.user.get_full_name() or request.user.username,
            "suggestions": SuggestView.SUGGESTIONS.get(role, SuggestView.SUGGESTIONS["buyer"]),
            "latest_conversation_id": str(latest.id) if latest else None,
        })
