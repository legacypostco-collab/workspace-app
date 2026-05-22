"""Security headers middleware (B-18).

Добавляет Content-Security-Policy и Permissions-Policy ко всем ответам.
Заголовки можно расширить через settings.CSP_EXTRA_* при необходимости.
"""
from __future__ import annotations


class SecurityHeadersMiddleware:
    """CSP + Permissions-Policy.

    `'unsafe-inline'` для script/style оставлены временно — много inline-стилей
    и скриптов в шаблонах. Когда будет nonce-инфраструктура, убрать.
    """

    CSP = (
        "default-src 'self'; "
        # SECURITY P1: убрали 'unsafe-eval' — код его не использует.
        # 'unsafe-inline' оставлен временно: много inline-стилей и скриптов
        # в шаблонах. План — перейти на nonce-CSP в следующем спринте.
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "media-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    PERMISSIONS = "camera=(self), microphone=(self), geolocation=(self)"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.CSP)
        response.setdefault("Permissions-Policy", self.PERMISSIONS)
        return response
