import logging

from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
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

    PIVOT 2026-05-28: AllowAny — anonymous buyer может писать в чат
    (поиск, RFQ, котировки). Регистрация триггерится при pay_reserve.
    Conversation для anon не сохраняется в БД (нет user_id), но action'ы
    выполняются stateless через execute_action(None, ...).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        ser = ChatRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        conv_id = ser.validated_data.get("conversation_id")
        message = ser.validated_data["message"]

        # ── Anon path: только fast-path, без БД, без LLM ──
        # Анон-юзер пишет parts list / запрос → fast_path определяет
        # intent (create_rfq, search_parts) → выполняем напрямую через
        # execute_action(conv=None, ...). LLM не зовём — экономим токены
        # и не сохраняем Conversation/Message (нет user_id для FK).
        if not request.user.is_authenticated:
            from . import fast_path
            from . import actions as action_executor
            fp_match = fast_path.match(message, "buyer")
            if not fp_match:
                # Не распознали intent — предлагаем зарегистрироваться или
                # уточнить запрос примерами OEM/RFQ
                return Response({
                    "conversation_id": None,
                    "response": _(
                        "Я не понял запрос. Попробуйте:\n"
                        "• Вставьте список артикулов (по одному на строке)\n"
                        "• Загрузите файл (.xlsx/.pdf) с позициями\n"
                        "• Нажмите «Создать RFQ» или «Открытые RFQ»\n\n"
                        "Если хотите вести историю чата — зарегистрируйтесь."
                    ),
                    "cards": [],
                    "actions": [
                        {"action": "start_registration", "label": _("🚀 Зарегистрироваться")},
                        {"action": "start_login",        "label": _("Войти")},
                    ],
                    "contextual_actions": [], "context_refs": [],
                    "suggestions": [], "message_id": None,
                })
            action_name, params, rule_name = fp_match
            # Block payment-actions для anon (триггер реги)
            if action_name in ANON_BLOCKED_PAYMENT_ACTIONS:
                request.session["pending_action"] = {"action": action_name, "params": params}
                request.session.modified = True
                resp = _payment_requires_registration_response(action_name, params)
                return Response({"conversation_id": None, **{
                    "response": resp.get("text", ""),
                    "cards": resp.get("cards", []),
                    "actions": resp.get("actions", []),
                    "contextual_actions": [], "context_refs": [],
                    "suggestions": [], "message_id": None,
                }})
            if action_name not in ANON_ALLOWED_ACTIONS:
                resp = _registration_required_response()
                return Response({"conversation_id": None, **{
                    "response": resp.get("text", ""),
                    "cards": resp.get("cards", []),
                    "actions": resp.get("actions", []),
                    "contextual_actions": [], "context_refs": [],
                    "suggestions": [], "message_id": None,
                }})
            try:
                result = execute_action(None, action_name, {**params, "_request": request}, request.user, role="buyer")
            except Exception as e:
                logger.exception("anon fast_path action failed")
                return Response({"error": str(e)},
                                 status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            return Response({
                "conversation_id": None,
                "response": result.get("text", ""),
                "cards": result.get("cards", []),
                "actions": result.get("actions", []),
                "contextual_actions": result.get("contextual_actions", []),
                "context_refs": [], "suggestions": result.get("suggestions", []),
                "message_id": None,
            })

        # ── Authenticated flow (как раньше) ──
        if conv_id:
            conv = get_object_or_404(
                Conversation, id=conv_id, user=request.user, is_active=True
            )
        else:
            conv = Conversation.objects.create(
                user=request.user, role=detect_user_role(request.user, request=request)
            )

        try:
            result = process_query_sync(conv, message, request.user,
                                       ui_lang=getattr(request, "LANGUAGE_CODE", "ru"))
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
# PIVOT 2026-05-28: покупатель может работать без регистрации
# до момента оплаты резерва. Регистрация триггерится при pay_reserve
# (и других денежных действиях) — там просим email/телефон/пароль.
ANON_ALLOWED_ACTIONS: set[str] = {
    # Auth-actions
    "start_registration", "start_login",
    # Discovery — поиск, база знаний, аналитика товаров
    "search_parts", "kb_search",
    "compare_products", "compare_suppliers", "top_suppliers",
    "buyer_best_offers", "buyer_offer_compare",
    "calc_part_logistics",
    # Аналитика спецификации (upload xlsx → AI-маппинг)
    "analyze_spec", "upload_parts_list",
    # RFQ flow — создать запрос, получить котировки, посмотреть
    "create_rfq", "get_rfq_status", "view_rfq_quotes", "view_quote",
    "compare_quotes", "ask_about_rfq",
    "generate_proposal",
    # Accept quote (только preview-фаза, confirmed=False).
    # Phase 2 (confirmed=True → создаёт Order → требует payment) блокируется
    # внутри handler'а в _block_anon_payment().
    "accept_quote",
    # Comms / help
    "ask_operator", "go_home",
}

# Actions требующие денег / payment intent — для anon триггерим registration.
ANON_BLOCKED_PAYMENT_ACTIONS: set[str] = {
    "pay_reserve", "pay_final", "pay_remaining",
    "confirm_kp_and_reserve", "auto_accept_and_pay_reserve",
    "submit_topup", "create_payment_intent",
    # quick_order — это оформление заказа (клик по базису доставки): аноним
    # должен получить контекстный close «создать аккаунт и оплатить» + resume
    # после регистрации, а не обобщённую карточку.
    "quick_order",
}


def _registration_required_response():
    """Карточка «зарегистрируйтесь» — для всех остальных action'ов.

    Кнопки запускают chat-action `start_registration` / `start_login`
    прямо в текущем чате (без редиректа на отдельную страницу).
    """
    return {
        "text": _(
            "🔒 Чтобы продолжить — зарегистрируйтесь прямо здесь, в чате.\n"
            "Это займёт 20 секунд."
        ),
        "actions": [
            {"action": "start_registration", "label": _("🚀 Зарегистрироваться")},
            {"action": "start_login",        "label": _("У меня есть аккаунт")},
        ],
        "cards": [], "suggestions": [], "contextual_actions": [],
    }


def _payment_requires_registration_response(action_name: str, params: dict):
    """Карточка для anon-юзера который пытается оплатить.

    Объясняет ЗАЧЕМ нужна регистрация именно сейчас (для приёма платежа
    и оформления заказа). Pending action сохранён в session — после
    регистрации фронт его сам replays.
    """
    return {
        "text": _(
            "💳 Чтобы оформить оплату — нужен аккаунт.\n"
            "Это нужно для:\n"
            "• приёма и возврата средств (резерв 10%)\n"
            "• юридического оформления заказа\n"
            "• трекинга вашей доставки в личном кабинете\n\n"
            "Регистрация в чате — 30 секунд. Все данные что вы ввели — "
            "RFQ, выбранная котировка — сохранятся."
        ),
        "actions": [
            {"action": "start_registration", "label": _("🚀 Создать аккаунт и оплатить"),
             "params": {"role": "buyer", "_resume": action_name}},
            {"action": "start_login",        "label": _("У меня уже есть аккаунт"),
             "params": {"role": "buyer", "_resume": action_name}},
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
        # Anonymous-flow: перепривязываем созданные ранее RFQ (created_by=None)
        # к новому user'у через email match или session_key.
        try:
            _attach_anonymous_rfqs_to_user(request, user)
        except Exception:
            logger.exception("attach anonymous RFQs failed")
        # Resume pending action (если до регистрации юзер нажал pay_reserve)
        pending = request.session.pop("pending_action", None)
        if pending and pending.get("action"):
            # Возвращаем resume-карточку: фронт сам кликнет action в чате
            return {
                "text": (result["response"].get("text", "") + "\n\n"
                         + _("▶ Продолжаю оформление заказа...")),
                "actions": [{
                    "action": pending["action"],
                    "label": _("▶ Продолжить"),
                    "params": pending.get("params", {}),
                }],
                "cards": [], "suggestions": [], "contextual_actions": [],
                "_post_action": "auto_resume",
            }
    return result["response"]


def _attach_anonymous_rfqs_to_user(request, user):
    """После регистрации/входа привязываем anon-RFQ к новому user'у.
    Скоуп: только те RFQ, чьи id записаны в session (anon_rfq_ids создаёт
    create_rfq в actions.py) — иначе была бы cross-tenant утечка чужих
    гостевых заявок. created_by__isnull=True оставлен как доп. защита.
    """
    from marketplace.models import RFQ
    # Берём только id из текущей сессии — не трогаем чужие анон-RFQ
    anon_ids = (request.session.pop("anon_rfq_ids", []) if hasattr(request, "session") else [])
    if not anon_ids:
        return
    qs = RFQ.objects.filter(
        id__in=anon_ids,
        created_by__isnull=True,
        customer_email="anon@chat.local",
    )
    updated = qs.update(
        created_by=user,
        customer_name=user.get_full_name() or user.username,
        customer_email=user.email or f"{user.username}@chat.local",
    )
    if updated:
        logger.info(f"Attached {updated} anonymous RFQs to user {user.username}")


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
            "text": _(
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
                    "title": _("🏭 Шаг 1 из 2 · Аккаунт поставщика"),
                    "submit_action": "start_registration",
                    "submit_label": _("Шаг 2: к KYB-анкете →"),
                    "fields": [
                        {"name": "username", "label": _("Логин"),
                         "required": True, "placeholder": "myshop_2026"},
                        {"name": "email", "label": _("E-mail"),
                         "type": "email", "required": True},
                        {"name": "password1", "label": _("Пароль"),
                         "type": "password", "required": True, "minlength": 8},
                        {"name": "password2", "label": _("Повторите пароль"),
                         "type": "password", "required": True, "minlength": 8},
                    ],
                    "fixed_params": {"confirmed": True, "role": "seller"},
                },
            }],
            "actions": [{"action": "start_login", "label": _("У меня уже есть аккаунт"),
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
            "text": _("⚠️ Не получилось создать аккаунт:\n") + errs,
            "actions": [{"action": "start_registration", "label": _("🔄 Попробовать снова"),
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
        "text": (_("✅ Аккаунт создан · %(u)s\n"
                   "Сейчас откроем KYB-анкету — нужны реквизиты компании, "
                   "банк и директор. После проверки оператором (≤24ч) сможете "
                   "отвечать на RFQ и принимать заказы.") % {"u": user.username}),
        "cards": [],
        "actions": [{"action": "reload_page", "label": _("🚀 Перейти к KYB")}],
        "suggestions": [], "contextual_actions": [],
        "_post_action": "reload",
    }


def _handle_switch_role_login(request, params):
    """Переключение на аккаунт другой роли через chat-форму с паролем.

    Двухфазный flow (как start_login):
      Phase 1: возвращает form-карточку с editable username (prefilled demo_<role>) + password
      Phase 2: authenticate + проверка что user.role совпадает + login + reload_page

    Если аккаунта с такой ролью нет — предлагается зарегистрировать
    (кроме операторских — операторов заводит только админ).
    """
    role = (params.get("role") or "").lower()
    DEMO_USERNAMES = {
        "buyer":    "demo_buyer",
        "seller":   "demo_seller",
        "operator": "demo_operator",
    }
    if role not in DEMO_USERNAMES:
        return {"text": _("⚠️ Неизвестная роль: %(r)s") % {"r": role},
                "cards": [], "actions": [], "suggestions": [], "contextual_actions": []}
    suggested_username = DEMO_USERNAMES[role]

    ROLE_META = {
        "buyer":    (_("👋 Войти как Покупатель"), _("Введите логин и пароль аккаунта покупателя.")),
        "seller":   (_("🏭 Войти как Поставщик"),  _("Введите логин и пароль аккаунта поставщика.")),
        "operator": (_("🛡 Войти как Оператор"),   _("Введите логин и пароль операторского аккаунта.")),
    }
    title, greeting = ROLE_META[role]
    confirmed = bool(params.get("confirmed"))

    if not confirmed:
        # Регистрационная кнопка — buyer/seller сами создают аккаунт,
        # operator заводит только админ.
        reg_actions = []
        if role == "buyer":
            reg_actions.append({"action": "start_registration",
                                "label": _("📝 Создать аккаунт покупателя"),
                                "params": {"role": "buyer"}})
        elif role == "seller":
            reg_actions.append({"action": "start_registration",
                                "label": _("🏭 Создать аккаунт поставщика"),
                                "params": {"role": "seller"}})
        elif role == "operator":
            reg_actions.append({"action": "contact_operator",
                                "label": _("📨 Запросить операторский доступ у админа"),
                                "params": {"topic": "operator_access"}})
        return {
            "text": greeting,
            "cards": [{
                "type": "form",
                "data": {
                    "title": title,
                    "intent": _(
                        "Каждая роль = отдельный аккаунт. Текущая сессия будет переключена. "
                        "Если аккаунта нет — создайте его кнопкой ниже формы."
                    ),
                    "submit_action": "switch_role_login",
                    "submit_label": _("Войти →"),
                    "fields": [
                        {"name": "username", "label": _("Логин или e-mail"),
                         "value": suggested_username,
                         "placeholder": "ivanov / you@company.ru",
                         "required": True,
                         "hint": _("Подставлен demo-аккаунт. Если у вас свой — замените.")},
                        {"name": "password", "label": _("Пароль"),
                         "type": "password", "required": True,
                         "placeholder": _("Введите пароль")},
                    ],
                    "fixed_params": {"confirmed": True, "role": role},
                },
            }],
            "actions": reg_actions,
            "suggestions": [], "contextual_actions": [],
        }

    # Phase 2: проверка пароля + login
    from django.contrib.auth import authenticate, get_user_model, login
    raw = (params.get("username") or "").strip()
    pwd = params.get("password") or ""
    U = get_user_model()
    # Разрешаем вход по e-mail
    if "@" in raw:
        u = U.objects.filter(email__iexact=raw).first()
        if u:
            raw = u.username
    if not raw:
        return {
            "text": _("❌ Логин не указан."),
            "actions": [{"action": "switch_role_login",
                         "label": _("🔄 Попробовать ещё раз"),
                         "params": {"role": role}}],
            "cards": [], "suggestions": [], "contextual_actions": [],
        }
    user = authenticate(request, username=raw, password=pwd)
    if not user:
        # Различаем «нет аккаунта» vs «неверный пароль»
        exists = U.objects.filter(username=raw).exists()
        if not exists:
            reg_btn = []
            if role == "buyer":
                reg_btn.append({"action": "start_registration",
                                "label": _("📝 Создать аккаунт покупателя"),
                                "params": {"role": "buyer"}})
            elif role == "seller":
                reg_btn.append({"action": "start_registration",
                                "label": _("🏭 Создать аккаунт поставщика"),
                                "params": {"role": "seller"}})
            return {
                "text": _("⚠️ Аккаунт «%(u)s» не найден. Зарегистрируйтесь или укажите другой логин.") % {"u": raw},
                "actions": reg_btn + [
                    {"action": "switch_role_login",
                     "label": _("🔄 Ввести другой логин"),
                     "params": {"role": role}},
                ],
                "cards": [], "suggestions": [], "contextual_actions": [],
            }
        return {
            "text": _("❌ Неверный пароль для «%(u)s». Попробуйте ещё раз.") % {"u": raw},
            "actions": [{"action": "switch_role_login",
                         "label": _("🔄 Войти снова"),
                         "params": {"role": role}}],
            "cards": [], "suggestions": [], "contextual_actions": [],
        }
    # Verify the user has the requested role (no privilege escalation)
    actual_role = detect_user_role(user)
    actual_norm = "operator" if actual_role.startswith("operator") else actual_role
    if actual_norm != role:
        return {
            "text": (_("⚠️ Аккаунт «%(u)s» имеет роль «%(actual)s», "
                       "а вы пытались войти как «%(role)s». Войдите под другим логином.")
                     % {"u": user.username, "actual": actual_norm, "role": role}),
            "actions": [{"action": "switch_role_login",
                         "label": _("🔄 Ввести другой логин"),
                         "params": {"role": role}}],
            "cards": [], "suggestions": [], "contextual_actions": [],
        }
    # Очищаем старый session-override чтобы UI взял правильную роль из identity
    request.session.pop("assistant_role_override", None)
    login(request, user)
    return {
        "text": _("✅ Вы вошли как «%(u)s». Перезагружаю кабинет...") % {"u": user.username},
        "actions": [{"action": "reload_page", "label": _("🚀 Открыть кабинет")}],
        "cards": [], "suggestions": [], "contextual_actions": [],
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
        "buyer":    (_("👋 Вход покупателя"), _("С возвращением. Введите логин или e-mail.")),
        "seller":   (_("🏭 Вход поставщика"), _("Войдите в кабинет поставщика.")),
        "operator": (_("🛡 Вход оператора"),  _("Войдите в операторский кабинет.")),
    }
    title, greeting = LOGIN_META.get(role, LOGIN_META["buyer"])

    if not confirmed:
        actions = []
        if role == "operator":
            # Оператора заводит только админ — никакой self-регистрации.
            pass
        elif role == "seller":
            actions.append({"action": "start_registration",
                             "label": _("Создать аккаунт поставщика"),
                             "params": {"role": "seller"}})
        else:
            actions.append({"action": "start_registration",
                             "label": _("Создать новый аккаунт")})
        return {
            "text": greeting,
            "cards": [{
                "type": "form",
                "data": {
                    "title": title,
                    "submit_action": "start_login",
                    "submit_label": _("Войти →"),
                    "fields": [
                        {"name": "username", "label": _("Логин или e-mail"),
                         "required": True, "placeholder": "ivanov / you@company.ru"},
                        {"name": "password", "label": _("Пароль"),
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
            "text": _("❌ Неверный логин или пароль. Попробуйте ещё раз."),
            "actions": [{"action": "start_login", "label": _("🔄 Войти снова")}],
            "cards": [], "suggestions": [], "contextual_actions": [],
        }
    login(request, user)
    # Resume pending payment action (anon → клик pay_reserve → login → resume)
    try:
        _attach_anonymous_rfqs_to_user(request, user)
    except Exception:
        logger.exception("attach anonymous RFQs failed on login")
    pending = request.session.pop("pending_action", None)
    if pending and pending.get("action"):
        return {
            "text": _("✅ Вы вошли как «%(u)s». ▶ Продолжаю оформление заказа...") % {"u": user.username},
            "actions": [{
                "action": pending["action"],
                "label": _("▶ Продолжить оформление"),
                "params": pending.get("params", {}),
            }],
            "cards": [], "suggestions": [], "contextual_actions": [],
            "_post_action": "auto_resume",
        }
    return {
        "text": _("✅ Привет, %(u)s! Перезагружу чат — увидите свои данные.") % {"u": user.username},
        "actions": [{"action": "reload_page", "label": _("🚀 Открыть кабинет")}],
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

        # Клиентский IP (XFF-aware) для ленты важных событий админа: экшены
        # создания заказа/RFQ читают params['_client_ip'] и пишут его в
        # ActivityEvent. Underscore-ключ = UI/meta-параметр (как _label/_url).
        if isinstance(params, dict):
            _xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
            params["_client_ip"] = (
                _xff.split(",")[0].strip() if _xff
                else request.META.get("REMOTE_ADDR", "")
            )[:64]

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
            # Payment-actions → принудительная регистрация с intent.
            if action in ANON_BLOCKED_PAYMENT_ACTIONS:
                # Сохраняем intent в session: после регистрации resume этого action
                request.session["pending_action"] = {"action": action, "params": params}
                request.session.modified = True
                return Response({"conversation_id": None,
                                  **_payment_requires_registration_response(action, params)})
            # Phase 2 accept_quote (confirmed=True) тоже требует регистрации,
            # потому что создаёт Order и сразу ведёт к pay_reserve.
            if action == "accept_quote" and bool(params.get("confirmed")):
                request.session["pending_action"] = {"action": action, "params": params}
                request.session.modified = True
                return Response({"conversation_id": None,
                                  **_payment_requires_registration_response(action, params)})
            if action not in ANON_ALLOWED_ACTIONS:
                return Response({"conversation_id": None,
                                  **_registration_required_response()})
            # Прочие whitelisted — выполняем как обычный buyer
            try:
                result = execute_action(
                    None, action, {**params, "_request": request}, request.user, role="buyer",
                )
            except Exception as e:
                return Response({"error": str(e)}, status=500)
            return Response({"conversation_id": None, **result})

        # ── Authenticated flow ──────────────────────────────
        # Special-case: switch_role_login (нужен request для login/session).
        # Это смена аккаунта через chat-форму с паролем (вместо JS-prompt).
        if action == "switch_role_login":
            return Response({"conversation_id": None,
                             **_handle_switch_role_login(request, params)})

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
                conv, action, {**params, "_request": request}, request.user, role=current_role,
            )
        except Exception as e:
            return Response({"error": str(e)}, status=500)

        # Авто-reload после переключения view-as / выхода — чтобы middleware
        # подхватил новую сессию и подменил request.user.
        if action in ("op_view_as_supplier", "op_exit_view_as"):
            result = {**result, "_post_action": "reload"}
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
            _lazy("Покажи мои активные RFQ"),
            _lazy("Какие гусеничные цепи есть для Komatsu?"),
            _lazy("Статус моих заказов за последний месяц"),
            _lazy("Сравни поставщиков по SLA"),
        ],
        "seller": [
            _lazy("Новые RFQ за сегодня"),
            _lazy("Какие запчасти ищут чаще всего?"),
            _lazy("Мои просроченные заказы"),
            _lazy("KPI за этот месяц"),
        ],
        "operator_logist": [
            _lazy("Какие отгрузки сейчас в пути?"),
            _lazy("Есть ли нарушения SLA?"),
            _lazy("Контейнеры на таможне"),
        ],
        "operator_customs": [
            _lazy("Грузы ожидающие растаможки"),
            _lazy("Документы для контейнера"),
            _lazy("Просроченные декларации"),
        ],
        "operator_payment": [
            _lazy("Неоплаченные инвойсы"),
            _lazy("Просроченные платежи"),
            _lazy("Эскроу-счета по заказам"),
        ],
        "operator_manager": [
            _lazy("Конверсия RFQ → заказ за месяц"),
            _lazy("Топ покупатели по выручке"),
            _lazy("Неактивные клиенты"),
        ],
        "admin": [
            _lazy("Метрики платформы за неделю"),
            _lazy("Поставщики на верификации"),
            _lazy("Просроченные SLA"),
        ],
    }

    def get(self, request):
        role = request.query_params.get("role") or detect_user_role(request.user, request=request)
        return Response({
            "role": role,
            "suggestions": self.SUGGESTIONS.get(role, self.SUGGESTIONS["buyer"]),
        })


class WidgetConfigView(APIView):
    """Initial config for the chat widget — role, suggestions, latest conv.

    B-14 fix: ранее [IsAuthenticated] → анонимы получали 403 на старте
    /chat/, фронт падал в catch и показывал «Загрузка...» вечно.
    Теперь AllowAny: анонимам возвращаем guest-конфиг (role=buyer,
    user_name='Гость', latest=None) — UI рендерит welcome без ошибок.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        if not request.user.is_authenticated:
            return Response({
                "role": "buyer",
                "role_override": None,
                "user_name": _("Гость"),
                "suggestions": SuggestView.SUGGESTIONS.get("buyer", []),
                "latest_conversation_id": None,
                "anonymous": True,
            })
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
    """POST /api/assistant/role/  body: {"role": "buyer"|"seller"|"operator", "password": "..."}

    PIVOT 2026-05-27: переключение ролей теперь = смена аккаунта.
    Каждая роль = отдельный пользователь. Чтобы переключиться, нужно
    ввести пароль аккаунта, владеющего этой ролью.

    Для демо-юзеров маппинг username:
      buyer    → demo_buyer
      seller   → demo_seller
      operator → demo_operator

    Anonymous: всегда отвечает `buyer` (без 403) — гость не может
    переключиться на seller/operator, это требует регистрации.
    """
    permission_classes = [AllowAny]

    # Демо-мэппинг роли на username. В проде заменить на per-user mapping
    # (один пользователь может иметь несколько аккаунтов под разными ролями).
    _DEMO_ROLE_TO_USERNAME = {
        "buyer":    "demo_buyer",
        "seller":   "demo_seller",
        "operator": "demo_operator",
    }

    def post(self, request):
        from django.contrib.auth import authenticate, login, get_user_model
        from .permissions import _normalize_override

        if not request.user.is_authenticated:
            return Response({"role": "buyer", "override": None, "anonymous": True})

        raw = request.data.get("role")
        password = request.data.get("password") or ""
        norm = _normalize_override(raw)
        if not norm or norm not in self._DEMO_ROLE_TO_USERNAME:
            return Response({"error": f"unsupported role '{raw}'"}, status=400)

        # Уже в этой роли — ничего не делаем
        current_role = detect_user_role(request.user)
        current_normalized = "operator" if current_role.startswith("operator") else current_role
        if current_normalized == norm:
            return Response({"role": current_role, "override": None, "no_change": True})

        # Operator → operator-сабролей (logist/customs/payment/manager) — это
        # UI-detail, не смена аккаунта. Разрешаем без пароля.
        from .permissions import _override_allowed
        if current_normalized == "operator" and norm == "operator":
            return Response({"role": current_role, "override": None, "no_change": True})

        target_username = self._DEMO_ROLE_TO_USERNAME[norm]
        if not password:
            # Без пароля — возвращаем "требуется пароль" + куда отправлять следующий запрос
            return Response({
                "password_required": True,
                "target_username": target_username,
                "target_role": norm,
            }, status=401)

        # Проверяем пароль через стандартный authenticate
        target_user = authenticate(request, username=target_username, password=password)
        if not target_user:
            return Response({
                "error": _("Неверный пароль"),
                "target_username": target_username,
                "target_role": norm,
            }, status=403)

        # Очищаем role-override (он больше не используется)
        request.session.pop("assistant_role_override", None)
        # Logout + login as target
        login(request, target_user)
        request.session.modified = True
        return Response({
            "role": detect_user_role(target_user),
            "username": target_user.username,
            "switched": True,
        })


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
            name=data.get("name", _("Новый проект"))[:200],
            code=data.get("code", "")[:50],
            customer=data.get("customer", "")[:200],
            tags=data.get("tags", []),
            description=data.get("description", ""),
            dot_color=data.get("dot_color", "green"),
        )
        return Response({"id": str(p.id), "name": p.name}, status=201)


class ProjectUpdateView(APIView):
    """PATCH-обновление полей проекта (name, code, customer, tags, dot_color, description)."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, project_id):
        p = get_object_or_404(Project, id=project_id, owner=request.user, is_active=True)
        data = request.data or {}
        allowed = ("name", "code", "customer", "description", "dot_color")
        for f in allowed:
            if f in data:
                setattr(p, f, (data.get(f) or "")[:500] if f != "name" else (data.get(f) or _("Новый проект"))[:200])
        if "tags" in data and isinstance(data.get("tags"), list):
            p.tags = data["tags"]
        p.save()
        return Response({"id": str(p.id), "name": p.name})


class KYBDocUploadView(APIView):
    """POST multipart/form-data 'file' → сохраняет файл в CompanyVerification.doc_<kind>.
    kind ∈ ('dealership', 'bank'). После загрузки документ ждёт проверки оператора —
    бейдж «Официальный дилер» выдаётся отдельным действием op_kyb_approve_doc."""
    permission_classes = [IsAuthenticated]
    from rest_framework.parsers import MultiPartParser, FormParser
    parser_classes = [MultiPartParser, FormParser]

    KIND_FIELD = {"dealership": "doc_dealership", "bank": "doc_bank"}

    def post(self, request, kind):
        if kind not in self.KIND_FIELD:
            return Response({"error": _("Неизвестный тип документа: %(kind)s") % {"kind": kind}}, status=400)
        f = request.FILES.get("file")
        if not f:
            return Response({"error": _("Файл не приложен")}, status=400)
        # Базовая валидация: размер ≤10MB, разрешённые расширения
        MAX_SIZE = 10 * 1024 * 1024
        if (f.size or 0) > MAX_SIZE:
            return Response({"error": _("Файл слишком большой (%(kb)s КБ, лимит 10 МБ)") % {"kb": f.size // 1024}}, status=400)
        name = f.name or "document"
        ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
        if ext not in ("pdf", "png", "jpg", "jpeg", "heic"):
            return Response({"error": _("Неподдерживаемое расширение «.%(ext)s». Используйте PDF, PNG или JPG.") % {"ext": ext}},
                             status=400)
        # FIX (HIGH): magic-byte валидация — защита от .exe, переименованных в .pdf.
        # Проверяем первые байты файла на соответствие реальному формату.
        try:
            head = f.read(12); f.seek(0)
            valid_magic = (
                (ext == "pdf" and head.startswith(b"%PDF"))
                or (ext in ("png",) and head.startswith(b"\x89PNG\r\n\x1a\n"))
                or (ext in ("jpg", "jpeg") and head[:3] == b"\xFF\xD8\xFF")
                or (ext == "heic" and (b"ftyp" in head[:12]))
            )
            if not valid_magic:
                return Response({
                    "error": _("Содержимое файла не соответствует расширению .%(ext)s. "
                               "Возможно, файл переименован — отправьте оригинал.") % {"ext": ext}
                }, status=400)
        except Exception:
            return Response({"error": _("Не удалось проверить содержимое файла.")}, status=400)
        try:
            from marketplace.models import CompanyVerification
            kyb, _created = CompanyVerification.objects.get_or_create(user=request.user)
            field_name = self.KIND_FIELD[kind]
            setattr(kyb, field_name, f)
            kyb.save(update_fields=[field_name])
            saved = getattr(kyb, field_name)
            return Response({
                "ok": True,
                "kind": kind,
                "name": name,
                "size_kb": round((f.size or 0) / 1024, 1),
                "url": saved.url if saved else None,
            }, status=201)
        except Exception as e:
            return Response({"error": str(e)}, status=500)


class ProjectDocumentUploadView(APIView):
    """POST multipart/form-data 'file' → создаёт ProjectDocument + сохраняет файл.
    Тип документа угадываем по расширению (xlsx/csv → spec, pdf → other, и т.д.)."""
    permission_classes = [IsAuthenticated]

    # Парсеры для multipart — иначе DRF не разберёт FormData
    from rest_framework.parsers import MultiPartParser, FormParser
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, project_id):
        p = get_object_or_404(Project, id=project_id, owner=request.user, is_active=True)
        f = request.FILES.get("file")
        if not f:
            return Response({"error": _("Файл не приложен")}, status=400)
        # Простая эвристика типа по расширению
        name = f.name or "document"
        ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
        _by_ext = {
            "xlsx": "spec", "xls": "spec", "csv": "spec",
            "pdf": "other", "docx": "other", "doc": "other",
            "dwg": "drawing", "dxf": "drawing",
            "png": "other", "jpg": "other", "jpeg": "other",
        }
        # Если фронт явно указал doctype (выбор слота категории) — используем его,
        # иначе угадываем по расширению.
        explicit_doctype = (request.data.get("doctype") or "").strip().lower()
        ALLOWED = ("fleet", "spec", "regulation", "drawing", "conditions", "contract", "invoice",
                   "pricelist", "certificate", "photo", "customs", "logistics", "payment", "other")
        doctype = explicit_doctype if explicit_doctype in ALLOWED else _by_ext.get(ext, "other")
        try:
            from .models import ProjectDocument
            doc = ProjectDocument.objects.create(
                project=p,
                name=name[:200],
                file=f,
                doctype=doctype,
                status="processed",
                size_bytes=f.size or 0,
                meta={"original_ext": ext},
            )
            # Мост Проект→Чертежи: чертёж проекта дублируем как Drawing(project=…),
            # чтобы он попал в «Мои чертежи» отдельной папкой проекта и получил
            # привязку к позиции каталога (🔗 умный поиск).
            if doctype == "drawing":
                try:
                    from marketplace.models import Drawing
                    FMT = {"dwg": "dwg", "dxf": "dxf", "pdf": "pdf", "step": "step",
                           "stp": "step", "iges": "iges", "igs": "iges", "stl": "stl",
                           "png": "png", "jpg": "jpg", "jpeg": "jpg"}
                    try:
                        d_url = doc.file.url
                    except Exception:
                        d_url = ""
                    Drawing.objects.create(
                        seller=request.user, project=p, project_doc=doc,
                        title=name[:255], file_url=d_url, file_name=name[:255],
                        file_format=FMT.get(ext, "pdf"), status="draft",
                        access_level="private", side="need",
                        file_size_kb=int((f.size or 0) / 1024),
                    )
                except Exception:
                    logger.exception("project→drawing bridge failed")
            return Response({
                "id": str(doc.id),
                "name": doc.name,
                "doctype": doc.doctype,
                "doctype_label": doc.get_doctype_display(),
                "status": doc.status,
                "size_kb": round((doc.size_bytes or 0) / 1024, 1),
            }, status=201)
        except Exception as e:
            return Response({"error": str(e)}, status=500)


class ProjectDocumentFileView(APIView):
    """GET → стримит файл документа проекта владельцу (надёжно: по file.name,
    без проблем с URL-кодированной кириллицей; работает локально и на проде)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, doc_id):
        from django.http import FileResponse
        from .models import ProjectDocument
        p = get_object_or_404(Project, id=project_id, owner=request.user)
        doc = get_object_or_404(ProjectDocument, id=doc_id, project=p)
        if not doc.file:
            return Response({"error": _("файл не найден")}, status=404)
        try:
            return FileResponse(doc.file.open("rb"), as_attachment=False,
                                filename=doc.name or "document")
        except Exception:
            return Response({"error": _("файл не найден")}, status=404)


class OrderDocumentFileView(APIView):
    """GET → стримит PDF/файл документа заказа (инвойс, packing list, QC и т.д.).

    Зачем отдельная вьюха, а не сырой /media/-URL: на проде user-media по
    /media/order_documents/ НЕ раздаётся (SERVE_MEDIA=False, nginx/WhiteNoise
    отдают только /static/), поэтому ссылка на счёт возвращала 404. Здесь файл
    стримится самим Django + проверкой доступа — работает и локально, и на проде.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id, doc_id):
        from django.http import FileResponse
        from marketplace.models import Order, OrderDocument, OrderItem
        order = get_object_or_404(Order, id=order_id)
        # Доступ: покупатель-владелец, продавец с позициями в заказе, оператор/staff.
        # ВАЖНО: операторы у нас is_staff=False — роль лежит в profile.role. Без этой
        # проверки оператор мог СОЗДАТЬ документ (доступ по роли в action), но получал
        # 403 при открытии PDF («не открывается»).
        _role = getattr(getattr(request.user, "profile", None), "role", None)
        allowed = (
            request.user.is_staff
            or _role in ("operator", "admin")
            or order.buyer_id == request.user.id
            or OrderItem.objects.filter(order=order, part__seller=request.user).exists()
        )
        if not allowed:
            return Response({"error": _("нет доступа")}, status=403)
        doc = get_object_or_404(OrderDocument, id=doc_id, order=order)
        if not doc.file_obj:
            return Response({"error": _("файл не найден")}, status=404)
        try:
            return FileResponse(
                doc.file_obj.open("rb"), as_attachment=False,
                filename=(doc.title or f"ORD-{order_id}-document") + ".pdf",
            )
        except Exception:
            return Response({"error": _("файл не найден")}, status=404)


def _eta_label(request, days=30):
    """ETA-дата через N дней, локализованная: «30 апр» / «Apr 30» / «4月30日»."""
    from django.utils import translation, timezone
    from django.utils.formats import date_format
    from datetime import timedelta
    lang = getattr(request, "LANGUAGE_CODE", "ru") if request else "ru"
    d = (timezone.now() + timedelta(days=days)).date()
    with translation.override(lang):
        return date_format(d, "j E").lower() if lang == "ru" else date_format(d, "M j")


def _prev_month_label(request, offset=1):
    """Возвращает название месяца (locale-aware) с offset месяцев назад.
    offset=0 — текущий, offset=1 — прошлый.
    """
    from django.utils import translation, timezone
    from django.utils.formats import date_format
    lang = getattr(request, "LANGUAGE_CODE", "ru") if request else "ru"
    now = timezone.now()
    # Откатываем месяц
    month = now.month - offset
    year = now.year
    while month < 1:
        month += 12
        year -= 1
    import datetime
    d = datetime.date(year, month, 1)
    with translation.override(lang):
        # Полное название месяца. Можно сократить до 3 букв через .strftime("%b") — но
        # Django formatting даёт корректную локаль для %b в зависимости от языка.
        return date_format(d, "F")


class ProjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, project_id):
        # Soft-delete: is_active=False — иначе оторвём связанные чаты/документы
        p = get_object_or_404(Project, id=project_id, owner=request.user)
        p.is_active = False
        p.save(update_fields=["is_active"])
        return Response(status=204)

    def get(self, request, project_id):
        p = get_object_or_404(Project, id=project_id, owner=request.user, is_active=True)
        # Documents — для чертежей подтягиваем bridge-Drawing (drawing_id + oem),
        # чтобы на странице проекта можно было привязать артикул.
        from marketplace.models import Drawing
        _FMT = {"dwg": "dwg", "dxf": "dxf", "pdf": "pdf", "step": "step", "stp": "step",
                "iges": "iges", "igs": "iges", "stl": "stl", "png": "png", "jpg": "jpg", "jpeg": "jpg"}
        docs = []
        for d in p.documents.all():
            entry = {
                "id": str(d.id),
                "name": d.name,
                "doctype": d.doctype,
                "doctype_label": d.get_doctype_display(),
                "status": d.status,
                "size_kb": round(d.size_bytes / 1024, 1) if d.size_bytes else None,
                "url": (d.file.url if d.file else None),
                "meta": d.meta,
                "uploaded_at": d.uploaded_at.strftime("%d.%m.%Y"),
                "drawing_id": None,
                "oem": "",
            }
            if d.doctype == "drawing":
                dr = Drawing.objects.filter(project_doc=d).first()
                if not dr:  # backfill для старых документов
                    try:
                        d_url = d.file.url if d.file else ""
                    except Exception:
                        d_url = ""
                    ext = (d.name.rsplit(".", 1)[-1] if "." in d.name else "").lower()
                    dr = Drawing.objects.create(
                        seller=request.user, project=p, project_doc=d,
                        title=d.name[:255], file_url=d_url, file_name=d.name[:255],
                        file_format=_FMT.get(ext, "pdf"), status="draft", side="need",
                        access_level="private", file_size_kb=int((d.size_bytes or 0) / 1024))
                entry["drawing_id"] = str(dr.id)
                entry["oem"] = dr.oem_number or ""
            docs.append(entry)
        # Linked chats
        chats = [{
            "id": str(c.id),
            "title": c.title,
            "updated_at": c.updated_at.isoformat(),
            "preview": (c.messages.first().content[:120] if c.messages.exists() else ""),
        } for c in p.conversations.filter(is_active=True).order_by("-updated_at")[:20]]
        # Stats/списки — демо (в проде были бы FK). Ветвим по роли: у покупателя проект =
        # закупочный (RFQ/заказы/расходы), у продавца проект = ТОВАРНОЕ НАПРАВЛЕНИЕ
        # (входящие RFQ по сегменту / заказы в работе / каталог / выручка).
        role = detect_user_role(request.user, request=request)
        is_seller = (role == "seller")
        is_operator = role.startswith("operator")
        participants = []  # только для оператора (видит обе стороны сделки)

        if is_operator:
            # Единый набор полей сделки — фронт показывает свой срез под подроль
            # (КАМ/менеджер, логист, таможня, платежи).
            stats = {
                "positions": {"count": 12, "awaiting": 3},
                "logistics": {"count": 4, "in_transit": 3, "at_customs": 1,
                              "earliest_eta": _eta_label(request, days=4), "delays": 1},
                "customs": {"at_customs": 1, "hs_pending": 2, "declarations": 3, "sanctions_risk": 0},
                "payments": {"escrow_usd": 142800, "awaiting_payout": 2,
                             "paid_by_buyer_usd": 168400, "margin_pct": 11},
                "deal_turnover": {"value_usd": 168400, "margin_pct": 11},
            }
            rfqs = [
                {"number": "RFQ-4421", "title": _("Spec Q2 — основной микс"), "tag": "AUTO",
                 "meta": _("39 позиций · сматчены с каталогом"),
                 "best_so_far": 47890, "best_label": _("сумма по подбору")},
                {"number": "RFQ-4418", "title": _("Track shoes D8T — нужен аналог"), "tag": "SEMI",
                 "meta": _("2 позиции · подберите аналог"),
                 "best_so_far": 7440, "best_label": _("ориентир по каталогу")},
                {"number": "RFQ-4407", "title": _("Гидрофильтры — пополнение"), "tag": "AUTO",
                 "meta": _("1 позиция · 12 шт · сматчена"),
                 "best_so_far": 2112, "best_label": _("сумма по подбору")},
            ]
            orders = [
                {"number": "PO-22841", "title": _("Spec Q2 partial — 14 позиций"),
                 "status": _("НА ТАМОЖНЕ"), "status_color": "amber",
                 "stages": [True, True, True, True, False],
                 "stage_labels": ["RFQ", _("Заказ"), _("Производство"), _("Таможня"), _("Доставлен")],
                 "seller": "XCMG", "operator": "",
                 "eta": _("ETA ") + _eta_label(request, days=4), "amount": 28640},
                {"number": "PO-22829", "title": _("Гидрофильтры — 12 шт"),
                 "status": _("В ПУТИ"), "status_color": "green",
                 "stages": [True, True, True, False, False],
                 "stage_labels": ["RFQ", _("Заказ"), _("Производство"), _("Таможня"), _("Доставлен")],
                 "seller": "Caterpillar Eurasia", "operator": "",
                 "eta": _("ETA ") + _eta_label(request, days=2), "amount": 2112},
            ]
            participants = [
                {"role": _("Покупатель"), "name": "СтройМонтаж-Урал", "meta": _("клиент · 3 RFQ в сделке")},
                {"role": _("Продавец"), "name": "XCMG", "meta": _("14 позиций · отгрузка")},
                {"role": _("Продавец"), "name": "Caterpillar Eurasia", "meta": _("12 шт · в пути")},
            ]
        elif is_seller:
            stats = {
                # incoming_rfqs.awaiting — RFQ по направлению, ждущие вашего КП
                "incoming_rfqs": {"count": 4, "awaiting": 2},
                # active_orders.to_ship — заказы в работе, из них к отгрузке
                "active_orders": {"count": 3, "value_usd": 96400, "to_ship": 2},
                # catalog_items.with_drawing — товаров в каталоге направления, с чертежом/фото
                "catalog_items": {"count": 128, "with_drawing": 34},
                # revenue_mtd — выручка по направлению за месяц + дельта
                "revenue_mtd": {"value_usd": 73250, "delta_pct": 9},
            }
            rfqs = [
                {"number": "RFQ-5102", "title": _("Ходовая Komatsu PC200 — запрос"), "tag": "AUTO",
                 "meta": "6 позиций · сматчены с вашим каталогом",
                 "best_so_far": 18400, "best_label": "потенциал по подбору"},
                {"number": "RFQ-5098", "title": _("Катки опорные — аналог"), "tag": "SEMI",
                 "meta": "2 позиции · оператор уточняет аналог",
                 "best_so_far": 5200, "best_label": "ориентир по каталогу"},
                {"number": "RFQ-5077", "title": _("Сегменты ведущей звезды — пополнение"), "tag": "AUTO",
                 "meta": "1 позиция · 8 шт · сматчена с каталогом",
                 "best_so_far": 3100, "best_label": "потенциал по подбору"},
            ]
            orders = [
                {"number": "PO-30122", "title": _("Катки опорные Komatsu — 14 шт"),
                 "status": "К ОТГРУЗКЕ", "status_color": "amber",
                 "stages": [True, True, True, False, False],
                 "stage_labels": ["RFQ", "Заказ", "Производство", "Отгрузка", "Доставлен"],
                 "seller": "", "operator": "",
                 "eta": "Отгрузка до " + _eta_label(request, days=4),
                 "amount": 28640},
                {"number": "PO-30119", "title": _("Сегменты ведущей звезды — 8 шт"),
                 "status": "В ПУТИ", "status_color": "green",
                 "stages": [True, True, True, True, False],
                 "stage_labels": ["RFQ", "Заказ", "Производство", "Отгрузка", "Доставлен"],
                 "seller": "", "operator": "",
                 "eta": "ETA " + _eta_label(request, days=2),
                 "amount": 2112},
            ]
        else:
            stats = {
                "open_rfqs": {"count": 3, "semi": 1},
                "active_orders": {"count": 5, "value_usd": 184200},
                "in_transit": {"count": 2, "earliest_eta": _eta_label(request, days=30)},
                "spend_mtd": {"value_usd": 124500, "delta_pct": 12},
            }
            rfqs = [
                {"number": "RFQ-4421", "title": _("Spec Q2 — основной микс"), "tag": "AUTO",
                 "meta": "39 позиций · сматчены с каталогом",
                 "best_so_far": 47890, "best_label": "сумма по подбору"},
                {"number": "RFQ-4418", "title": _("Track shoes D8T — аналоги"), "tag": "SEMI",
                 "meta": "2 позиции · оператор подбирает аналог",
                 "best_so_far": 7440, "best_label": "ориентир по каталогу"},
                {"number": "RFQ-4407", "title": _("Гидрофильтры — пополнение"), "tag": "AUTO",
                 "meta": "1 позиция · 12 шт · сматчена с каталогом",
                 "best_so_far": 2112, "best_label": "сумма по подбору"},
            ]
            orders = [
                {"number": "PO-22841", "title": _("Spec Q2 partial — 14 позиций"),
                 "status": "НА ТАМОЖНЕ", "status_color": "amber",
                 "stages": [True, True, True, True, False],
                 "stage_labels": ["RFQ", "Заказ", "Производство", "Таможня", "Доставлен"],
                 "seller": "XCMG", "operator": "",
                 "eta": "ETA " + _eta_label(request, days=4), "amount": 28640},
                {"number": "PO-22829", "title": _("Гидрофильтры — 12 шт"),
                 "status": "В ПУТИ", "status_color": "green",
                 "stages": [True, True, True, False, False],
                 "stage_labels": ["RFQ", "Заказ", "Производство", "Таможня", "Доставлен"],
                 "seller": "Caterpillar Eurasia", "operator": "",
                 "eta": "ETA " + _eta_label(request, days=2), "amount": 2112},
            ]

        return Response({
            "id": str(p.id),
            "name": p.name,
            "code": p.code,
            "customer": p.customer,
            "tags": p.tags,
            "deadline": p.deadline.strftime("%d %B").lower() if p.deadline else None,
            "dot_color": p.dot_color,
            "description": p.description,
            "role": role,
            "documents": docs,
            "chats": chats,
            "stats": stats,
            "rfqs": rfqs,
            "orders": orders,
            "participants": participants,
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

        from marketplace.fx import to_usd_float  # живой бирж. курс
        items = []
        total_usd = 0.0
        for it in rfq.items.select_related("matched_part__brand").all():
            mp = it.matched_part
            price = float(mp.price) if (mp and mp.price is not None) else None
            ccy = (getattr(mp, "currency", "USD") if mp else "USD") or "USD"
            qty = it.quantity or 1
            price_usd = to_usd_float(price, ccy) if price is not None else None
            if price_usd is not None:
                total_usd += price_usd * qty
            # Покупатель (владелец RFQ) видит USD; продавец-адресат/staff — исходную валюту.
            show_price = price_usd if is_owner else price
            show_ccy = "USD" if is_owner else ccy
            items.append({
                "article": it.query,
                "qty": qty,
                "state": "matched" if mp else ("no_match" if it.state == "needs_review" else "pending"),
                "match": mp.title if mp else None,
                "brand": (mp.brand.name if (mp and mp.brand) else None),
                "supplier": getattr(mp, "supplier_name", None) if mp else None,
                "price": show_price,
                "currency": show_ccy,
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
        # Стримим САМ файл (после проверки доступа выше), а не JSON со ссылкой
        # на /media/: иначе приватный чертёж открыт всем по прямой ссылке.
        # Работает одинаково на runserver (локально) и на проде.
        import urllib.parse

        from django.core.files.storage import default_storage
        from django.http import FileResponse
        # file_url хранится URL-кодированным (кириллица → %D0..); на диске путь
        # декодирован — раскодируем, иначе default_storage.exists() = False.
        rel = urllib.parse.unquote((drawing.file_url or "").split("/media/", 1)[-1].lstrip("/"))
        if not rel or not default_storage.exists(rel):
            return Response({"ok": False, "error": "файл чертежа не найден"}, status=404)
        fname = drawing.file_name or rel.rsplit("/", 1)[-1]
        return FileResponse(
            default_storage.open(rel, "rb"),
            as_attachment=(action == "download"),
            filename=fname,
        )


class DrawingUploadView(APIView):
    """POST /api/assistant/drawings/upload/  (multipart/form-data)

    Продавец загружает чертёж: file (required) + опц. oem_number, title,
    revision, access_level. Файл кладётся в media/drawings/<seller>/, создаётся
    Drawing (status=draft → на модерацию оператору). Привязка к детали по OEM.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import os

        from django.core.files.storage import default_storage
        from django.utils.text import get_valid_filename

        from marketplace.models import Drawing, Part

        role = detect_user_role(request.user, request=request)
        # Загружают и продавцы (что предлагают), и покупатели (что нужно).
        # Чертежи приватны: видны только владельцу и оператору (при согласовании).
        if role not in ("seller", "buyer"):
            return Response(
                {"ok": False, "error": "Загрузка чертежей доступна продавцам и покупателям."},
                status=403)

        f = request.FILES.get("file")
        if not f:
            return Response({"ok": False, "error": "Файл не передан."}, status=400)
        if f.size > 50 * 1024 * 1024:
            return Response({"ok": False, "error": "Файл больше 50 МБ."}, status=400)

        ext = (os.path.splitext(f.name)[1] or "").lstrip(".").lower()
        FMT = {"pdf": "pdf", "dwg": "dwg", "dxf": "dxf", "step": "step",
               "stp": "step", "iges": "iges", "igs": "iges", "stl": "stl",
               "png": "png", "jpg": "jpg", "jpeg": "jpg"}
        file_format = FMT.get(ext, "pdf")

        safe_name = get_valid_filename(f.name)[:200] or "drawing"
        path = default_storage.save(f"drawings/{request.user.id}/{safe_name}", f)
        try:
            file_url = default_storage.url(path)
        except Exception:
            file_url = f"/media/{path}"

        oem = (request.data.get("oem_number") or "").strip()
        part = None
        if oem:
            part = Part.objects.filter(
                seller=request.user, oem_number__iexact=oem).first()

        access_level = (request.data.get("access_level") or "private").strip()
        if access_level not in ("private", "for_sale", "rewardable"):
            access_level = "private"

        d = Drawing.objects.create(
            seller=request.user,
            part=part,
            title=((request.data.get("title") or "").strip()[:255] or f.name[:255]),
            file_url=file_url,
            file_name=f.name[:255],
            file_size_kb=max(1, f.size // 1024),
            file_format=file_format,
            revision=((request.data.get("revision") or "A").strip()[:20] or "A"),
            status="draft",
            access_level=access_level,
            oem_number=oem[:100],
            side=("need" if role == "buyer" else "offer"),
        )
        return Response({
            "ok": True,
            "drawing_id": d.id,
            "title": d.title,
            "file_format": d.file_format,
            "linked_part": (part.oem_number if part else None),
            "access_level": d.access_level,
            "status": d.status,
        }, status=201)


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
        unread_qs = Notification.objects.filter(user=request.user, is_read=False)
        unread_count = unread_qs.count()
        # Непрочитанные по kind → фронт вешает бейдж «требует действия» на
        # соответствующую пилюлю (rfq/order/payment/sla/claim → её раздел).
        from django.db.models import Count as _Count
        unread_by_kind = {
            row["kind"]: row["c"]
            for row in unread_qs.values("kind").annotate(c=_Count("id"))
        }
        return Response({
            "unread_count": unread_count,
            "unread_by_kind": unread_by_kind,
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
