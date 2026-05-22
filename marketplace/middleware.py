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
