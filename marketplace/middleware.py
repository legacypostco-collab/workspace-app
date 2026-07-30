"""
Custom middleware: applies the authenticated user's preferred language.

Приоритет:
  1. cookie `django_language` (явный выбор пользователя — его уже активировал
     LocaleMiddleware). Профиль подлечивается под cookie, чтобы не расходились.
  2. UserProfile.language (если cookie нет — напр. свежий вход с устройства).

Раньше middleware безусловно форсил profile.language поверх cookie → если POST
`/api/set-language/` не сохранил профиль (CSRF/ошибка), возникал split-brain
(cookie=en, profile=ru) и страница оставалась русской при выбранном English.

Anonymous users fall back to the session-stored or Accept-Language value
that Django's LocaleMiddleware already resolved.

Must run AFTER `django.middleware.locale.LocaleMiddleware` and AFTER
`AuthenticationMiddleware` so `request.user` is populated.
"""
from django.conf import settings
import json

from django.http import JsonResponse, HttpResponseRedirect
from django.utils import translation

# ────────────────────────────────────────────────────────────────────
# Legacy cabinet handling.
#
# Старые ролевые кабинеты /buyer/*, /seller/* и /operator/* отключены
# маршрутизатором с ответом 410. Middleware намеренно не перехватывает их:
# скрытый редирект в чат маскировал устаревшие ссылки и запускал неожиданный
# сценарий вместо явной ошибки.
#
# /dashboard и /admin_panel остаются только как переходные адреса в чат.
# Удалённый /admin-panel/* обрабатывается маршрутизатором единым ответом 410.
# ────────────────────────────────────────────────────────────────────

_LEGACY_PREFIXES = (
    "/dashboard",
    "/admin_panel",
)

_LEGACY_WHITELIST = (
    "/admin/",                  # Django admin (отдельный staff-UI)
    "/admin_panel/api/",        # JSON API для интеграций
)

_LEGACY_EXACT_REDIRECTS = {
    "/team/": "/chat/?new=1&run=seller_team",
    "/team": "/chat/?new=1&run=seller_team",
}

_AUTHENTICATED_CHAT_REDIRECTS = {
    "/demo-center/": "/chat/?workspace=1",
    "/demo-center": "/chat/?workspace=1",
    "/demo/": "/chat/?workspace=1",
    "/demo": "/chat/?workspace=1",
    "/reports/kpi/": "/chat/?new=1&run=get_analytics",
    "/reports/kpi": "/chat/?new=1&run=get_analytics",
}

_AUTHENTICATED_CHAT_PREFIXES = (
    "/password_reset",
    "/password-reset",
    "/reset/",
)


class LegacyCabinetRedirectMiddleware:
    """Перенаправляет только переходные общие страницы в chat-first UI."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info or ""
        user = getattr(request, "user", None)
        is_auth = bool(getattr(user, "is_authenticated", False))
        # Whitelist первым делом — пускаем без редиректа
        for w in _LEGACY_WHITELIST:
            if path.startswith(w):
                return self.get_response(request)
        if path in _LEGACY_EXACT_REDIRECTS:
            return HttpResponseRedirect(_LEGACY_EXACT_REDIRECTS[path])
        if is_auth:
            if path in _AUTHENTICATED_CHAT_REDIRECTS:
                return HttpResponseRedirect(_AUTHENTICATED_CHAT_REDIRECTS[path])
            for p in _AUTHENTICATED_CHAT_PREFIXES:
                if path == p or path.startswith(p):
                    return HttpResponseRedirect("/chat/")
        # Любой матч legacy-префикса → редирект на /chat/
        for p in _LEGACY_PREFIXES:
            if path == p or path.startswith(p + "/"):
                # Сохраняем query-string на случай ?role=... etc
                target = "/chat/"
                qs = request.META.get("QUERY_STRING") or ""
                if qs:
                    target = f"{target}?{qs}"
                return HttpResponseRedirect(target)
        return self.get_response(request)


class UserLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            cookie_name = getattr(settings, "LANGUAGE_COOKIE_NAME", "django_language") or "django_language"
            cookie_lang = (request.COOKIES.get(cookie_name) or "").strip().lower()
            allowed = {code for code, _label in settings.LANGUAGES}
            try:
                profile = getattr(user, "profile", None)
            except Exception:
                profile = None
            prof_lang = (getattr(profile, "language", None) or "").strip().lower()

            if cookie_lang in allowed:
                # Явный выбор языка в cookie `django_language` — главный.
                # LocaleMiddleware его уже активировал; свежий выбор НЕ должен
                # перебиваться устаревшим profile.language (раньше был split-brain:
                # cookie=en, profile=ru → форсился ru, и страница оставалась русской).
                translation.activate(cookie_lang)
                request.LANGUAGE_CODE = cookie_lang
                # Подлечиваем профиль, чтобы preference не расходился (нужно для
                # входа с другого устройства без cookie). Один раз на расхождение.
                if profile is not None and prof_lang != cookie_lang:
                    try:
                        profile.language = cookie_lang
                        profile.save(update_fields=["language"])
                    except Exception:
                        pass
            elif prof_lang in allowed:
                # Cookie нет (напр. свежий вход) — берём язык из профиля.
                translation.activate(prof_lang)
                request.LANGUAGE_CODE = prof_lang
        response = self.get_response(request)
        translation.deactivate()
        return response


class OperatorViewAsMiddleware:
    """View-as: оператор «входит» в кабинет поставщика для просмотра / контроля.

    Принцип:
    — В `request.session['op_view_as_id']` хранится ID поставщика-цели.
    — Оригинальный оператор хранится в `request.session['op_view_as_originator_id']`.
    — При наличии этих ключей middleware подменяет `request.user` на поставщика
      (для всех views — actions, страницы кабинета и т.д. естественно видят
      его контекст). Оригинального юзера кладём в `request.original_user`.
    — Флаг `request.is_view_as = True` + `request.view_as_readonly = True`
      позволяют views отказывать в мутациях.

    Безопасность:
    — Подмена возможна только если оригинальному пользователю явно выдана
      роль operator или admin. Если кто-то другой случайно поставил ключ
      в сессию — ignore.
    — Цель должна существовать и быть seller.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_view_as = False
        request.view_as_readonly = False
        request.original_user = None
        try:
            self._maybe_swap_user(request)
        except Exception:
            # Никогда не валим запрос из-за view-as
            request.is_view_as = False
        if request.view_as_readonly and request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Единственные POST-команды, разрешённые в режиме просмотра, не
            # меняют данные поставщика: выход и служебная помощь оператора.
            allowed_actions = {
                "op_exit_view_as", "op_help_supplier",
                "op_help_send_reminder", "op_help_escalate",
            }
            action = ""
            if request.path == "/api/assistant/action/":
                try:
                    action = (json.loads(request.body or b"{}") or {}).get("action", "")
                except (TypeError, ValueError, json.JSONDecodeError):
                    action = ""
            if action not in allowed_actions:
                return JsonResponse(
                    {"error": "Режим просмотра доступен только для чтения."},
                    status=403,
                )
            # Служебная команда должна выполняться от имени оператора, а не
            # от имени просматриваемого продавца. Данные продавца по-прежнему
            # доступны команде только через явный seller_id в параметрах.
            request.user = request.original_user
        return self.get_response(request)

    def _maybe_swap_user(self, request):
        sess = getattr(request, "session", None)
        if sess is None:
            return
        target_id = sess.get("op_view_as_id")
        if not target_id:
            return
        original = request.user
        # Только реально выданная операторская роль или superuser может
        # открывать режим просмотра. Сам по себе is_staff не даёт это право.
        from assistant.permissions import detect_user_role
        original_role = detect_user_role(original)
        if (
            not getattr(original, "is_authenticated", False)
            or not (original_role.startswith("operator") or original_role == "admin")
        ):
            # Левый юзер с ключом в сессии — чистим
            sess.pop("op_view_as_id", None)
            sess.pop("op_view_as_originator_id", None)
            return
        # Загружаем target
        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            target = User.objects.select_related("profile").get(id=int(target_id))
        except (User.DoesNotExist, ValueError, TypeError):
            sess.pop("op_view_as_id", None)
            sess.pop("op_view_as_originator_id", None)
            return
        # FIX (CRITICAL): нельзя имперсонировать staff/superuser — иначе оператор
        # с view-as правами может «войти» как админ и получить полный доступ.
        if getattr(target, "is_staff", False) or getattr(target, "is_superuser", False):
            sess.pop("op_view_as_id", None)
            sess.pop("op_view_as_originator_id", None)
            return
        # Target должен быть seller — view-as только для контроля поставщиков.
        try:
            target_role = getattr(getattr(target, "profile", None), "role", "")
            if target_role and target_role != "seller":
                sess.pop("op_view_as_id", None)
                sess.pop("op_view_as_originator_id", None)
                return
        except Exception:
            pass
        # Подмена
        request.original_user = original
        request.user = target
        request.is_view_as = True
        request.view_as_readonly = True


class ActiveRoleContextMiddleware:
    """Фиксирует подтвержденную активную роль для всех представлений запроса."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        request.active_role = None
        if user is not None and getattr(user, "is_authenticated", False):
            from assistant.permissions import detect_user_role
            request.active_role = detect_user_role(user, request=request)
            # Старые внутренние функции получают только user. Объект User
            # живет в рамках запроса, поэтому атрибут не разделяется между
            # пользователями и позволяет им читать тот же серверный контекст.
            user._assistant_active_role = request.active_role
        return self.get_response(request)
