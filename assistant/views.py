from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Conversation, Feedback, Message
from .permissions import detect_user_role
from .rag import execute_action, process_query_sync
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
        serializer.save(user=self.request.user,
                        role=detect_user_role(self.request.user, request=self.request))

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
                user=request.user, role=detect_user_role(request.user, request=request)
            )

        try:
            result = process_query_sync(conv, ser.validated_data["message"], request.user)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "conversation_id": str(conv.id),
            "response": result["text"],
            "cards": result["cards"],
            "actions": result["actions"],
            "contextual_actions": result.get("contextual_actions", []),
            "context_refs": result["context_refs"],
            "suggestions": result.get("suggestions", []),
            "message_id": result.get("message_id"),
        })


class ActionView(APIView):
    """Execute a chat action (button click).

    POST /api/assistant/action/
    Body: {"conversation_id":"uuid","action":"create_rfq","params":{"product_ids":[...],"_label":"..."}}
    Resp: {"text":"...","cards":[...],"actions":[...],"suggestions":[...]}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        conv_id = request.data.get("conversation_id")
        action = request.data.get("action") or ""
        params = request.data.get("params") or {}
        if not action:
            return Response({"error": "action required"}, status=400)

        if conv_id:
            conv = get_object_or_404(Conversation, id=conv_id, user=request.user, is_active=True)
        else:
            conv = Conversation.objects.create(
                user=request.user, role=detect_user_role(request.user, request=request)
            )

        try:
            result = execute_action(conv, action, params, request.user)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

        return Response({
            "conversation_id": str(conv.id),
            **result,
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
        role = request.query_params.get("role") or detect_user_role(request.user, request=request)
        return Response({
            "role": role,
            "suggestions": self.SUGGESTIONS.get(role, self.SUGGESTIONS["buyer"]),
        })


class WidgetConfigView(APIView):
    """Initial config for the chat widget — role, suggestions, latest conv."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        role = detect_user_role(request.user, request=request)
        latest = Conversation.objects.filter(
            user=request.user, is_active=True
        ).order_by("-updated_at").first()
        return Response({
            "role": role,
            "role_override": (request.session.get("assistant_role_override") if hasattr(request, "session") else None),
            "user_name": request.user.get_full_name() or request.user.username,
            "suggestions": SuggestView.SUGGESTIONS.get(role, SuggestView.SUGGESTIONS["buyer"]),
            "latest_conversation_id": str(latest.id) if latest else None,
        })


class RoleSwitchView(APIView):
    """POST /api/assistant/role/  body: {"role": "buyer"|"seller"|"operator"|null}

    Сохраняет выбор UI-toggle в сессии. На последующих запросах
    `detect_user_role` подхватит его автоматически.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .permissions import _normalize_override
        raw = request.data.get("role")
        if raw in (None, "", "auto"):
            request.session.pop("assistant_role_override", None)
            request.session.modified = True
            new_role = detect_user_role(request.user)
            return Response({"role": new_role, "override": None})
        norm = _normalize_override(raw)
        if not norm:
            return Response({"error": f"unsupported role '{raw}'"}, status=400)
        request.session["assistant_role_override"] = norm
        request.session.modified = True
        return Response({"role": norm, "override": norm})


# ── Projects API ────────────────────────────────────────────
from .models import Project, ProjectDocument


class ProjectListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Project.objects.filter(owner=request.user, is_active=True)
        items = []
        for p in qs:
            chats = p.conversations.filter(is_active=True).count() if hasattr(p, "conversations") else 0
            items.append({
                "id": str(p.id),
                "name": p.name,
                "code": p.code,
                "customer": p.customer,
                "tags": p.tags,
                "deadline": p.deadline.isoformat() if p.deadline else None,
                "dot_color": p.dot_color,
                "chats": chats,
            })
        return Response({"projects": items})

    def post(self, request):
        data = request.data
        p = Project.objects.create(
            owner=request.user,
            name=data.get("name", "Новый проект")[:200],
            code=data.get("code", "")[:50],
            customer=data.get("customer", "")[:200],
            tags=data.get("tags", []),
            description=data.get("description", ""),
            dot_color=data.get("dot_color", "green"),
        )
        return Response({"id": str(p.id), "name": p.name}, status=201)


class ProjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        p = get_object_or_404(Project, id=project_id, owner=request.user, is_active=True)
        # Documents
        docs = [{
            "id": str(d.id),
            "name": d.name,
            "doctype": d.doctype,
            "doctype_label": d.get_doctype_display(),
            "status": d.status,
            "size_kb": round(d.size_bytes / 1024, 1) if d.size_bytes else None,
            "meta": d.meta,
            "uploaded_at": d.uploaded_at.strftime("%d.%m.%Y"),
        } for d in p.documents.all()]
        # Linked chats
        chats = [{
            "id": str(c.id),
            "title": c.title,
            "updated_at": c.updated_at.isoformat(),
            "preview": (c.messages.first().content[:120] if c.messages.exists() else ""),
        } for c in p.conversations.filter(is_active=True).order_by("-updated_at")[:20]]
        # Stats: count linked RFQs/orders by code matching (demo)
        from marketplace.models import RFQ, Order
        # In real system there'd be FK; for now just demo counts
        return Response({
            "id": str(p.id),
            "name": p.name,
            "code": p.code,
            "customer": p.customer,
            "tags": p.tags,
            "deadline": p.deadline.strftime("%d %B").lower() if p.deadline else None,
            "dot_color": p.dot_color,
            "description": p.description,
            "documents": docs,
            "chats": chats,
            # Demo stats — could be real per-project counts later
            "stats": {
                "open_rfqs": {"count": 3, "urgent": 1, "urgent_left": "42m"},
                "active_orders": {"count": 5, "value_usd": 184200},
                "in_transit": {"count": 2, "earliest_eta": "30 апр"},
                "spend_mtd": {"value_usd": 124500, "delta_pct": 12, "vs_period": "Mar"},
            },
            # Demo RFQs/orders/chats from this project (could be filtered by FK in real)
            "rfqs": [
                {"number": "RFQ-4421", "title": "Spec Q2 — основной микс", "tag": "URGENT 42M",
                 "meta": "39 позиций · отправлен 5 поставщикам · 2 ответили",
                 "responded": "2/5", "best_so_far": 47890, "responded_color": "green"},
                {"number": "RFQ-4418", "title": "Track shoes D8T — аналоги", "tag": "",
                 "meta": "2 позиции · отправлен 4 поставщикам · 4 ответили",
                 "responded": "4/4", "best_so_far": 7440, "responded_color": "green",
                 "best_label": "BEST PRICE"},
                {"number": "RFQ-4407", "title": "Hydraulic filters — refill", "tag": "",
                 "meta": "1 позиция · 12 шт · отправлен 3 поставщикам",
                 "responded": "1/3", "best_so_far": 2112, "responded_color": "amber"},
            ],
            "orders": [
                {"number": "PO-22841", "title": "Spec Q2 partial — 14 позиций",
                 "status": "AT CUSTOMS", "status_color": "amber",
                 "stages": [True, True, True, True, False],  # 4/5 done
                 "stage_labels": ["RFQ", "Order", "Production", "Customs", "Delivered"],
                 "seller": "XCMG", "operator": "Logist + Customs",
                 "eta": "ETA · 2 мая · day 3 of 4", "amount": 28640},
                {"number": "PO-22829", "title": "Hydraulic filters — 12 шт",
                 "status": "IN TRANSIT", "status_color": "green",
                 "stages": [True, True, True, False, False],
                 "stage_labels": ["RFQ", "Order", "Production", "Customs", "Delivered"],
                 "seller": "Caterpillar Eurasia", "operator": "Logist",
                 "eta": "ETA · 30 апр · on schedule", "amount": 2112},
            ],
        })


class ProjectChatView(APIView):
    """Create new conversation in this project."""
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        p = get_object_or_404(Project, id=project_id, owner=request.user)
        c = Conversation.objects.create(
            user=request.user,
            role=detect_user_role(request.user, request=request),
            project=p,
        )
        return Response({"conversation_id": str(c.id)}, status=201)


class RFQDetailView(APIView):
    """RFQ detail JSON for chat-first /chat/rfq/<id>/ page.

    Returns structured data the rfq-page.js renderer expects:
    {id, status, mode, urgency, customer_name, created_at, items:[{
      article, qty, state, match, brand, supplier, price, currency
    }]}
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, rfq_id):
        from marketplace.models import RFQ
        rfq = get_object_or_404(RFQ, id=rfq_id)
        items = []
        for it in rfq.items.select_related("matched_part__brand").all():
            mp = it.matched_part
            items.append({
                "article": it.query,
                "qty": it.quantity,
                "state": "matched" if mp else ("no_match" if it.state == "needs_review" else "pending"),
                "match": mp.title if mp else None,
                "brand": (mp.brand.name if (mp and mp.brand) else None),
                "supplier": getattr(mp, "supplier_name", None) if mp else None,
                "price": float(mp.price) if (mp and mp.price is not None) else None,
                "currency": getattr(mp, "currency", "USD") if mp else "USD",
            })
        return Response({
            "id": rfq.id,
            "status": rfq.status,
            "mode": rfq.mode,
            "urgency": rfq.urgency,
            "customer_name": rfq.customer_name,
            "company_name": rfq.company_name,
            "notes": rfq.notes,
            "created_at": rfq.created_at.isoformat() if rfq.created_at else None,
            "items": items,
        })
