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
import hashlib
from datetime import timedelta

from django.contrib.auth import get_user_model, login
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _
from django.views import View

logger = logging.getLogger(__name__)


def _hash_magic_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _consume_rate_limit(cache, key: str, limit: int, window: int) -> bool:
    if cache.add(key, 1, window):
        return True
    try:
        return cache.incr(key) <= limit
    except ValueError:
        cache.set(key, 1, window)
        return True


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
        from .security import client_ip

        ip = client_ip(request)
        email_digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        email_key = f"magic-link:email:{email_digest}"
        ip_key = f"magic-link:ip:{ip}"
        email_allowed = _consume_rate_limit(cache, email_key, 3, 3600)
        ip_allowed = _consume_rate_limit(cache, ip_key, 10, 3600)
        if not email_allowed or not ip_allowed:
            return JsonResponse({"ok": True, "message":
                _("Если этот email зарегистрирован, мы отправили на него ссылку.")})

        # Никогда не палим существование email — всегда 200
        from marketplace.models import MagicLinkToken
        U = get_user_model()
        user = U.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            token = secrets.token_urlsafe(32)
            MagicLinkToken.objects.create(
                token=_hash_magic_token(token), user=user,
                expires_at=timezone.now() + timedelta(minutes=15),
                ip_requested=ip[:64],
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
                or request.build_absolute_uri("/")
            )
            link = f"{site.rstrip('/')}/api/assistant/auth/magic-link/{token}/"
            subject = _("[Consolidator] Ваша ссылка для входа")
            text = (
                _("Перейдите по ссылке для входа в Consolidator:\n\n%(link)s\n\n"
                  "Ссылка действует 15 минут.\n"
                  "Если вы не запрашивали — просто проигнорируйте письмо.") % {"link": link}
            )
            html = str(
                format_html(
                    "<p>{}</p><p><a href=\"{}\">{}</a></p><p>{}</p>",
                    _("Перейдите по ссылке для входа в Consolidator:"),
                    link,
                    _("Войти"),
                    _("Ссылка действует 15 минут. Если вы не запрашивали — проигнорируйте."),
                )
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
        from .security import client_ip, user_has_enabled_2fa

        with transaction.atomic():
            ml = (
                MagicLinkToken.objects.select_for_update()
                .select_related("user")
                .filter(token=_hash_magic_token(token))
                .first()
            )
            if not ml:
                return JsonResponse({"ok": False, "error": "invalid token"}, status=410)
            if not ml.is_active:
                return JsonResponse({"ok": False, "error": "token expired or used"}, status=410)
            user = ml.user
            if not user.is_active:
                return JsonResponse({"ok": False, "error": "account inactive"}, status=403)
            if user_has_enabled_2fa(user):
                return JsonResponse({
                    "ok": False,
                    "error": "2fa_required",
                    "message": _("Для этого аккаунта включена 2FA. Войдите с паролем и одноразовым кодом."),
                }, status=403)
            ml.used_at = timezone.now()
            ml.ip_used = client_ip(request)[:64]
            ml.save(update_fields=["used_at", "ip_used"])

        # При обычном UserModel Django нужно установить backend
        user.backend = "django.contrib.auth.backends.ModelBackend"
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
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
