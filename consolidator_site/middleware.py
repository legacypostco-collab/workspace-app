"""Security headers middleware (B-18).

Добавляет Permissions-Policy ко всем ответам.
CSP полностью делегирован django-csp (csp.middleware.CSPMiddleware),
настраивается через CONTENT_SECURITY_POLICY в settings.py.
"""
from __future__ import annotations


class SecurityHeadersMiddleware:
    """Permissions-Policy only — CSP обрабатывает django-csp."""

    PERMISSIONS = "camera=(self), microphone=(self), geolocation=(self)"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Permissions-Policy", self.PERMISSIONS)
        return response
