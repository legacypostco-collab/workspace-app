"""HTTP views for passwordless / OAuth authentication.

URL routes (added в assistant/urls.py):
  POST /api/assistant/auth/magic-link/         — запрос ссылки
  GET  /api/assistant/auth/magic-link/<token>/ — клик на ссылку → login
  GET  /api/assistant/auth/oauth/<provider>/   — OAuth scaffolding
  GET  /api/assistant/auth/oauth/callback/<provider>/

Магиc-link flow:
  1. POST /magic-link/ {email}
     → если юзер с этим email есть, создаём MagicLinkToken (TTL 15 мин)
     → шлём email со ссылкой `/magic-link/<token>/`
     → возвращаем 200 (всегда, чтобы не утекала инфа существует ли email)
  2. GET /magic-link/<token>/
     → если token active → login + redirect на `/chat/`
     → иначе 410 Gone

OAuth scaffolding:
  GET /oauth/google/ → redirect на accounts.google.com/o/oauth2/v2/auth
  GET /oauth/callback/google/?code=... → exchange + login

Реализуется когда есть GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET в env.
Сейчас — stub: возвращает «not configured».
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model, login
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View

logger = logging.getLogger(__name__)


class MagicLinkRequestView(View):
    """POST /api/assistant/auth/magic-link/ {email} → отправить ссылку."""

    def post(self, request):
        import json
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            body = {}
        email = (body.get("email") or "").strip().lower()
        if not email:
            return JsonResponse({"ok": False, "error": "email required"}, status=400)

        from django.core.cache import cache

        ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")
        email_key = f"magic-link:email:{email}"
        ip_key = f"magic-link:ip:{ip}"
        if int(cache.get(email_key, 0) or 0) >= 3 or int(cache.get(ip_key, 0) or 0) >= 10:
            return JsonResponse({"ok": True, "message":
                _("Если этот email зарегистрирован, мы отправили на него ссылку.")})
        cache.set(email_key, int(cache.get(email_key, 0) or 0) + 1, 3600)
        cache.set(ip_key, int(cache.get(ip_key, 0) or 0) + 1, 3600)

        # Никогда не палим существование email — всегда 200
        from marketplace.models import MagicLinkToken
        U = get_user_model()
        user = U.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            token = secrets.token_urlsafe(32)
            ml = MagicLinkToken.objects.create(
                token=token, user=user,
                expires_at=timezone.now() + timedelta(minutes=15),
                ip_requested=request.META.get("REMOTE_ADDR", "")[:64],
            )
            self._send_email(user, token, request)
            logger.info("magic-link sent for user_id=%s", user.id)

        return JsonResponse({"ok": True, "message":
            _("Если этот email зарегистрирован, мы отправили на него ссылку.")})

    def _send_email(self, user, token: str, request) -> None:
        try:
            from django.conf import settings
            from django.core.mail import EmailMultiAlternatives
            site = (
                os.getenv("SITE_URL")
                or getattr(settings, "SITE_URL", "")
                or f"http://{request.get_host()}"
            )
            link = f"{site.rstrip('/')}/api/assistant/auth/magic-link/{token}/"
            subject = _("[Consolidator] Ваша ссылка для входа")
            text = (
                _("Перейдите по ссылке для входа в Consolidator:\n\n%(link)s\n\n"
                  "Ссылка действует 15 минут.\n"
                  "Если вы не запрашивали — просто проигнорируйте письмо.") % {"link": link}
            )
            html = (
                _("<p>Перейдите по ссылке для входа в Consolidator:</p>"
                  "<p><a href='%(link)s'>Войти</a></p>"
                  "<p>Ссылка действует 15 минут. Если вы не запрашивали — проигнорируйте.</p>") % {"link": link}
            )
            msg = EmailMultiAlternatives(
                subject=subject, body=text,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@consolidator.local"),
                to=[user.email],
            )
            msg.attach_alternative(html, "text/html")
            msg.send(fail_silently=False)
        except Exception:
            logger.exception("magic-link email failed")


class MagicLinkConfirmView(View):
    """GET /api/assistant/auth/magic-link/<token>/ → login + redirect."""

    def get(self, request, token):
        from marketplace.models import MagicLinkToken
        ml = MagicLinkToken.objects.filter(token=token).first()
        if not ml:
            return JsonResponse({"ok": False, "error": "invalid token"}, status=410)
        if not ml.is_active:
            return JsonResponse({"ok": False, "error": "token expired or used"}, status=410)
        # Login
        user = ml.user
        if not user.is_active:
            return JsonResponse({"ok": False, "error": "account inactive"}, status=403)
        from .security import user_has_enabled_2fa
        if user_has_enabled_2fa(user):
            return JsonResponse({
                "ok": False,
                "error": "2fa_required",
                "message": _("Для этого аккаунта включена 2FA. Войдите с паролем и одноразовым кодом."),
            }, status=403)
        # При обычном UserModel Django нужно установить backend
        user.backend = "django.contrib.auth.backends.ModelBackend"
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        ml.used_at = timezone.now()
        ml.ip_used = request.META.get("REMOTE_ADDR", "")[:64]
        ml.save(update_fields=["used_at", "ip_used"])
        # SECURITY P1: проверяем, что next-URL локальный (защита от open redirect /
        # фишинга через подмененный next).
        from django.utils.http import url_has_allowed_host_and_scheme
        next_url = request.GET.get("next") or "/chat/"
        if not url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = "/chat/"
        return redirect(next_url)


# ──────────────────────────────────────────────────────────
# OAuth scaffolding — Google / Yandex
# ──────────────────────────────────────────────────────────

OAUTH_PROVIDERS = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "scope": "openid email profile",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "yandex": {
        "auth_url": "https://oauth.yandex.ru/authorize",
        "scope": "login:email login:info",
        "client_id_env": "YANDEX_CLIENT_ID",
        "client_secret_env": "YANDEX_CLIENT_SECRET",
    },
}


class OAuthLoginView(View):
    """GET /api/assistant/auth/oauth/<provider>/ → redirect на провайдера."""

    def get(self, request, provider):
        cfg = OAUTH_PROVIDERS.get(provider)
        if not cfg:
            return JsonResponse({"ok": False, "error": f"unknown provider {provider}"}, status=400)
        client_id = os.getenv(cfg["client_id_env"], "")
        if not client_id:
            return JsonResponse({"ok": False,
                "error": _("OAuth для %(provider)s не настроен (нужен %(env)s в env)")
                         % {"provider": provider, "env": cfg["client_id_env"]},
            }, status=503)
        # Сохраним state в сессии для CSRF-защиты
        state = secrets.token_urlsafe(24)
        request.session[f"oauth_state_{provider}"] = state
        # Build redirect URL
        from urllib.parse import urlencode
        site = os.getenv("SITE_URL", f"http://{request.get_host()}").rstrip("/")
        redirect_uri = f"{site}/api/assistant/auth/oauth/callback/{provider}/"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": cfg["scope"],
            "state": state,
        }
        return redirect(f"{cfg['auth_url']}?{urlencode(params)}")


class OAuthCallbackView(View):
    """GET /api/assistant/auth/oauth/callback/<provider>/?code=…&state=…"""

    def get(self, request, provider):
        cfg = OAUTH_PROVIDERS.get(provider)
        if not cfg:
            return JsonResponse({"ok": False, "error": f"unknown provider {provider}"}, status=400)
        # state CSRF check
        sent = request.GET.get("state", "")
        expected = request.session.pop(f"oauth_state_{provider}", "")
        if not sent or sent != expected:
            return JsonResponse({"ok": False, "error": "state mismatch"}, status=400)
        code = request.GET.get("code", "")
        if not code:
            return JsonResponse({"ok": False, "error": "no code"}, status=400)
        # Здесь должен быть exchange кода на токен + получение профиля.
        # Реализуется когда клиент-секрет конкретного провайдера известен.
        return JsonResponse({"ok": False,
            "error": (
                _("OAuth callback для %(provider)s получен (code=%(code)s…), "
                  "но exchange не реализован. Нужны реальные %(id_env)s "
                  "и %(secret_env)s в env.")
                % {"provider": provider, "code": code[:8],
                   "id_env": cfg["client_id_env"], "secret_env": cfg["client_secret_env"]}
            ),
        }, status=501)
