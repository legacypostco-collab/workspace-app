import logging
import time
from urllib.parse import parse_qs, urlparse

from django.db.models import Q as _Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .security import confirmation_is_true

logger = logging.getLogger(__name__)

_PENDING_2FA_SESSION_KEY = "assistant_pending_2fa_login"
_PENDING_2FA_TTL_SECONDS = 5 * 60
_PENDING_2FA_MAX_ATTEMPTS = 5


def _safe_local_url(value) -> str:
    url = str(value or "").strip()
    if not url.startswith("/") or url.startswith("//"):
        return ""
    if not url_has_allowed_host_and_scheme(url, allowed_hosts=set()):
        return ""
    return url


def _notification_targets_rfq(url: str, rfq_id: int) -> bool:
    """Match an RFQ notification link by an exact id, never by prefix."""
    parsed = urlparse(str(url or ""))
    target = str(rfq_id)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if target in query.get("rfq", []) or target in query.get("rfq_id", []):
        return True

    parts = [part for part in parsed.path.split("/") if part]
    return any(
        part == "rfq" and parts[index + 1] == target
        for index, part in enumerate(parts[:-1])
    )


def _begin_pending_2fa(request, user, *, flow, role):
    request.session[_PENDING_2FA_SESSION_KEY] = {
        "user_id": user.pk,
        "auth_hash": user.get_session_auth_hash(),
        "flow": flow,
        "role": role,
        "created_at": int(time.time()),
        "attempts": 0,
    }
    request.session.modified = True


def _pending_2fa_user(request, *, flow, role):
    from django.contrib.auth import get_user_model
    from django.utils.crypto import constant_time_compare

    pending = request.session.get(_PENDING_2FA_SESSION_KEY)
    if not isinstance(pending, dict):
        return None
    valid_context = pending.get("flow") == flow and pending.get("role") == role
    fresh = int(time.time()) - int(pending.get("created_at") or 0) <= _PENDING_2FA_TTL_SECONDS
    attempts_ok = int(pending.get("attempts") or 0) < _PENDING_2FA_MAX_ATTEMPTS
    user = get_user_model().objects.filter(
        pk=pending.get("user_id"),
        is_active=True,
    ).first()
    auth_hash_ok = bool(
        user
        and constant_time_compare(
            pending.get("auth_hash") or "",
            user.get_session_auth_hash(),
        )
    )
    if not (valid_context and fresh and attempts_ok and auth_hash_ok):
        request.session.pop(_PENDING_2FA_SESSION_KEY, None)
        request.session.modified = True
        return None
    return user


def _record_pending_2fa_failure(request):
    pending = request.session.get(_PENDING_2FA_SESSION_KEY)
    if not isinstance(pending, dict):
        return 0
    attempts = int(pending.get("attempts") or 0) + 1
    remaining = max(0, _PENDING_2FA_MAX_ATTEMPTS - attempts)
    if remaining:
        pending["attempts"] = attempts
        request.session[_PENDING_2FA_SESSION_KEY] = pending
    else:
        request.session.pop(_PENDING_2FA_SESSION_KEY, None)
    request.session.modified = True
    return remaining


def _clear_pending_2fa(request):
    request.session.pop(_PENDING_2FA_SESSION_KEY, None)
    request.session.modified = True

from .models import Conversation, Feedback, Message
from .permissions import detect_user_role, user_allowed_role_tabs
from .rag import execute_action, process_query_sync
from .serializers import (
    ActionRequestSerializer,
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

    def get_permissions(self):
        # Гостю нужен только пустой список для первичного рендера. Создавать,
        # читать по id, менять или удалять диалоги может лишь владелец.
        if self.action == "list":
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        # Anon: пустой queryset (UI покажет «Нет проектов / Недавнее пусто»)
        if not self.request.user.is_authenticated:
            return Conversation.objects.none()
        from .conversation_access import accessible_conversations
        role = detect_user_role(self.request.user, request=self.request)
        return accessible_conversations(self.request.user, role)

    def get_serializer_class(self):
        if self.action == "list":
            return ConversationListSerializer
        return ConversationSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user,
                        role=detect_user_role(self.request.user, request=self.request))

    def perform_update(self, serializer):
        if serializer.instance.participant_links.exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Общий разговор поддержки нельзя изменять.")
        serializer.save()

    def perform_destroy(self, instance):
        # Hard delete: пользователь явно нажал «Удалить» в UI и ожидает,
        # что чат пропадёт навсегда (а не вернётся при следующем
        # order-event / WS-reconnect через find_or_create_conv,
        # который фильтрует по is_active=True). Messages удаляются по
        # CASCADE из FK в models.Message.
        if instance.participant_links.exclude(user=self.request.user).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Общий разговор поддержки нельзя удалить из истории.")
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
                        "• Откройте поиск запчастей или базу знаний\n\n"
                        "Для сохранения заявки и истории понадобится аккаунт."
                    ),
                    "cards": [],
                    "actions": [
                        {"action": "start_registration", "label": _("Зарегистрироваться")},
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
                if action_name in ANON_RESUMABLE_ACTIONS:
                    request.session["pending_action"] = {
                        "action": action_name,
                        "params": params,
                    }
                    request.session.modified = True
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
            except Exception:
                logger.exception("anon fast_path action failed")
                return Response({"error": _("Не удалось выполнить запрос.")},
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
        current_role = detect_user_role(request.user, request=request)
        if conv_id:
            from .conversation_access import accessible_conversations
            try:
                conv = get_object_or_404(
                    accessible_conversations(request.user, current_role),
                    id=conv_id,
                )
            except Http404:
                raise
        else:
            conv = Conversation.objects.create(
                user=request.user,
                role=current_role,
            )

        from .support_threads import is_human_support, post_support_message
        if is_human_support(conv):
            try:
                msg = post_support_message(conv, request.user, current_role, message)
            except PermissionError as exc:
                return Response({"error": str(exc)}, status=403)
            return Response({
                "conversation_id": str(conv.id),
                "response": _("Сообщение отправлено участникам обращения."),
                "cards": [], "actions": [], "contextual_actions": [],
                "context_refs": [], "suggestions": [],
                "message_id": str(msg.id), "human_support": True,
            })

        try:
            result = process_query_sync(conv, message, request.user,
                                       ui_lang=getattr(request, "LANGUAGE_CODE", "ru"))
        except Exception:
            logger.exception("chat query processing failed")
            return Response(
                {"error": _("Не удалось обработать сообщение.")},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

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
# Гость может изучать публичный каталог и справочные возможности, но не
# создавать сущности и не читать данные по идентификаторам чужих сделок.
# Это публичный просмотр, а не анонимный кабинет.
ANON_ALLOWED_ACTIONS: set[str] = {
    # Auth-actions
    "start_registration", "start_login",
    # Публичные данные каталога и справочные расчёты
    "search_parts", "browse_brands", "browse_categories", "kb_search",
    "compare_products", "compare_suppliers", "top_suppliers",
    "calc_part_logistics",
    # Аналитика загруженной спецификации без сохранения сделки
    "analyze_spec", "upload_parts_list",
    "go_home",
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

ANON_RESUMABLE_ACTIONS: set[str] = {
    "create_rfq",
}


def _registration_required_response():
    """Карточка «зарегистрируйтесь» — для всех остальных action'ов.

    Кнопки запускают chat-action `start_registration` / `start_login`.
    Фронт открывает формы в модальном окне без редиректа на отдельную страницу.
    """
    return {
        "text": _(
            "Чтобы продолжить, войдите или создайте аккаунт.\n"
            "Так мы сохраним историю запросов, проекты и статусы поставок."
        ),
        "actions": [
            {"action": "start_registration", "label": _("Зарегистрироваться")},
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
            "Чтобы оформить оплату, нужен аккаунт.\n"
            "Это нужно для:\n"
            "• приёма и возврата средств (резерв 10%)\n"
            "• юридического оформления заказа\n"
            "• трекинга вашей доставки в личном кабинете\n\n"
            "После входа текущий запрос, RFQ и выбранная котировка сохранятся."
        ),
        "actions": [
            {"action": "start_registration", "label": _("Создать аккаунт и оплатить"),
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

    confirmed = confirmation_is_true(params.get("confirmed"))
    role = (params.get("role") or "buyer").lower()

    if confirmed:
        from marketplace.views import _rl_consume

        if not _rl_consume(request, "register", 5, 3600):
            return {
                "text": _("Слишком много попыток регистрации. Попробуйте через час."),
                "actions": [], "cards": [], "suggestions": [],
                "contextual_actions": [],
            }

    # ── Seller — простой flow + KYB-onboarding после регистрации ─
    if role == "seller":
        return _handle_seller_quick_registration(request, params)

    # ── Buyer — 8 полей по ТЗ §1 ───────────────────────────
    if not confirmed:
        return bureg.render_form(params)
    result = bureg.attempt_register(request, params)
    if result["ok"]:
        user = result["user"]
        if not result.get("login_allowed", True):
            return result["response"]
        user.backend = "django.contrib.auth.backends.ModelBackend"
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
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
        pending = request.session.get("pending_action")
        if pending and pending.get("action"):
            continuing = (
                _("Продолжаю создание заявки...")
                if pending["action"] == "create_rfq"
                else _("Продолжаю оформление заказа...")
            )
            # Намерение заберёт widget-config после перезагрузки уже в
            # авторизованном контексте. Так форма не теряется и не выполняется
            # в старом гостевом состоянии страницы.
            return {
                "text": (result["response"].get("text", "") + "\n\n"
                         + continuing),
                "actions": [],
                "cards": [], "suggestions": [], "contextual_actions": [],
                "_post_action": "reload",
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
    from django.conf import settings
    from django.contrib.auth import login
    from django.db import transaction
    from marketplace.forms import RegisterForm
    from marketplace.models import UserProfile
    from .consents import (
        record_registration_consents,
        registration_consent_errors,
        registration_consent_fields,
    )

    confirmed = confirmation_is_true(params.get("confirmed"))
    if not confirmed:
        return {
            "text": _(
                "Регистрация поставщика — 2 шага.\n\n"
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
                    "title": _("Шаг 1 из 2 · Аккаунт поставщика"),
                    "submit_action": "start_registration",
                        "submit_label": _("Создать аккаунт"),
                    "fields": [
                        {"name": "username", "label": _("Логин"),
                         "required": True, "placeholder": "myshop_2026"},
                        {"name": "email", "label": _("Электронная почта"),
                         "type": "email", "required": True},
                        {"name": "password1", "label": _("Пароль"),
                         "type": "password", "required": True, "minlength": 8},
                        {"name": "password2", "label": _("Повторите пароль"),
                         "type": "password", "required": True, "minlength": 8},
                    ] + registration_consent_fields(),
                    "fixed_params": {"confirmed": True, "role": "seller"},
                },
            }],
            "actions": [{"action": "start_login", "label": _("У меня уже есть аккаунт"),
                          "params": {"role": "seller"}}],
            "suggestions": [], "contextual_actions": [],
        }

    consent_errors = registration_consent_errors(params)
    if consent_errors:
        return {
            "text": _("Подтвердите условия регистрации в двух отдельных полях."),
            "actions": [{"action": "start_registration", "label": _("Попробовать снова"),
                         "params": {"role": "seller"}}],
            "cards": [], "suggestions": [], "contextual_actions": [],
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
            "text": _("Не получилось создать аккаунт:\n") + errs,
            "actions": [{"action": "start_registration", "label": _("Попробовать снова"),
                         "params": {"role": "seller"}}],
            "cards": [], "suggestions": [], "contextual_actions": [],
        }
    user = form.save(commit=False)
    user.email = form.cleaned_data["email"]
    email_verification_required = bool(
        getattr(settings, "EMAIL_VERIFICATION_REQUIRED", not settings.DEBUG)
    )
    if email_verification_required:
        user.is_active = False
    with transaction.atomic():
        user.save()
        UserProfile.objects.create(
            user=user,
            role="seller",
            language="ru",
            company_name="",
        )
        record_registration_consents(request, user, role="seller")
    if email_verification_required:
        from marketplace.views import _send_verification_email

        delivered = _send_verification_email(request, user)
        return {
            "text": (
                _("Аккаунт создан. Мы отправили ссылку подтверждения на %(email)s. "
                  "После подтверждения e-mail войдите в аккаунт и заполните KYB-анкету.")
                % {"email": user.email}
                if delivered
                else _(
                    "Аккаунт создан, но письмо подтверждения отправить не удалось. "
                    "Аккаунт пока не активирован; обратитесь в поддержку через чат."
                )
            ),
            "cards": [],
            "actions": (
                []
                if delivered
                else [{"action": "contact_operator", "label": _("Поддержка")}]
            ),
            "suggestions": [], "contextual_actions": [],
        }
    user.backend = "django.contrib.auth.backends.ModelBackend"
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    try:
        from .order_events import notify_operator_alert
        notify_operator_alert(user_obj=user, event="user_registered",
                              extra={"role": "seller"})
    except Exception:
        logger.exception("notify_operator_alert user_registered failed")
    return {
        "text": (_("Аккаунт создан · %(u)s\n"
                   "Сейчас откроем KYB-анкету — нужны реквизиты компании, "
                   "банк и директор. После проверки оператором (≤24ч) сможете "
                   "отвечать на RFQ и принимать заказы.") % {"u": user.username}),
        "cards": [],
        "actions": [{"action": "reload_page", "label": _("Перейти к KYB")}],
        "suggestions": [], "contextual_actions": [],
        "_post_action": "reload",
    }


def _handle_switch_role_login(request, params):
    """Переключение на аккаунт другой роли через chat-форму с паролем.

    Двухфазный flow (как start_login):
      Phase 1: возвращает form-карточку с логином и паролем
      Phase 2: authenticate + проверка что user.role совпадает + login + reload_page

    Если аккаунта с такой ролью нет — предлагается зарегистрировать
    (кроме операторских — операторов заводит только админ).
    """
    role = (params.get("role") or "").lower()
    if role not in {"buyer", "seller", "operator", "admin"}:
        return {"text": _("Неизвестная роль: %(r)s") % {"r": role},
                "cards": [], "actions": [], "suggestions": [], "contextual_actions": []}

    if request.user.is_authenticated:
        from .permissions import _override_allowed
        current_role = detect_user_role(request.user, request=request)
        current_norm = "operator" if current_role.startswith("operator") else current_role
        if current_norm == role:
            return {
                "text": _("Эта роль уже активна."),
                "actions": [], "cards": [], "suggestions": [], "contextual_actions": [],
            }
        if _override_allowed(request.user, role):
            real_role = detect_user_role(request.user)
            real_norm = "operator" if real_role.startswith("operator") else real_role
            if real_norm == role:
                request.session.pop("assistant_role_override", None)
            else:
                request.session["assistant_role_override"] = role
            request.session.modified = True
            return {
                "text": _("Переключаю кабинет..."),
                "actions": [{"action": "reload_page", "label": _("Открыть кабинет")}],
                "cards": [], "suggestions": [], "contextual_actions": [],
                "_post_action": "reload",
            }
        return _handle_add_account_role(request, {"role": role})

    ROLE_META = {
        "buyer":    (_("Войти как покупатель"), _("Введите логин и пароль аккаунта покупателя.")),
        "seller":   (_("Войти как поставщик"),  _("Введите логин и пароль аккаунта поставщика.")),
        "operator": (_("Войти как оператор"),   _("Введите логин и пароль операторского аккаунта.")),
        "admin":    (_("Вход администратора"),   _("Введите логин и пароль административного аккаунта.")),
    }
    title, greeting = ROLE_META[role]
    confirmed = confirmation_is_true(params.get("confirmed"))

    def _registration_actions():
        reg_actions = []
        if role == "buyer":
            reg_actions.append({"action": "start_registration",
                                "label": _("Создать аккаунт покупателя"),
                                "params": {"role": "buyer"}})
        elif role == "seller":
            reg_actions.append({"action": "start_registration",
                                "label": _("Создать аккаунт поставщика"),
                                "params": {"role": "seller"}})
        elif role == "operator":
            reg_actions.append({"action": "contact_operator",
                                "label": _("Запросить операторский доступ у админа"),
                                "params": {"topic": "operator_access"}})
        return reg_actions

    def _password_form(message, *, username="", password_error=""):
        return {
            "text": message,
            "cards": [{
                "type": "form",
                "data": {
                    "title": title,
                    "intent": _(
                        "Войдите в аккаунт с нужной ролью. Если вы уже внутри своего аккаунта, "
                        "дополнительную роль можно добавить через кнопку «+» рядом с переключателем."
                    ),
                    "submit_action": "switch_role_login",
                    "submit_label": _("Войти →"),
                    "fields": [
                        {"name": "username", "label": _("Логин или e-mail"),
                         "value": username,
                         "placeholder": "ivanov / you@company.ru",
                         "required": True},
                        {"name": "password", "label": _("Пароль"),
                         "type": "password", "required": True,
                         "placeholder": _("Введите пароль"),
                         "error": password_error},
                    ],
                    "fixed_params": {"confirmed": True, "role": role},
                },
            }],
            "actions": _registration_actions(),
            "suggestions": [], "contextual_actions": [],
        }

    def _otp_form(message, *, otp_error=""):
        return {
            "text": message,
            "cards": [{
                "type": "form",
                "data": {
                    "title": _("Подтверждение входа"),
                    "submit_action": "switch_role_login",
                    "submit_label": _("Подтвердить"),
                    "fields": [{
                        "name": "otp_code",
                        "label": _("Одноразовый код"),
                        "placeholder": "000000",
                        "required": True,
                        "autocomplete": "one-time-code",
                        "error": otp_error,
                    }],
                    "fixed_params": {
                        "confirmed": True,
                        "two_factor": True,
                        "role": role,
                    },
                },
            }],
            "actions": [{
                "action": "switch_role_login",
                "label": _("Вернуться к вводу пароля"),
                "params": {"role": role},
            }],
            "suggestions": [], "contextual_actions": [],
        }

    if not confirmed:
        _clear_pending_2fa(request)
        return _password_form(greeting)

    from django.contrib.auth import authenticate, get_user_model, login
    from .security import user_has_enabled_2fa, verify_user_2fa

    if params.get("two_factor"):
        user = _pending_2fa_user(request, flow="switch_role_login", role=role)
        if not user:
            return _password_form(
                _("Время подтверждения истекло. Введите логин и пароль ещё раз."),
                username="",
            )
        if not verify_user_2fa(user, params.get("otp_code") or ""):
            remaining = _record_pending_2fa_failure(request)
            if not remaining:
                return _password_form(
                    _("Слишком много неверных кодов. Введите логин и пароль ещё раз."),
                    username="",
                )
            return _otp_form(
                _("Введите код из приложения-аутентификатора или резервный код."),
                otp_error=_("Неверный код. Осталось попыток: %(n)s") % {"n": remaining},
            )
        _clear_pending_2fa(request)
    else:
        raw = (params.get("username") or "").strip()
        pwd = params.get("password") or ""
        U = get_user_model()
        if "@" in raw:
            u = U.objects.filter(email__iexact=raw).first()
            if u:
                raw = u.username
        if not raw:
            return _password_form(_("Укажите логин или e-mail."), username="")
        user = authenticate(request, username=raw, password=pwd)
        if not user:
            _clear_pending_2fa(request)
            return _password_form(
                _("Не удалось войти. Проверьте введённые данные."),
                username=(params.get("username") or "").strip(),
                password_error=_("Неверный логин или пароль."),
            )
        # Сначала проверяем роль, затем раскрываем наличие второго фактора.
        actual_role = detect_user_role(user)
        actual_norm = "operator" if actual_role.startswith("operator") else actual_role
        if actual_norm != role:
            return _password_form(
                _("Этот аккаунт не имеет выбранной роли."),
                username=(params.get("username") or "").strip(),
                password_error=_("Выберите аккаунт с ролью «%(role)s».") % {"role": role},
            )
        if user_has_enabled_2fa(user):
            _begin_pending_2fa(request, user, flow="switch_role_login", role=role)
            return _otp_form(
                _("Для аккаунта включена двухфакторная защита. Введите код из приложения-аутентификатора."),
            )

    # Очищаем старый session-override чтобы UI взял правильную роль из identity
    request.session.pop("assistant_role_override", None)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return {
        "text": _("Вы вошли как «%(u)s». Перезагружаю кабинет...") % {"u": user.username},
        "actions": [{"action": "reload_page", "label": _("Открыть кабинет")}],
        "cards": [], "suggestions": [], "contextual_actions": [],
        "_post_action": "reload",
    }


def _profile_for_role_extension(user):
    from marketplace.models import UserProfile

    profile = getattr(user, "userprofile", None) or getattr(user, "profile", None)
    if not profile:
        profile = UserProfile.objects.create(user=user, role="buyer", language="ru")
    return profile


def _set_if_present(obj, attr, params, key=None):
    key = key or attr
    if key in params:
        value = (params.get(key) or "").strip()
        if value:
            setattr(obj, attr, value)


def _handle_add_account_role(request, params):
    """Add a second business role to the current account.

    Buyer role is enabled immediately. Seller role creates a disabled role and
    a KYB draft; an operator enables it after verification.
    """
    if not request.user.is_authenticated:
        return _registration_required_response()

    from .permissions import ROLE_LABELS, user_allowed_role_tabs

    role = (params.get("role") or "").lower().strip()
    current_roles = {tab["role"] for tab in user_allowed_role_tabs(request.user)}
    available = [r for r in ("buyer", "seller") if r not in current_roles]

    if not role:
        actions = [
            {"action": "add_account_role", "label": _("Добавить роль: %(role)s") % {"role": ROLE_LABELS[r]},
             "params": {"role": r}}
            for r in available
        ]
        actions.append({"action": "contact_operator",
                        "label": _("Запросить операторский доступ"),
                        "params": {"topic": "operator_access"}})
        if not available:
            return {
                "text": _("На этом аккаунте уже подключены все роли, которые можно добавить самостоятельно. Операторский доступ выдаёт администратор."),
                "actions": actions[-1:], "cards": [], "suggestions": [], "contextual_actions": [],
            }
        return {
            "text": _("Выберите, какую роль добавить к текущему аккаунту. Переключатель вверху покажет новую роль только после её выдачи."),
            "actions": actions, "cards": [], "suggestions": [], "contextual_actions": [],
        }

    if role == "operator":
        return {
            "text": _("Операторскую роль нельзя подключить самостоятельно. Её выдаёт администратор после назначения зоны ответственности."),
            "actions": [{"action": "contact_operator",
                         "label": _("Запросить операторский доступ"),
                         "params": {"topic": "operator_access"}}],
            "cards": [], "suggestions": [], "contextual_actions": [],
        }
    if role not in ("buyer", "seller"):
        return {"text": _("Неизвестная роль: %(r)s") % {"r": role},
                "cards": [], "actions": [], "suggestions": [], "contextual_actions": []}
    if role in current_roles:
        request.session["assistant_role_override"] = role
        request.session.modified = True
        return {
            "text": _("Эта роль уже подключена. Переключаю кабинет..."),
            "actions": [{"action": "reload_page", "label": _("Открыть кабинет")}],
            "cards": [], "suggestions": [], "contextual_actions": [],
            "_post_action": "reload",
        }

    confirmed = confirmation_is_true(params.get("confirmed"))
    if role == "buyer":
        if not confirmed:
            return {
                "text": _("Добавим роль покупателя к текущему аккаунту. После сохранения она сразу появится в переключателе."),
                "cards": [{
                    "type": "form",
                    "data": {
                        "title": _("Данные покупателя"),
                        "submit_action": "add_account_role",
                        "submit_label": _("Добавить роль покупателя"),
                        "fields": [
                            {"name": "company_name", "label": _("Компания"), "required": True},
                            {"name": "country", "label": _("Страна"), "placeholder": "RU", "required": False},
                            {"name": "tax_id", "label": _("ИНН / Tax ID"), "required": False},
                            {"name": "contact_name", "label": _("Контактное лицо"), "required": True},
                            {"name": "position", "label": _("Должность"), "required": False},
                            {"name": "phone_e164", "label": _("Телефон"), "placeholder": "+7...", "required": False},
                            {"name": "equipment_fleet", "label": _("Парк техники"), "type": "textarea", "required": False},
                        ],
                        "fixed_params": {"confirmed": True, "role": "buyer"},
                    },
                }],
                "actions": [], "suggestions": [], "contextual_actions": [],
            }
        from marketplace.models import UserRole

        profile = _profile_for_role_extension(request.user)
        for field in ("company_name", "country", "tax_id", "contact_name", "position", "phone_e164", "equipment_fleet"):
            _set_if_present(profile, field, params)
        profile.save()
        UserRole.objects.update_or_create(
            user=request.user,
            role="buyer",
            operator_role="",
            defaults={"is_enabled": True},
        )
        request.session["assistant_role_override"] = "buyer"
        request.session.modified = True
        return {
            "text": _("Роль покупателя добавлена. Переключаю кабинет..."),
            "actions": [{"action": "reload_page", "label": _("Открыть кабинет покупателя")}],
            "cards": [], "suggestions": [], "contextual_actions": [],
            "_post_action": "reload",
        }

    if not confirmed:
        return {
            "text": _("Добавим роль поставщика к текущему аккаунту. Сразу после заявки роль появится у оператора на проверке, а в переключателе включится после одобрения."),
            "cards": [{
                "type": "form",
                "data": {
                    "title": _("Заявка на роль поставщика"),
                    "submit_action": "add_account_role",
                    "submit_label": _("Отправить заявку"),
                    "fields": [
                        {"name": "legal_name", "label": _("Юридическое название"), "required": True},
                        {"name": "inn", "label": _("ИНН / Tax ID"), "required": True},
                        {"name": "country", "label": _("Страна регистрации"), "placeholder": "RU", "required": False},
                        {"name": "legal_address", "label": _("Юридический адрес"), "type": "textarea", "required": False},
                        {"name": "contact_name", "label": _("Контактное лицо"), "required": True},
                        {"name": "phone", "label": _("Телефон"), "placeholder": "+7...", "required": False},
                        {"name": "website", "label": _("Сайт"), "required": False},
                        {"name": "categories", "label": _("Бренды и категории"), "type": "textarea", "required": False},
                    ],
                    "fixed_params": {"confirmed": True, "role": "seller"},
                },
            }],
            "actions": [], "suggestions": [], "contextual_actions": [],
        }

    from django.utils import timezone
    from marketplace.models import CompanyVerification, UserRole

    profile = _profile_for_role_extension(request.user)
    _set_if_present(profile, "company_name", params, "legal_name")
    _set_if_present(profile, "tax_id", params, "inn")
    _set_if_present(profile, "contact_name", params)
    _set_if_present(profile, "phone_e164", params, "phone")
    profile.save()

    kyb, _created = CompanyVerification.objects.get_or_create(user=request.user)
    for field in ("legal_name", "inn", "country", "legal_address", "phone", "website", "categories"):
        _set_if_present(kyb, field, params)
    _set_if_present(kyb, "director_name", params, "contact_name")
    if kyb.status == "none":
        kyb.status = "pending"
        kyb.submitted_at = timezone.now()
    kyb.save()

    UserRole.objects.update_or_create(
        user=request.user,
        role="seller",
        operator_role="",
        defaults={"is_enabled": False},
    )
    return {
        "text": _("Заявка на роль поставщика отправлена. До одобрения оператором роль не будет доступна в переключателе."),
        "actions": [
            {"action": "kyb_status", "label": _("Проверить статус")},
            {"action": "upload_kyb_doc", "label": _("Загрузить документы")},
        ],
        "cards": [], "suggestions": [], "contextual_actions": [],
    }


def _handle_start_login(request, params):
    """Вход существующим пользователем — тоже через chat-форму.

    Принимает `role` (buyer | seller | operator) — разные сущности, разные
    кабинеты. Для buyer/seller есть кнопка регистрации, для operator её нет
    (оператора заводит только админ).
    """
    confirmed = confirmation_is_true(params.get("confirmed"))
    role = (params.get("role") or "buyer").lower()

    LOGIN_META = {
        "buyer":    (_("Вход покупателя"), _("С возвращением. Введите логин или e-mail.")),
        "seller":   (_("Вход поставщика"), _("Войдите в кабинет поставщика.")),
        "operator": (_("Вход оператора"),  _("Войдите в операторский кабинет.")),
    }
    title, greeting = LOGIN_META.get(role, LOGIN_META["buyer"])

    def _login_actions():
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
        return actions

    def _login_form_response(message, *, username="", password_error=""):
        return {
            "text": message,
            "cards": [{
                "type": "form",
                "data": {
                    "title": title,
                    "submit_action": "start_login",
                    "submit_label": _("Войти →"),
                    "fields": [
                        {"name": "username", "label": _("Логин или e-mail"),
                         "required": True, "placeholder": "ivanov / you@company.ru",
                         "value": username},
                        {"name": "password", "label": _("Пароль"),
                         "type": "password", "required": True,
                         "error": password_error},
                    ],
                    "fixed_params": {"confirmed": True, "role": role},
                },
            }],
            "actions": _login_actions(),
            "suggestions": [], "contextual_actions": [],
        }

    def _otp_form_response(message, *, otp_error=""):
        return {
            "text": message,
            "cards": [{
                "type": "form",
                "data": {
                    "title": _("Подтверждение входа"),
                    "submit_action": "start_login",
                    "submit_label": _("Подтвердить"),
                    "fields": [
                        {"name": "otp_code", "label": _("Одноразовый код"),
                         "placeholder": "000000", "required": True,
                         "autocomplete": "one-time-code", "error": otp_error},
                    ],
                    "fixed_params": {
                        "confirmed": True,
                        "two_factor": True,
                        "role": role,
                    },
                },
            }],
            "actions": [{
                "action": "start_login",
                "label": _("Вернуться к вводу пароля"),
                "params": {"role": role},
            }],
            "suggestions": [], "contextual_actions": [],
        }

    if not confirmed:
        _clear_pending_2fa(request)
        return _login_form_response(greeting)

    from django.contrib.auth import authenticate, get_user_model, login
    from .security import user_has_enabled_2fa, verify_user_2fa

    if params.get("two_factor"):
        user = _pending_2fa_user(request, flow="start_login", role=role)
        if not user:
            return _login_form_response(
                _("Время подтверждения истекло. Введите логин и пароль ещё раз."),
            )
        if not verify_user_2fa(user, params.get("otp_code") or ""):
            remaining = _record_pending_2fa_failure(request)
            if not remaining:
                return _login_form_response(
                    _("Слишком много неверных кодов. Введите логин и пароль ещё раз."),
                )
            return _otp_form_response(
                _("Введите код из приложения-аутентификатора или резервный код."),
                otp_error=_("Неверный код. Осталось попыток: %(n)s") % {"n": remaining},
            )
        _clear_pending_2fa(request)
    else:
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
            _clear_pending_2fa(request)
            return _login_form_response(
                _("Не удалось войти. Проверьте введённые данные."),
                username=(params.get("username") or "").strip(),
                password_error=_("Неверный логин или пароль."),
            )
        if user_has_enabled_2fa(user):
            _begin_pending_2fa(request, user, flow="start_login", role=role)
            return _otp_form_response(
                _("Для аккаунта включена двухфакторная защита. Введите код из приложения-аутентификатора."),
            )

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # The role selected in the login form must become the active workspace,
    # but only when that role has already been granted to this account.
    from .permissions import _override_allowed, detect_user_role

    real_role = detect_user_role(user)
    real_role_base = "operator" if real_role.startswith("operator") else real_role
    if _override_allowed(user, role) and real_role_base != role:
        request.session["assistant_role_override"] = role
    else:
        request.session.pop("assistant_role_override", None)
    request.session.modified = True
    # Resume pending payment action (anon → клик pay_reserve → login → resume)
    try:
        _attach_anonymous_rfqs_to_user(request, user)
    except Exception:
        logger.exception("attach anonymous RFQs failed on login")
    pending = request.session.get("pending_action")
    if pending and pending.get("action"):
        continuing = (
            _("Продолжаю создание заявки...")
            if pending["action"] == "create_rfq"
            else _("Продолжаю оформление заказа...")
        )
        return {
            "text": _("Вы вошли как «%(u)s».") % {"u": user.username} + " " + continuing,
            "actions": [],
            "cards": [], "suggestions": [], "contextual_actions": [],
            "_post_action": "reload",
        }
    return {
        "text": _("Привет, %(u)s! Перезагружу чат — увидите свои данные.") % {"u": user.username},
        "actions": [{"action": "reload_page", "label": _("Открыть кабинет")}],
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
        serializer = ActionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]
        params = dict(serializer.validated_data.get("params") or {})

        # Клиентский IP (XFF-aware) для ленты важных событий админа: экшены
        # создания заказа/RFQ читают params['_client_ip'] и пишут его в
        # ActivityEvent. Underscore-ключ = UI/meta-параметр (как _label/_url).
        from .security import client_ip

        params["_client_ip"] = client_ip(request)[:64]

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
            if action == "accept_quote" and confirmation_is_true(params.get("confirmed")):
                request.session["pending_action"] = {"action": action, "params": params}
                request.session.modified = True
                return Response({"conversation_id": None,
                                  **_payment_requires_registration_response(action, params)})
            if action not in ANON_ALLOWED_ACTIONS:
                if action in ANON_RESUMABLE_ACTIONS:
                    request.session["pending_action"] = {
                        "action": action,
                        "params": params,
                    }
                    request.session.modified = True
                return Response({"conversation_id": None,
                                  **_registration_required_response()})
            # Прочие whitelisted — выполняем как обычный buyer
            try:
                result = execute_action(
                    None, action, {**params, "_request": request}, request.user, role="buyer",
                )
            except Exception:
                logger.exception("anonymous action failed: %s", action)
                return Response(
                    {"error": _("Не удалось выполнить действие.")},
                    status=500,
                )
            return Response({"conversation_id": None, **result})

        # ── Authenticated flow ──────────────────────────────
        # Special-case: account/session actions need request for login/session.
        # Это смена аккаунта через chat-форму с паролем (вместо JS-prompt).
        if action == "switch_role_login":
            return Response({"conversation_id": None,
                             **_handle_switch_role_login(request, params)})
        if action == "add_account_role":
            return Response({"conversation_id": None,
                             **_handle_add_account_role(request, params)})

        # start_login / start_registration для уже аутентифицированного пользователя:
        # это устаревшие кнопки из stale-сообщений или случайный повторный клик.
        # Вместо «No permission» предлагаем сменить аккаунт или вернуться домой.
        if action in ("start_login", "start_registration"):
            return Response({
                "conversation_id": None,
                "text": _("Вы уже авторизованы как «%(u)s». Хотите войти под другим аккаунтом?") % {"u": request.user.username},
                "actions": [
                    {"action": "switch_role_login", "label": _("Сменить аккаунт")},
                    {"action": "go_home",            "label": _("На главную")},
                ],
                "cards": [], "suggestions": [], "contextual_actions": [],
            })

        execution = None
        operation_id = serializer.validated_data.get("operation_id")
        if operation_id:
            import uuid

            from django.db import transaction

            from .models import ActionExecution

            try:
                operation_id = uuid.UUID(str(operation_id))
            except (TypeError, ValueError, AttributeError):
                return Response({"error": "invalid operation_id"}, status=400)
            with transaction.atomic():
                execution, created = ActionExecution.objects.get_or_create(
                    user=request.user,
                    operation_id=operation_id,
                    defaults={"action": action},
                )
                if not created:
                    execution = ActionExecution.objects.select_for_update().get(
                        pk=execution.pk,
                    )
                    if execution.action != action:
                        return Response(
                            {"error": "operation_id belongs to another action"},
                            status=409,
                        )
                    if execution.completed_at:
                        return Response(execution.response)
                    return Response(
                        {"error": "operation is already in progress"},
                        status=409,
                    )

        conv_id = serializer.validated_data.get("conversation_id")

        from .conv_category import category_for_action, title_for_action
        label = (params.get("_label") or "").strip() or action
        current_role = detect_user_role(request.user, request=request)

        if conv_id:
            from .conversation_access import accessible_conversations
            conv = (
                accessible_conversations(request.user, current_role)
                .filter(id=conv_id)
                .first()
            )
            if conv is None:
                if execution:
                    execution.delete()
                raise Http404
            # Служебная команда не должна менять категорию общего обращения
            # или отправлять его историю в ИИ. Для команды создаём отдельный
            # обычный разговор, а текстовые сообщения остаются в поддержке.
            if conv.category == "support" and conv.participant_links.exists():
                conv = Conversation.objects.create(
                    user=request.user,
                    role=current_role,
                    category=category_for_action(action),
                    title=title_for_action(action, label)[:200],
                )
        else:
            # Явное действие вне открытого диалога начинает новый диалог.
            # Повторное использование "похожего" чата делало кнопку
            # «Новый чат» фиктивной и смешивало независимые сценарии.
            conv = Conversation.objects.create(
                user=request.user,
                role=current_role,
                category=category_for_action(action),
                title=title_for_action(action, label)[:200],
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
            result = execute_action(
                conv, action, {**params, "_request": request}, request.user, role=current_role,
            )
        except Exception:
            if execution:
                execution.delete()
            logger.exception("assistant action failed: %s", action)
            return Response(
                {"error": _("Не удалось выполнить действие.")},
                status=500,
            )

        # Авто-reload после переключения view-as / выхода — чтобы middleware
        # подхватил новую сессию и подменил request.user.
        if action in ("op_view_as_supplier", "op_exit_view_as"):
            result = {**result, "_post_action": "reload"}
        storage_response = result.pop("_storage_response", None)
        payload = {
            "conversation_id": str(conv.id),
            **result,
        }
        if execution:
            import json

            from django.utils import timezone
            from rest_framework.utils.encoders import JSONEncoder

            stored_payload = json.loads(json.dumps(payload, cls=JSONEncoder))
            if storage_response:
                stored_payload.update(storage_response)
            execution.response = stored_payload
            execution.completed_at = timezone.now()
            execution.save(update_fields=["response", "completed_at"])
        return Response(payload)


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
            Message.objects.filter(
                id=ser.validated_data["message_id"],
            ).filter(
                _Q(conversation__user=request.user)
                | _Q(conversation__participant_links__user=request.user)
            ).distinct(),
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

    def get(self, request):
        from .commands import suggestions_for_role

        role = request.query_params.get("role") or detect_user_role(request.user, request=request)
        return Response({
            "role": role,
            "suggestions": suggestions_for_role(role),
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
        from .commands import commands_for_role, suggestions_for_role
        from .permissions import display_role_label

        if not request.user.is_authenticated:
            return Response({
                "role": "buyer",
                "role_label": _("Публичный просмотр"),
                "role_override": None,
                "user_name": _("Гость"),
                "suggestions": [],
                "commands": commands_for_role("guest", anonymous=True),
                "latest_conversation_id": None,
                "anonymous": True,
            })
        role = detect_user_role(request.user, request=request)
        from .conversation_access import accessible_conversations
        latest = accessible_conversations(request.user, role).order_by("-updated_at").first()
        pending_action = request.session.pop("pending_action", None)
        if pending_action:
            request.session.modified = True
        return Response({
            "role": role,
            "role_label": display_role_label(role),
            "role_override": (request.session.get("assistant_role_override") if hasattr(request, "session") else None),
            "roles": user_allowed_role_tabs(request.user),
            "user_name": request.user.get_full_name() or request.user.username,
            "suggestions": suggestions_for_role(role),
            "commands": commands_for_role(role),
            "latest_conversation_id": str(latest.id) if latest else None,
            "pending_action": pending_action,
        })


class RoleSwitchView(APIView):
    """POST /api/assistant/role/  body: {"role": "buyer"|"seller"|"operator"|"admin"}

    Переключает только роли, уже выданные текущему пользователю. Новая роль
    оформляется отдельным потоком `add_account_role`; чужой аккаунт и его
    пароль этот endpoint не принимает.

    Anonymous: всегда отвечает `buyer` (без 403) — гость не может
    переключиться на seller/operator, это требует регистрации.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from .permissions import _normalize_override, _override_allowed

        if not request.user.is_authenticated:
            return Response({"role": "buyer", "override": None, "anonymous": True})

        raw = request.data.get("role")
        norm = _normalize_override(raw)
        if not norm or norm not in {"buyer", "seller", "operator", "admin"}:
            return Response({"error": f"unsupported role '{raw}'"}, status=400)

        current_role = detect_user_role(request.user, request=request)
        current_normalized = "operator" if current_role.startswith("operator") else current_role
        if current_normalized == norm:
            return Response({"role": current_role, "override": None, "no_change": True})

        if _override_allowed(request.user, norm):
            real_role = detect_user_role(request.user)
            real_normalized = "operator" if real_role.startswith("operator") else real_role
            if real_normalized == norm:
                request.session.pop("assistant_role_override", None)
            else:
                request.session["assistant_role_override"] = norm
            request.session.modified = True
            return Response({
                "role": detect_user_role(request.user, request=request),
                "override": request.session.get("assistant_role_override"),
                "switched": True,
                "same_account": True,
            })

        return Response({
            "error": _("Эта роль не подключена к вашему аккаунту."),
            "target_role": norm,
            "action": "add_account_role",
        }, status=403)


# ── Projects API ────────────────────────────────────────────
from .models import Project


_PROJECT_TEXT_LIMITS = {
    "name": 200,
    "code": 50,
    "customer": 200,
    "description": 5_000,
}
_PROJECT_DOT_COLORS = {choice for choice, _label in Project.DOT_COLORS}
_PROJECT_MAX_TAGS = 20
_PROJECT_MAX_TAG_LENGTH = 80


def _clean_project_payload(data, *, partial: bool) -> dict:
    if not hasattr(data, "get"):
        raise ValueError(_("Тело запроса должно быть объектом."))
    cleaned = {}
    for field, max_length in _PROJECT_TEXT_LIMITS.items():
        if partial and field not in data:
            continue
        value = data.get(field)
        if value is None:
            value = _("Новый проект") if field == "name" and not partial else ""
        if not isinstance(value, str):
            raise ValueError(_("Поля проекта должны содержать текст."))
        value = value.strip()
        if field == "name" and not value:
            raise ValueError(_("Название проекта не может быть пустым."))
        if len(value) > max_length:
            raise ValueError(
                _("Поле «%(field)s» слишком длинное.") % {"field": field}
            )
        cleaned[field] = value

    if not partial or "tags" in data:
        tags = data.get("tags", [])
        if not isinstance(tags, list) or len(tags) > _PROJECT_MAX_TAGS:
            raise ValueError(
                _("Допускается не более %(count)s тегов.")
                % {"count": _PROJECT_MAX_TAGS}
            )
        clean_tags = []
        for tag in tags:
            if not isinstance(tag, str):
                raise ValueError(_("Каждый тег должен содержать текст."))
            tag = tag.strip()
            if len(tag) > _PROJECT_MAX_TAG_LENGTH:
                raise ValueError(_("Тег проекта слишком длинный."))
            if tag and tag not in clean_tags:
                clean_tags.append(tag)
        cleaned["tags"] = clean_tags

    if not partial or "dot_color" in data:
        color = data.get("dot_color", "green")
        if color not in _PROJECT_DOT_COLORS:
            raise ValueError(_("Неизвестный цвет проекта."))
        cleaned["dot_color"] = color
    return cleaned


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
        try:
            values = _clean_project_payload(request.data, partial=False)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        p = Project.objects.create(owner=request.user, **values)
        return Response({"id": str(p.id), "name": p.name}, status=201)


class ProjectUpdateView(APIView):
    """PATCH-обновление полей проекта (name, code, customer, tags, dot_color, description)."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, project_id):
        p = get_object_or_404(Project, id=project_id, owner=request.user, is_active=True)
        try:
            values = _clean_project_payload(request.data, partial=True)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        for field, value in values.items():
            setattr(p, field, value)
        if values:
            p.save(update_fields=[*values.keys(), "updated_at"])
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
        name = f.name or "document"
        try:
            from marketplace.upload_security import validate_uploaded_file
            validate_uploaded_file(
                f,
                allowed_ext={".pdf", ".png", ".jpg", ".jpeg", ".heic"},
                max_bytes=10 * 1024 * 1024,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("KYB document validation failed user_id=%s", request.user.id)
            return Response(
                {"error": _("Не удалось проверить файл.")},
                status=400,
            )
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
                "url": (
                    f"/api/assistant/kyb/{request.user.id}/doc/{kind}/file/"
                    if saved
                    else None
                ),
            }, status=201)
        except Exception:
            logger.exception("KYB document storage failed")
            return Response({"error": _("Не удалось сохранить документ.")}, status=500)


class KYBDocumentFileView(APIView):
    """Download a KYB document after owner/operator authorization."""

    permission_classes = [IsAuthenticated]
    KIND_FIELD = {
        "charter": "doc_charter",
        "egrul": "doc_egrul",
        "passport": "doc_passport",
        "dealership": "doc_dealership",
        "bank": "doc_bank",
    }

    def get(self, request, user_id, kind):
        import os

        from django.http import FileResponse
        from marketplace.models import CompanyVerification

        field_name = self.KIND_FIELD.get(kind)
        if not field_name:
            return Response({"error": _("Неизвестный тип документа.")}, status=404)
        role = detect_user_role(request.user, request=request)
        if (
            request.user.id != user_id
            and not request.user.is_staff
            and role != "admin"
            and not role.startswith("operator")
        ):
            return Response({"error": _("нет доступа")}, status=403)

        verification = get_object_or_404(CompanyVerification, user_id=user_id)
        stored_file = getattr(verification, field_name)
        if not stored_file:
            return Response({"error": _("файл не найден")}, status=404)
        try:
            response = FileResponse(
                stored_file.open("rb"),
                as_attachment=True,
                filename=os.path.basename(stored_file.name) or f"{kind}.bin",
            )
            response["Cache-Control"] = "private, no-store"
            return response
        except Exception:
            return Response({"error": _("файл не найден")}, status=404)


class SettlementReportCsvView(APIView):
    """Internal finance register with invoices and immutable bank operations."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        import csv
        import io

        from django.http import StreamingHttpResponse
        from django.utils import timezone
        from marketplace.models import SettlementInvoice, SettlementPayment

        role = detect_user_role(request.user, request=request)
        if role not in {"admin", "operator_payment"}:
            return Response({"error": _("нет доступа")}, status=403)
        order_id = request.query_params.get("order_id")
        if order_id:
            try:
                order_id = int(order_id)
            except (TypeError, ValueError):
                return Response({"error": _("некорректный номер заказа")}, status=400)

        def safe_cell(value):
            text = str(value if value is not None else "")
            if text.startswith(("=", "+", "-", "@")):
                return "'" + text
            return text

        def line(values):
            buffer = io.StringIO()
            csv.writer(buffer, delimiter=";").writerow(
                [safe_cell(value) for value in values]
            )
            return buffer.getvalue()

        def rows():
            yield "\ufeff"
            yield line([
                "Тип", "Номер", "Заказ", "Направление", "Этап",
                "Контрагент", "Статус", "Сумма", "Оплачено", "Остаток",
                "Валюта", "Срок оплаты", "Банковская операция", "Дата операции",
                "Подтверждающий файл", "Оператор", "Комментарий",
            ])
            invoices = SettlementInvoice.objects.select_related(
                "contract", "order"
            )
            if order_id:
                invoices = invoices.filter(order_id=order_id)
            invoices = invoices.order_by("created_at").iterator(chunk_size=500)
            for invoice in invoices:
                counterparty = (invoice.contract.counterparty_snapshot or {}).get(
                    "legal_name", ""
                )
                yield line([
                    "Счёт", invoice.number, f"ORD-{invoice.order_id}",
                    invoice.get_direction_display(), invoice.get_stage_display(),
                    counterparty, invoice.get_status_display(), invoice.amount,
                    invoice.paid_amount, invoice.outstanding_amount, invoice.currency,
                    invoice.due_date.strftime("%d.%m.%Y"), "", "", "", "", "",
                ])
            payments = SettlementPayment.objects.select_related(
                "invoice__contract", "confirmed_by", "reversed_by"
            )
            if order_id:
                payments = payments.filter(invoice__order_id=order_id)
            payments = payments.order_by("paid_at", "id").iterator(chunk_size=500)
            for payment in payments:
                invoice = payment.invoice
                counterparty = (invoice.contract.counterparty_snapshot or {}).get(
                    "legal_name", ""
                )
                yield line([
                    "Банковская операция", invoice.number, f"ORD-{invoice.order_id}",
                    payment.get_direction_display(), invoice.get_stage_display(),
                    counterparty, payment.get_status_display(), payment.amount,
                    "", "", payment.currency, "", payment.bank_reference,
                    timezone.localtime(payment.paid_at).strftime("%d.%m.%Y %H:%M"),
                    (
                        f"/api/assistant/settlements/payments/{payment.id}/proof/"
                        if payment.proof_file else ""
                    ),
                    (
                        payment.reversed_by.username
                        if payment.status == "reversed" and payment.reversed_by_id
                        else payment.confirmed_by.username
                    ),
                    payment.reversal_reason if payment.status == "reversed" else payment.note,
                ])

        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="settlement-register-{timezone.localdate():%Y-%m-%d}.csv"'
        )
        response["Cache-Control"] = "private, no-store"
        return response


class SettlementPaymentProofView(APIView):
    """Finance-only upload and download of bank operation evidence."""

    permission_classes = [IsAuthenticated]
    from rest_framework.parsers import FormParser, MultiPartParser
    parser_classes = [MultiPartParser, FormParser]

    @staticmethod
    def _payment(request, payment_id):
        from marketplace.models import SettlementPayment

        role = detect_user_role(request.user, request=request)
        if role not in {"admin", "operator_payment"}:
            return None
        return SettlementPayment.objects.select_related("invoice").filter(
            id=payment_id
        ).first()

    def get(self, request, payment_id):
        import os

        from django.http import FileResponse

        payment = self._payment(request, payment_id)
        if not payment:
            return Response({"error": _("Нет доступа.")}, status=403)
        if not payment.proof_file:
            return Response({"error": _("Подтверждающий файл не загружен.")}, status=404)
        try:
            stream = payment.proof_file.open("rb")
        except (FileNotFoundError, OSError):
            logger.exception("settlement proof missing payment_id=%s", payment.id)
            return Response({"error": _("Файл подтверждения недоступен.")}, status=404)
        response = FileResponse(
            stream,
            as_attachment=True,
            filename=os.path.basename(payment.proof_file.name),
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    def post(self, request, payment_id):
        from django.conf import settings
        from marketplace.upload_security import safe_upload_name, validate_uploaded_file

        payment = self._payment(request, payment_id)
        if not payment:
            return Response({"error": _("Нет доступа.")}, status=403)
        if payment.proof_file:
            return Response(
                {"error": _("Подтверждение уже загружено и сохранено в журнале.")},
                status=409,
            )
        uploaded = request.FILES.get("file")
        try:
            ext = validate_uploaded_file(
                uploaded,
                allowed_ext={".pdf", ".png", ".jpg", ".jpeg", ".webp"},
                max_bytes=min(
                    int(getattr(settings, "MAX_ORDER_DOCUMENT_BYTES", 20 * 1024 * 1024)),
                    20 * 1024 * 1024,
                ),
            )
            uploaded.name = safe_upload_name(uploaded, ext)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("settlement proof validation failed payment_id=%s", payment.id)
            return Response({"error": _("Не удалось проверить файл.")}, status=400)
        payment.proof_file = uploaded
        payment.save(update_fields=["proof_file"])
        from marketplace.models import OrderEvent

        OrderEvent.objects.create(
            order_id=payment.invoice.order_id,
            event_type="document_uploaded",
            source="operator",
            actor=request.user,
            meta={
                "kind": "settlement_payment_proof",
                "settlement_payment_id": payment.id,
                "settlement_invoice_id": payment.invoice_id,
                "bank_reference": payment.bank_reference,
                "file_name": uploaded.name,
            },
        )
        return Response(
            {
                "ok": True,
                "payment_id": payment.id,
                "name": uploaded.name,
                "url": f"/api/assistant/settlements/payments/{payment.id}/proof/",
            },
            status=201,
        )


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
        try:
            from marketplace.upload_security import validate_uploaded_file
            validate_uploaded_file(
                f,
                allowed_ext={
                    ".xlsx", ".xls", ".csv", ".pdf", ".docx", ".doc",
                    ".dwg", ".dxf", ".png", ".jpg", ".jpeg",
                },
                max_bytes=50 * 1024 * 1024,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception:
            logger.exception(
                "project document validation failed user_id=%s project_id=%s",
                request.user.id,
                p.id,
            )
            return Response(
                {"error": _("Не удалось проверить файл.")},
                status=400,
            )
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
                    d_url = (
                        f"/media/{str(doc.file.name).lstrip('/')}"
                        if doc.file
                        else ""
                    )
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
        except Exception:
            logger.exception("project document storage failed")
            return Response({"error": _("Не удалось сохранить документ.")}, status=500)


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
            response = FileResponse(
                doc.file.open("rb"),
                as_attachment=False,
                filename=doc.name or "document",
            )
            response["Cache-Control"] = "private, no-store"
            return response
        except Exception:
            return Response({"error": _("файл не найден")}, status=404)


class OrderDocumentUploadView(APIView):
    """Store checklist evidence and bind it to the current order stage."""

    permission_classes = [IsAuthenticated]
    from rest_framework.parsers import FormParser, MultiPartParser
    parser_classes = [MultiPartParser, FormParser]

    DOC_TYPES = {
        "invoice": "invoice",
        "packing_list": "packing_list",
        "certificates": "certificate",
        "declaration": "customs",
        "transport_invoice": "other",
        "ttn_rf": "other",
        "signed_docs": "other",
    }

    def post(self, request, order_id):
        import hashlib

        from django.conf import settings

        from marketplace.models import Order, OrderDocument, OrderEvent, OrderItem
        from marketplace.upload_security import safe_upload_name, validate_uploaded_file

        from .actions import _stage_checklist, complete_trigger

        order = get_object_or_404(Order, id=order_id)
        status_code = (request.data.get("status") or "").strip()
        trigger_id = (request.data.get("trigger_id") or "").strip()
        if order.status != status_code:
            return Response(
                {"error": _("Статус заказа уже изменился. Обновите карточку.")},
                status=409,
            )
        trigger = next(
            (
                item for item in _stage_checklist(
                    status_code,
                    order.incoterm or "FOB",
                )
                if item["id"] == trigger_id
            ),
            None,
        )
        if not trigger or trigger.get("type") != "upload":
            return Response(
                {"error": _("Этот пункт не принимает загрузку файла.")},
                status=400,
            )

        role = detect_user_role(request.user, request=request)
        allowed = role == "admin" or role.startswith("operator")
        document_audience = "participants"
        document_seller = None
        if role == "buyer":
            allowed = (
                order.buyer_id == request.user.id
                and status_code == "delivered"
                and trigger_id == "signed_docs"
            )
            document_audience = "buyer"
        elif role == "seller":
            from .seller_actions import _effective_seller

            seller = _effective_seller(request.user)
            allowed = OrderItem.objects.filter(
                order=order,
                part__seller=seller,
            ).exists()
            document_audience = "seller"
            document_seller = seller
        if not allowed:
            return Response({"error": _("Нет доступа к заказу.")}, status=403)

        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response({"error": _("Файл не приложен.")}, status=400)
        try:
            ext = validate_uploaded_file(
                uploaded,
                allowed_ext={
                    ".pdf", ".png", ".jpg", ".jpeg", ".webp",
                    ".doc", ".docx", ".xls", ".xlsx",
                },
                max_bytes=int(settings.MAX_ORDER_DOCUMENT_BYTES),
            )
            uploaded.name = safe_upload_name(uploaded, ext)
            digest = hashlib.sha256()
            for chunk in uploaded.chunks():
                digest.update(chunk)
            uploaded.seek(0)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception:
            logger.exception(
                "order evidence validation failed user_id=%s order_id=%s",
                request.user.id,
                order.id,
            )
            return Response(
                {"error": _("Не удалось проверить файл.")},
                status=400,
            )

        document = OrderDocument.objects.create(
            order=order,
            doc_type=self.DOC_TYPES.get(trigger_id, "other"),
            audience=document_audience,
            seller=document_seller,
            title=str(trigger["label"])[:255],
            file_obj=uploaded,
            uploaded_by=request.user,
        )
        result = complete_trigger(
            {
                "order_id": order.id,
                "status": status_code,
                "trigger_id": trigger_id,
                "document_id": document.id,
                "sha256": digest.hexdigest(),
            },
            request.user,
            role,
        )
        order.refresh_from_db(fields=["logistics_meta"])
        evidence = (
            (((order.logistics_meta or {}).get("triggers") or {})
             .get(status_code, {}))
            .get(trigger_id)
        )
        if (
            not result.action_succeeded
            or not isinstance(evidence, dict)
            or evidence.get("document_id") != document.id
        ):
            document.file_obj.delete(save=False)
            document.delete()
            return Response(
                {"error": str(result.text) or _("Не удалось связать документ с этапом.")},
                status=409,
            )

        OrderEvent.objects.create(
            order=order,
            event_type="document_uploaded",
            source=role,
            actor=request.user,
            meta={
                "document_id": document.id,
                "trigger_id": trigger_id,
                "status": status_code,
                "sha256": digest.hexdigest(),
            },
        )
        return Response(
            {
                "ok": True,
                "document_id": document.id,
                "trigger_id": trigger_id,
                "name": uploaded.name,
            },
            status=201,
        )


class OrderDocumentFileView(APIView):
    """GET → стримит PDF/файл документа заказа (инвойс, packing list, QC и т.д.).

    Зачем отдельная вьюха, а не сырой /media/-URL: на проде user-media по
    /media/order_documents/ НЕ раздаётся (SERVE_MEDIA=False, nginx/WhiteNoise
    отдают только /static/), поэтому ссылка на счёт возвращала 404. Здесь файл
    стримится самим Django + проверкой доступа — работает и локально, и на проде.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id, doc_id):
        import os

        from django.http import FileResponse
        from marketplace.models import Order, OrderDocument
        order = get_object_or_404(Order, id=order_id)
        doc = get_object_or_404(OrderDocument, id=doc_id, order=order)
        # Доступ: покупатель-владелец, продавец с позициями в заказе или
        # пользователь с явно выданной операторской ролью.
        _role = detect_user_role(request.user, request=request)
        if _role == "seller":
            from assistant.seller_actions import _effective_seller
            from marketplace.order_access import seller_can_access_document

            allowed = seller_can_access_document(
                _effective_seller(request.user),
                doc,
            )
        else:
            if _role.startswith("operator") or _role == "admin":
                allowed = True
            else:
                from marketplace.order_access import buyer_can_access_document

                allowed = buyer_can_access_document(request.user, doc)
        if not allowed:
            return Response({"error": _("нет доступа")}, status=403)
        if not doc.file_obj:
            return Response({"error": _("файл не найден")}, status=404)
        try:
            response = FileResponse(
                doc.file_obj.open("rb"), as_attachment=False,
                filename=(
                    os.path.basename(doc.file_obj.name)
                    or doc.title
                    or f"ORD-{order_id}-document"
                ),
            )
            response["Cache-Control"] = "private, no-store"
            return response
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
                "url": (
                    f"/api/assistant/projects/{p.id}/documents/{d.id}/file/"
                    if d.file
                    else None
                ),
                "meta": d.meta,
                "uploaded_at": d.uploaded_at.strftime("%d.%m.%Y"),
                "drawing_id": None,
                "oem": "",
            }
            if d.doctype == "drawing":
                dr = Drawing.objects.filter(project_doc=d).first()
                if not dr:  # backfill для старых документов
                    d_url = (
                        f"/media/{str(d.file.name).lstrip('/')}"
                        if d.file
                        else ""
                    )
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
        # Пока заявки и заказы не связаны с Project отдельным FK, показываем
        # честное пустое состояние. Подмешивать демонстрационные сделки в
        # пользовательский проект нельзя: их легко принять за реальные данные.
        role = detect_user_role(request.user, request=request)
        is_seller = (role == "seller")
        is_operator = role.startswith("operator")
        rfqs = []
        orders = []
        participants = []

        if is_operator:
            stats = {
                "positions": {"count": 0, "awaiting": 0},
                "logistics": {"count": 0, "in_transit": 0, "at_customs": 0,
                              "earliest_eta": None, "delays": 0},
                "customs": {"at_customs": 0, "hs_pending": 0,
                            "declarations": 0, "sanctions_risk": 0},
                "payments": {"escrow_usd": 0, "awaiting_payout": 0,
                             "paid_by_buyer_usd": 0, "margin_pct": 0},
                "deal_turnover": {"value_usd": 0, "margin_pct": 0},
            }
        elif is_seller:
            stats = {
                "incoming_rfqs": {"count": 0, "awaiting": 0},
                "active_orders": {"count": 0, "value_usd": 0, "to_ship": 0},
                "catalog_items": {"count": 0, "with_drawing": 0},
                "revenue_mtd": {"value_usd": 0, "delta_pct": 0},
            }
        else:
            stats = {
                "open_rfqs": {"count": 0, "semi": 0},
                "active_orders": {"count": 0, "value_usd": 0},
                "in_transit": {"count": 0, "earliest_eta": None},
                "spend_mtd": {"value_usd": 0, "delta_pct": 0},
            }

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
        from marketplace.order_access import seller_principal
        from marketplace.participant_identity import (
            customer_label,
            partner_label,
            redact_party_contacts,
        )

        rfq = get_object_or_404(
            RFQ.objects.select_related("created_by__profile"),
            id=rfq_id,
        )

        # SECURITY (IDOR fix): доступ к RFQ имеют только:
        #  • владелец (создатель RFQ),
        #  • продавец-адресат (получивший Notification по этому RFQ),
        #  • пользователь с явно выданной ролью оператора/администратора.
        # Для остальных — 404 (не 403), чтобы не утекало само наличие RFQ.
        is_owner = (rfq.created_by_id == request.user.id)
        access_role = detect_user_role(request.user, request=request)
        is_operator = access_role.startswith("operator") or access_role == "admin"
        is_recipient = False
        if not (is_owner or is_operator):
            recipient_urls = (
                Notification.objects.filter(
                    kind="rfq",
                    user_id=request.user.id,
                    url__contains=str(rfq.id),
                )
                .values_list("url", flat=True)
            )
            is_recipient = any(
                _notification_targets_rfq(url, rfq.id)
                for url in recipient_urls
            )
        if not (is_owner or is_operator or is_recipient):
            raise Http404("RFQ not found")

        # Для не-владельцев скрываем чувствительные поля покупателя
        redact_pii = not (is_owner or is_operator)

        from marketplace.fx import to_usd_float  # живой бирж. курс
        items = []
        total_usd = 0.0
        item_qs = rfq.items.select_related(
            "matched_part__brand",
            "matched_part__seller__profile",
        )
        recipient_seller = None
        if is_recipient and not (is_owner or is_operator):
            recipient_seller = seller_principal(request.user)
            item_qs = item_qs.filter(matched_part__seller=recipient_seller)
        for it in item_qs:
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
                "article": (
                    redact_party_contacts(it.query)
                    if recipient_seller
                    else it.query
                ),
                "qty": qty,
                "state": "matched" if mp else ("no_match" if it.state == "needs_review" else "pending"),
                "match": mp.title if mp else None,
                "brand": (mp.brand.name if (mp and mp.brand) else None),
                "supplier": (
                    getattr(mp, "supplier_name", None)
                    if mp and is_operator
                    else (partner_label(mp.seller, fallback_id=mp.id) if mp else None)
                ),
                "price": show_price,
                "currency": show_ccy,
            })

        # Quotes-аналитика
        quotes = Quote.objects.filter(rfq=rfq, direction="seller_to_buyer")
        if recipient_seller:
            quotes = quotes.filter(seller=recipient_seller)
        quotes_count = quotes.values_list("seller_id", flat=True).distinct().count()
        # Supplier reach: сколько уведомлений было разослано.
        # _notify пишет url'ы двух форматов:
        #   /chat/?rfq=<id>           — общая ссылка (старый формат)
        #   /chat/rfq/<id>/?source=…  — детальная страница (новый формат)
        # Считаем оба варианта, по distinct user_id.
        sent_count = (
            1
            if recipient_seller
            else len({
                user_id
                for user_id, url in (
                    Notification.objects.filter(
                        kind="rfq",
                        url__contains=str(rfq.id),
                    )
                    .values_list("user_id", "url")
                )
                if _notification_targets_rfq(url, rfq.id)
            })
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
            payload["customer_name"] = customer_label(rfq.created_by, fallback_id=rfq.id)
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

        from .drawings_access import build_watermarked_copy, can_access, record_access
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

        # Внешняя ссылка после проверки всё равно остаётся общей и может быть
        # переслана другому человеку. Такие записи нужно перенести в закрытое
        # хранилище; выдавать фиктивный ?wm= параметр небезопасно.
        file_url = (drawing.file_url or "").strip()
        if file_url.startswith(("http://", "https://")) and "/media/" not in file_url:
            record_access(
                request.user,
                drawing,
                "denied",
                request=request,
                note="external file must be migrated to protected storage",
            )
            return Response(
                {"ok": False, "error": "Файл нужно перенести в защищённое хранилище."},
                status=409,
            )

        # Стримим САМ файл (после проверки доступа выше), а не JSON со ссылкой
        # на /media/: иначе приватный чертёж открыт всем по прямой ссылке.
        # Работает одинаково на runserver (локально) и на проде.
        import urllib.parse

        from django.core.files.storage import default_storage
        from django.http import FileResponse
        # file_url хранится URL-кодированным (кириллица → %D0..); на диске путь
        # декодирован — раскодируем, иначе default_storage.exists() = False.
        rel = urllib.parse.unquote(file_url.split("/media/", 1)[-1].lstrip("/"))
        if not rel or not default_storage.exists(rel):
            return Response({"ok": False, "error": "файл чертежа не найден"}, status=404)
        fname = drawing.file_name or rel.rsplit("/", 1)[-1]
        source = default_storage.open(rel, "rb")
        try:
            protected = build_watermarked_copy(source, fname, request.user, drawing)
        except Exception:
            source.close()
            logger.exception("drawing watermark failed drawing=%s", drawing.id)
            return Response(
                {"ok": False, "error": "Не удалось подготовить защищённую копию файла."},
                status=503,
            )
        if protected:
            source.close()
            protected_file, content_type = protected
            record_access(request.user, drawing, action, request=request)
            record_access(request.user, drawing, "watermark_added", request=request)
            return FileResponse(
                protected_file,
                as_attachment=(action == "download"),
                filename=fname,
                content_type=content_type,
            )
        # CAD-файлы нельзя визуально маркировать без изменения их структуры.
        # Они остаются защищены проверкой доступа и потоковой выдачей, но в
        # журнале не утверждается, что водяной знак был нанесён.
        record_access(request.user, drawing, action, request=request)
        return FileResponse(
            source,
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

        ext = (os.path.splitext(f.name)[1] or "").lstrip(".").lower()
        try:
            from marketplace.upload_security import validate_uploaded_file
            validate_uploaded_file(
                f,
                allowed_ext={".pdf", ".dwg", ".dxf", ".step", ".stp", ".iges", ".igs", ".stl", ".png", ".jpg", ".jpeg"},
                max_bytes=50 * 1024 * 1024,
            )
        except ValueError as exc:
            return Response({"ok": False, "error": str(exc)}, status=400)
        except Exception:
            logger.exception("drawing validation failed user_id=%s", request.user.id)
            return Response(
                {"ok": False, "error": _("Не удалось проверить файл.")},
                status=400,
            )
        FMT = {"pdf": "pdf", "dwg": "dwg", "dxf": "dxf", "step": "step",
               "stp": "step", "iges": "iges", "igs": "iges", "stl": "stl",
               "png": "png", "jpg": "jpg", "jpeg": "jpg"}
        file_format = FMT.get(ext, "pdf")

        safe_name = get_valid_filename(f.name)[:200] or "drawing"
        path = default_storage.save(f"drawings/{request.user.id}/{safe_name}", f)
        # Храним стабильную внутреннюю ссылку, а не временный S3 presigned URL.
        # /media/ закрыт на reverse proxy; DrawingFileView извлекает storage key
        # и выдаёт файл только после проверки доступа.
        file_url = f"/media/{str(path).lstrip('/')}"

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
                    "url": _safe_local_url(n.url),
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

    Запрос всегда проверяется по Stripe-Signature. Без
    STRIPE_WEBHOOK_SECRET обработчик закрыт во всех режимах.
    """
    permission_classes = []
    authentication_classes = []

    def post(self, request):
        import json

        from django.conf import settings
        from django.core.exceptions import RequestDataTooBig

        from .payments import dispatch_webhook
        from .payments_engines import verify_webhook_signature

        max_body_bytes = int(
            getattr(settings, "PAYMENT_CALLBACK_MAX_BODY_BYTES", 64 * 1024)
        )
        try:
            content_length = int(request.META.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length > max_body_bytes:
            return Response(
                {"received": False, "error": "payload too large"},
                status=413,
            )
        try:
            raw_body = request.body
        except RequestDataTooBig:
            return Response(
                {"received": False, "error": "payload too large"},
                status=413,
            )
        if len(raw_body) > max_body_bytes:
            return Response(
                {"received": False, "error": "payload too large"},
                status=413,
            )
        signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        if not verify_webhook_signature(raw_body, signature):
            return Response({"received": False, "error": "invalid signature"}, status=400)
        try:
            event = json.loads(raw_body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response(
                {"received": False, "error": "invalid payload"},
                status=400,
            )
        if not isinstance(event, dict):
            return Response(
                {"received": False, "error": "invalid payload"},
                status=400,
            )
        result = dispatch_webhook(event)
        return Response(result)


class NotificationMarkReadView(APIView):
    """POST /api/assistant/notifications/<id>/read/  → mark single as read.

    POST /api/assistant/notifications/read-all/   → mark all as read.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, notif_id=None):
        from django.db.models import Count

        from .consumers import push_notification_state_to_user
        from marketplace.models import Notification

        if notif_id is None:
            updated = Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
            push_notification_state_to_user(
                request.user.id,
                unread_count=0,
                unread_by_kind={},
            )
            return Response({"ok": True, "updated": updated, "unread_count": 0})
        n = get_object_or_404(Notification, id=notif_id, user=request.user)
        if not n.is_read:
            n.is_read = True
            n.save(update_fields=["is_read"])
        unread_qs = Notification.objects.filter(user=request.user, is_read=False)
        unread_count = unread_qs.count()
        unread_by_kind = {
            row["kind"]: row["count"]
            for row in unread_qs.values("kind").annotate(count=Count("id"))
        }
        push_notification_state_to_user(
            request.user.id,
            unread_count=unread_count,
            unread_by_kind=unread_by_kind,
        )
        return Response({"ok": True, "id": n.id, "unread_count": unread_count})
