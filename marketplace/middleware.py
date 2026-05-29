"""
Custom middleware: applies the authenticated user's preferred language
(stored in UserProfile.language) to the current request.

Anonymous users fall back to the session-stored or Accept-Language value
that Django's LocaleMiddleware already resolved.

Must run AFTER `django.middleware.locale.LocaleMiddleware` and AFTER
`AuthenticationMiddleware` so `request.user` is populated.
"""
from django.http import HttpResponseRedirect
from django.utils import translation

# ────────────────────────────────────────────────────────────────────
# Legacy cabinet → chat-first redirect.
#
# Старые кабинеты (/dashboard, /buyer/*, /seller/*, /operator/*,
# /admin_panel/*) — deprecated. Единственный UI = /chat/.
# Этот middleware ловит залогиненных пользователей на этих путях и
# редиректит на /chat/. URL'ы пока остаются в urls.py (для обратной
# совместимости со ссылками в email-уведомлениях и старых redirect'ах),
# но из чат-UI на них нет ни одной ссылки.
#
# Не-залогиненные на /buyer/, /seller/* — Django по auth_required даст
# redirect на /login/?next=..., после успешного логина LOGIN_REDIRECT_URL
# = /chat/, и они уйдут в чат. Из /login/?next=/dashboard/ останется
# только редирект — middleware перехватит и направит в /chat/.
#
# Whitelisted под-URL'ы: /admin/ (Django admin), /seller/onboarding/
# (часть legacy flow, который ещё используется в register'е),
# /admin_panel/api/ (если есть JSON-вьюшки для интеграций).
# ────────────────────────────────────────────────────────────────────

_LEGACY_PREFIXES = (
    "/dashboard",
    "/buyer",
    "/seller",
    "/operator",
    "/admin_panel",
)

_LEGACY_WHITELIST = (
    "/admin/",                  # Django admin (отдельный staff-UI)
    "/seller/onboarding/",      # KYB flow ещё может ссылать туда
    "/admin_panel/api/",        # JSON API для интеграций
)


class LegacyCabinetRedirectMiddleware:
    """Залогиненный заход на /dashboard, /buyer, /seller, /operator,
    /admin_panel → 302 на /chat/. Не трогает /admin/, API, whitelisted
    пути и не-залогиненных (их обработает обычный auth-required flow).
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info or ""
        # Whitelist первым делом — пускаем без редиректа
        for w in _LEGACY_WHITELIST:
            if path.startswith(w):
                return self.get_response(request)
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
            try:
                lang = getattr(getattr(user, "profile", None), "language", None)
            except Exception:
                lang = None
            if lang:
                translation.activate(lang)
                request.LANGUAGE_CODE = lang
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
    — Подмена возможна только если оригинальный юзер — operator или admin
      (is_staff / is_superuser). Если кто-то другой случайно поставил ключ
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
        return self.get_response(request)

    def _maybe_swap_user(self, request):
        sess = getattr(request, "session", None)
        if sess is None:
            return
        target_id = sess.get("op_view_as_id")
        if not target_id:
            return
        original = request.user
        # Только staff может имперсонировать (operator/admin — у всех is_staff=True)
        if not getattr(original, "is_authenticated", False) or not getattr(original, "is_staff", False):
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
