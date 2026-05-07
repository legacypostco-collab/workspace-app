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
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
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
            "Если этот email зарегистрирован, мы отправили на него ссылку."})

    def _send_email(self, user, token: str, request) -> None:
        try:
            from django.core.mail import EmailMultiAlternatives
            from django.conf import settings
            site = (
                os.getenv("SITE_URL")
                or getattr(settings, "SITE_URL", "")
                or f"http://{request.get_host()}"
            )
            link = f"{site.rstrip('/')}/api/assistant/auth/magic-link/{token}/"
            subject = "[Consolidator] Ваша ссылка для входа"
            text = (
                f"Перейдите по ссылке для входа в Consolidator:\n\n{link}\n\n"
                f"Ссылка действует 15 минут.\n"
                f"Если вы не запрашивали — просто проигнорируйте письмо."
            )
            html = (
                f"<p>Перейдите по ссылке для входа в Consolidator:</p>"
                f"<p><a href='{link}'>Войти</a></p>"
                f"<p>Ссылка действует 15 минут. Если вы не запрашивали — проигнорируйте.</p>"
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
        # При обычном UserModel Django нужно установить backend
        user.backend = "django.contrib.auth.backends.ModelBackend"
        login(request, user)
        ml.used_at = timezone.now()
        ml.ip_used = request.META.get("REMOTE_ADDR", "")[:64]
        ml.save(update_fields=["used_at", "ip_used"])
        next_url = request.GET.get("next") or "/chat/"
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
                "error": f"OAuth для {provider} не настроен (нужен {cfg['client_id_env']} в env)",
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
                f"OAuth callback для {provider} получен (code={code[:8]}…), "
                f"но exchange не реализован. Нужны реальные {cfg['client_id_env']} "
                f"и {cfg['client_secret_env']} в env."
            ),
        }, status=501)
