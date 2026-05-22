import logging

from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

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
    DELETE /api/assistant/conversations/{id}/   — hard delete (cascades messages)
    """
    permission_classes = [AllowAny]  # anon → пустой список (без 401 в консоли)

    def get_queryset(self):
        # Anon: пустой queryset (UI покажет «Нет проектов / Недавнее пусто»)
        if not self.request.user.is_authenticated:
            return Conversation.objects.none()
        return Conversation.objects.filter(user=self.request.user, is_active=True)

    def get_serializer_class(self):
        if self.action == "list":
            return ConversationListSerializer
        return ConversationSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user,
                        role=detect_user_role(self.request.user, request=self.request))

    def perform_destroy(self, instance):
        # Hard delete: пользователь явно нажал «Удалить» в UI и ожидает,
        # что чат пропадёт навсегда (а не вернётся при следующем
        # order-event / WS-reconnect через find_or_create_conv,
        # который фильтрует по is_active=True). Messages удаляются по
        # CASCADE из FK в models.Message.
        instance.delete()


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


# Whitelist actions, разрешённых анонимному гостю.
# Сейчас только auth-actions — регистрация и вход проходят таким же
# chat-flow, как KYB у поставщика: форма-карточка прямо в чате.
ANON_ALLOWED_ACTIONS: set[str] = {"start_registration", "start_login"}


def _registration_required_response():
    """Карточка «зарегистрируйтесь» — для всех остальных action'ов.

    Кнопки запускают chat-action `start_registration` / `start_login`
    прямо в текущем чате (без редиректа на отдельную страницу).
    """
    return {
        "text": (
            "🔒 Чтобы продолжить — зарегистрируйтесь прямо здесь, в чате.\n"
            "Это займёт 20 секунд."
        ),
        "actions": [
            {"action": "start_registration", "label": "🚀 Зарегистрироваться"},
            {"action": "start_login",        "label": "У меня есть аккаунт"},
        ],
        "cards": [], "suggestions": [], "contextual_actions": [],
    }


# ── start_registration / start_login: chat-native auth ─────────
# Двухфазный flow, как все form-actions (KYB, claims, RFQ):
#   Phase 1 (нет confirmed=true) → возвращаем form-карточку
#   Phase 2 (есть confirmed=true + поля) → создаём user / login

def _handle_start_registration(request, params):
    """Регистрация прямо в чате. Делегирует в buyer_registration.py (ТЗ §1–§4).

    Для role=seller — упрощённый flow (минимальная форма + дальше KYB).
    Для role=buyer (default) — 8 полей по ТЗ с автопроверками.
    """
    from django.contrib.auth import login
    from . import buyer_registration as bureg

    confirmed = bool(params.get("confirmed"))
    role = (params.get("role") or "buyer").lower()

    # ── Seller — простой flow + KYB-onboarding после регистрации ─
    if role == "seller":
        return _handle_seller_quick_registration(request, params)

    # ── Buyer — 8 полей по ТЗ §1 ───────────────────────────
    if not confirmed:
        return bureg.render_form(params)
    result = bureg.attempt_register(request, params)
    if result["ok"]:
        user = result["user"]
        user.backend = "django.contrib.auth.backends.ModelBackend"
        login(request, user)
        try:
            from .order_events import notify_operator_alert
            notify_operator_alert(user_obj=user, event="user_registered",
                                  extra={"role": "buyer"})
        except Exception:
            logger.exception("notify_operator_alert user_registered failed")
    return result["response"]


def _handle_seller_quick_registration(request, params):
    """Seller-регистрация: 4 базовых поля → создание аккаунта → KYB-онбоардинг.

    Полные реквизиты компании поставщик заполняет в `start_onboarding`
    (отдельный многошаговый flow в assistant/onboarding.py).
    """
    from django.contrib.auth import login
    from marketplace.forms import RegisterForm
    from marketplace.models import UserProfile

    confirmed = bool(params.get("confirmed"))
    if not confirmed:
        return {
            "text": (
                "🏭 Регистрация поставщика — 2 шага.\n\n"
                "▸ Шаг 1 (сейчас): только аккаунт — логин, e-mail, пароль. "
                "Это нужно, чтобы вы могли сохранять прогресс.\n"
                "▸ Шаг 2 (после): KYB-анкета — реквизиты компании, ИНН/ОГРН, "
                "юр.адрес, банковский счёт, директор, сертификаты. После "
                "проверки оператором (≤24ч) сможете отвечать на RFQ и "
                "принимать заказы."
            ),
            "cards": [{
                "type": "form",
                "data": {
                    "title": "🏭 Шаг 1 из 2 · Аккаунт поставщика",
                    "submit_action": "start_registration",
                    "submit_label": "Шаг 2: к KYB-анкете →",
                    "fields": [
                        {"name": "username", "label": "Логин",
                         "required": True, "placeholder": "myshop_2026"},
                        {"name": "email", "label": "E-mail",
                         "type": "email", "required": True},
                        {"name": "password1", "label": "Пароль",
                         "type": "password", "required": True, "minlength": 8},
                        {"name": "password2", "label": "Повторите пароль",
                         "type": "password", "required": True, "minlength": 8},
                    ],
                    "fixed_params": {"confirmed": True, "role": "seller"},
                },
            }],
            "actions": [{"action": "start_login", "label": "У меня уже есть аккаунт",
                          "params": {"role": "seller"}}],
            "suggestions": [], "contextual_actions": [],
        }

    form = RegisterForm({
        "username": (params.get("username") or "").strip(),
        "email":    (params.get("email") or "").strip(),
        "password1": params.get("password1") or "",
        "password2": params.get("password2") or "",
        "role": "seller", "language": "ru",
        "first_name": "", "last_name": "", "company_name": "",
    })
    if not form.is_valid():
        errs = "\n".join(f"• {f}: {e[0]}" for f, e in form.errors.items())
        return {
            "text": "⚠️ Не получилось создать аккаунт:\n" + errs,
            "actions": [{"action": "start_registration", "label": "🔄 Попробовать снова",
                         "params": {"role": "seller"}}],
            "cards": [], "suggestions": [], "contextual_actions": [],
        }
    user = form.save(commit=False)
    user.email = params.get("email")
    user.save()
    UserProfile.objects.create(user=user, role="seller", language="ru",
                                company_name="")
    user.backend = "django.contrib.auth.backends.ModelBackend"
    login(request, user)
    try:
        from .order_events import notify_operator_alert
        notify_operator_alert(user_obj=user, event="user_registered",
                              extra={"role": "seller"})
    except Exception:
        logger.exception("notify_operator_alert user_registered failed")
    return {
        "text": (f"✅ Аккаунт создан · {user.username}\n"
                 f"Сейчас откроем KYB-анкету — нужны реквизиты компании, "
                 f"банк и директор. После проверки оператором (≤24ч) сможете "
                 f"отвечать на RFQ и принимать заказы."),
        "cards": [],
        "actions": [{"action": "reload_page", "label": "🚀 Перейти к KYB"}],
        "suggestions": [], "contextual_actions": [],
        "_post_action": "reload",
    }


def _handle_start_login(request, params):
    """Вход существующим пользователем — тоже через chat-форму.

    Принимает `role` (buyer | seller | operator) — разные сущности, разные
    кабинеты. Для buyer/seller есть кнопка регистрации, для operator её нет
    (оператора заводит только админ).
    """
    confirmed = bool(params.get("confirmed"))
    role = (params.get("role") or "buyer").lower()

    LOGIN_META = {
        "buyer":    ("👋 Вход покупателя", "С возвращением. Введите логин или e-mail."),
        "seller":   ("🏭 Вход поставщика", "Войдите в кабинет поставщика."),
        "operator": ("🛡 Вход оператора",  "Войдите в операторский кабинет."),
    }
    title, greeting = LOGIN_META.get(role, LOGIN_META["buyer"])

    if not confirmed:
        actions = []
        if role == "operator":
            # Оператора заводит только админ — никакой self-регистрации.
            pass
        elif role == "seller":
            actions.append({"action": "start_registration",
                             "label": "Создать аккаунт поставщика",
                             "params": {"role": "seller"}})
        else:
            actions.append({"action": "start_registration",
                             "label": "Создать новый аккаунт"})
        return {
            "text": greeting,
            "cards": [{
                "type": "form",
                "data": {
                    "title": title,
                    "submit_action": "start_login",
                    "submit_label": "Войти →",
                    "fields": [
                        {"name": "username", "label": "Логин или e-mail",
                         "required": True, "placeholder": "ivanov / you@company.ru"},
                        {"name": "password", "label": "Пароль",
                         "type": "password", "required": True},
                    ],
                    "fixed_params": {"confirmed": True, "role": role},
                },
            }],
            "actions": actions,
            "suggestions": [], "contextual_actions": [],
        }

    from django.contrib.auth import authenticate, get_user_model, login
    raw = (params.get("username") or "").strip()
    pwd = params.get("password") or ""
    U = get_user_model()
    # Разрешаем вход по e-mail
    if "@" in raw:
        u = U.objects.filter(email__iexact=raw).first()
        if u:
            raw = u.username
    user = authenticate(request, username=raw, password=pwd)
    if not user:
        return {
            "text": "❌ Неверный логин или пароль. Попробуйте ещё раз.",
            "actions": [{"action": "start_login", "label": "🔄 Войти снова"}],
            "cards": [], "suggestions": [], "contextual_actions": [],
        }
    login(request, user)
    return {
        "text": f"✅ Привет, {user.username}! Перезагружу чат — увидите свои данные.",
        "actions": [{"action": "reload_page", "label": "🚀 Открыть кабинет"}],
        "cards": [], "suggestions": [], "contextual_actions": [],
        "_post_action": "reload",
    }


class ActionView(APIView):
    """Execute a chat action (button click).

    POST /api/assistant/action/
    Body: {"conversation_id":"uuid","action":"create_rfq","params":{...}}
    Resp: {"text":"...","cards":[...],"actions":[...],"suggestions":[...]}

    Для anonymous гостя: пускаем только whitelisted read-only actions
    (search_parts, kb_search). Всё остальное → карточка «зарегистрируйтесь».
    """
    permission_classes = [AllowAny]  # гость может звать read-only actions

    def post(self, request):
        action = request.data.get("action") or ""
        params = request.data.get("params") or {}
        if not action:
            return Response({"error": "action required"}, status=400)

        # ── Anon gate ────────────────────────────────────────
        if not request.user.is_authenticated:
            # start_registration / start_login обрабатываем тут (им нужен
            # request для django.contrib.auth.login()).
            if action == "start_registration":
                return Response({"conversation_id": None,
                                  **_handle_start_registration(request, params)})
            if action == "start_login":
                return Response({"conversation_id": None,
                                  **_handle_start_login(request, params)})
            if action not in ANON_ALLOWED_ACTIONS:
                return Response({"conversation_id": None,
                                  **_registration_required_response()})
            # Прочие whitelisted — пока пусто; на будущее.
            # NB: execute_action импортирован в module scope (`from .rag`),
            # не делаем повторный локальный импорт — он бы сделал имя
            # local-only и стал бы причиной UnboundLocalError в auth-ветке.
            try:
                result = execute_action(
                    None, action, params, request.user, role="buyer",
                )
            except Exception as e:
                return Response({"error": str(e)}, status=500)
            return Response({"conversation_id": None, **result})

        # ── Authenticated flow (как раньше) ──────────────────
        conv_id = request.data.get("conversation_id")

        from .conv_category import category_for_action, find_or_create_conv, title_for_action
        label = (params.get("_label") or "").strip() or action

        if conv_id:
            conv = get_object_or_404(Conversation, id=conv_id, user=request.user, is_active=True)
        else:
            # Reuse существующий conv той же категории (admin/purchase/support/general)
            # вместо плодить новый на каждый клик пилюли.
            role = detect_user_role(request.user, request=request)
            conv = find_or_create_conv(
                request.user, action_name=action, role=role, action_label=label,
            )

        # Динамически обновляем title по текущему action: «Верификация · Шаг 2/5»
        new_title = title_for_action(action, label)
        if new_title and new_title[:200] != conv.title:
            conv.title = new_title[:200]
            # category может тоже измениться если пользователь сменил вид деятельности
            new_cat = category_for_action(action)
            if conv.category != new_cat and new_cat != "general":
                conv.category = new_cat
            conv.save(update_fields=["title", "category", "updated_at"])

        try:
            # role от текущего UI-toggle, не от сохранённой в conversation —
            # юзер мог переключить роль и теперь видит другую сторону.
            current_role = detect_user_role(request.user, request=request)
            result = execute_action(
                conv, action, params, request.user, role=current_role,
            )
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

    Anonymous: всегда отвечает `buyer` (без 403) — гость не может
    переключиться на seller/operator, это требует регистрации.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        if not request.user.is_authenticated:
            return Response({"role": "buyer", "override": None,
                             "anonymous": True})
        from .permissions import _normalize_override, _override_allowed
        raw = request.data.get("role")
        if raw in (None, "", "auto"):
            request.session.pop("assistant_role_override", None)
            request.session.modified = True
            new_role = detect_user_role(request.user)
            return Response({"role": new_role, "override": None})
        norm = _normalize_override(raw)
        if not norm:
            return Response({"error": f"unsupported role '{raw}'"}, status=400)
        # SECURITY P0-1: проверяем, что user реально имеет право на эту роль.
        # Buyer не может стать operator через POST {"role":"operator"}.
        if not _override_allowed(request.user, norm):
            return Response(
                {"error": "forbidden: insufficient privileges for this role",
                 "role": detect_user_role(request.user)},
                status=403,
            )
        request.session["assistant_role_override"] = norm
        request.session.modified = True
        return Response({"role": norm, "override": norm})


# ── Projects API ────────────────────────────────────────────
from .models import Project


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

    # Условные FX rates → USD. В проде брать из ЦБ или Stripe FX.
    _FX_TO_USD = {
        "USD": 1.0, "EUR": 1.08, "RUB": 0.011, "CNY": 0.135,
        "JPY": 0.0067, "GBP": 1.27,
    }

    def get(self, request, rfq_id):

        from marketplace.models import RFQ, Notification, Quote
        from django.db.models import Q as _Q
        from django.http import Http404
        rfq = get_object_or_404(RFQ, id=rfq_id)

        # SECURITY (IDOR fix): доступ к RFQ имеют только:
        #  • владелец (создатель RFQ),
        #  • продавец-адресат (получивший Notification по этому RFQ),
        #  • staff/superuser.
        # Для остальных — 404 (не 403), чтобы не утекало само наличие RFQ.
        is_owner = (rfq.created_by_id == request.user.id)
        is_staff = bool(getattr(request.user, "is_staff", False))
        is_recipient = False
        if not (is_owner or is_staff):
            is_recipient = Notification.objects.filter(
                kind="rfq", user_id=request.user.id,
            ).filter(
                _Q(url__contains=f"rfq={rfq.id}") | _Q(url__contains=f"/rfq/{rfq.id}/")
            ).exists()
        if not (is_owner or is_staff or is_recipient):
            raise Http404("RFQ not found")

        # Для не-владельцев скрываем чувствительные поля покупателя
        redact_pii = not (is_owner or is_staff)

        items = []
        total_usd = 0.0
        for it in rfq.items.select_related("matched_part__brand").all():
            mp = it.matched_part
            price = float(mp.price) if (mp and mp.price is not None) else None
            ccy = (getattr(mp, "currency", "USD") if mp else "USD") or "USD"
            qty = it.quantity or 1
            if price is not None:
                total_usd += price * qty * self._FX_TO_USD.get(ccy.upper(), 1.0)
            items.append({
                "article": it.query,
                "qty": qty,
                "state": "matched" if mp else ("no_match" if it.state == "needs_review" else "pending"),
                "match": mp.title if mp else None,
                "brand": (mp.brand.name if (mp and mp.brand) else None),
                "supplier": getattr(mp, "supplier_name", None) if mp else None,
                "price": price,
                "currency": ccy,
            })

        # Quotes-аналитика
        quotes = Quote.objects.filter(rfq=rfq, direction="seller_to_buyer")
        quotes_count = quotes.values_list("seller_id", flat=True).distinct().count()
        # Supplier reach: сколько уведомлений было разослано.
        # _notify пишет url'ы двух форматов:
        #   /chat/?rfq=<id>           — общая ссылка (старый формат)
        #   /chat/rfq/<id>/?source=…  — детальная страница (новый формат)
        # Считаем оба варианта, по distinct user_id.
        sent_count = (
            Notification.objects.filter(kind="rfq")
            .filter(_Q(url__contains=f"rfq={rfq.id}") | _Q(url__contains=f"/rfq/{rfq.id}/"))
            .values_list("user_id", flat=True).distinct().count()
        )

        # Состояние «что делать дальше»
        if rfq.status == "cancelled":
            stage = "cancelled"
        elif rfq.status == "needs_review":
            stage = "needs_review"
        elif quotes_count > 0:
            stage = "quotes_received"  # есть котировки — выбирать
        elif sent_count > 0:
            stage = "awaiting_quotes"  # разослан, ждём
        else:
            stage = "draft"            # создан, не разослан

        # has_priced — есть ли хоть одна позиция с известной ценой? Если нет —
        # total_usd=0 не «бюджет», а «оценим после котировок».
        has_priced = any(it["price"] is not None for it in items)

        # Classifier reason: парсим из notes (записан create_rfq как «Mode: …»)
        classifier_reason = ""
        if rfq.notes:
            for line in (rfq.notes or "").split(" | "):
                line = line.strip()
                if line.lower().startswith("mode:"):
                    classifier_reason = line.split(":", 1)[1].strip()
                    break

        payload = {
            "id": rfq.id,
            "status": rfq.status,
            "stage": stage,
            "mode": rfq.mode,            # 'auto' | 'semi' | 'manual'
            "mode_reason": classifier_reason,
            "urgency": rfq.urgency,
            "customer_name": rfq.customer_name,
            "company_name": rfq.company_name,
            "notes": rfq.notes,
            "created_at": rfq.created_at.isoformat() if rfq.created_at else None,
            "items": items,
            "total_usd": round(total_usd, 2),
            "has_priced": has_priced,
            "quotes_count": quotes_count,
            "sent_count": sent_count,
            "is_owner": is_owner,
        }
        # PII-protection: продавцу-адресату не видны имя/компания/заметки покупателя
        if redact_pii:
            payload["customer_name"] = None
            payload["company_name"] = None
            payload["notes"] = None
        return Response(payload)


# ──────────────────────────────────────────────────────────
# Notifications inbox (bell + dropdown in chat-first UI)
# ──────────────────────────────────────────────────────────
class DrawingFileView(APIView):
    """GET /api/assistant/drawings/<id>/file/?action=view|download

    Контролируемая выдача файла чертежа: проверяет access_level + KYB +
    payment_status, добавляет watermark, пишет в DrawingAccessLog.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, drawing_id):
        from marketplace.models import Drawing

        from .drawings_access import apply_watermark_url, can_access, record_access
        try:
            drawing = Drawing.objects.get(id=drawing_id)
        except Drawing.DoesNotExist:
            return Response({"ok": False, "error": "drawing not found"}, status=404)
        action = (request.GET.get("action") or "view").strip()
        if action not in ("view", "download"):
            action = "view"

        allowed, reason = can_access(request.user, drawing)
        if not allowed:
            record_access(request.user, drawing, "denied", request=request, note=reason)
            return Response({"ok": False, "error": reason}, status=403)

        record_access(request.user, drawing, action, request=request)
        wm_url = apply_watermark_url(drawing.file_url, request.user, drawing)
        return Response({
            "ok": True,
            "drawing_id": drawing.id,
            "title": drawing.title,
            "file_url": wm_url,
            "file_format": drawing.file_format,
            "access_level": drawing.access_level,
            "reason": reason,
        })


class NotificationListView(APIView):
    """GET /api/assistant/notifications/?unread=1&limit=20

    Returns recent notifications for the user. Pair with WS-push
    (handled in consumers.py) for realtime updates.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from marketplace.models import Notification
        unread_only = str(request.GET.get("unread", "")).strip() in ("1", "true", "yes")
        try:
            limit = max(1, min(100, int(request.GET.get("limit", 20))))
        except (TypeError, ValueError):
            limit = 20
        qs = Notification.objects.filter(user=request.user)
        if unread_only:
            qs = qs.filter(is_read=False)
        items = list(qs.order_by("-created_at")[:limit])
        unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
        return Response({
            "unread_count": unread_count,
            "items": [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "title": n.title,
                    "body": n.body,
                    "url": n.url,
                    "is_read": n.is_read,
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                }
                for n in items
            ],
        })


from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt


@method_decorator(csrf_exempt, name="dispatch")
class PaymentsWebhookView(APIView):
    """POST /api/assistant/payments/webhook/

    Stripe-style webhook receiver. Принимает JSON-event с полями
    {type, data} и роутит через assistant.payments.dispatch_webhook().

    HMAC: если задан STRIPE_WEBHOOK_SECRET — проверяем Stripe-Signature.
    Если не задан — demo-режим, верим на слово (фронт-демо без реального
    Stripe). Это безопасно потому что body всё равно идёт через
    dispatch_webhook → registered handlers, без права писать в БД от
    лица пользователя.
    """
    permission_classes = []
    authentication_classes = []

    def post(self, request):
        from .payments import dispatch_webhook
        from .payments_engines import verify_webhook_signature
        signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        if not verify_webhook_signature(request.body, signature):
            return Response({"received": False, "error": "invalid signature"}, status=400)
        result = dispatch_webhook(request.data or {})
        return Response(result)


class NotificationMarkReadView(APIView):
    """POST /api/assistant/notifications/<id>/read/  → mark single as read.

    POST /api/assistant/notifications/read-all/   → mark all as read.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, notif_id=None):
        from marketplace.models import Notification
        if notif_id is None:
            updated = Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
            return Response({"ok": True, "updated": updated, "unread_count": 0})
        n = get_object_or_404(Notification, id=notif_id, user=request.user)
        if not n.is_read:
            n.is_read = True
            n.save(update_fields=["is_read"])
        unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
        return Response({"ok": True, "id": n.id, "unread_count": unread_count})
