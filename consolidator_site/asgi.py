"""ASGI config — supports HTTP + WebSocket via Django Channels.

Production:
  daphne -b 0.0.0.0 -p 8001 consolidator_site.asgi:application
Or with uvicorn:
  uvicorn consolidator_site.asgi:application --host 0.0.0.0 --port 8001
"""
import os

import django
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "consolidator_site.settings")
django.setup()

# Setup must run before importing app code that touches models
from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from django.conf import settings  # noqa: E402

from assistant.routing import websocket_urlpatterns as assistant_ws  # noqa: E402

websocket_urlpatterns = assistant_ws


class ExpandedOriginValidator:
    """AllowedHostsOriginValidator + CSRF_TRUSTED_ORIGINS.

    AllowedHostsOriginValidator только проверяет ALLOWED_HOSTS. Но в проде
    CSRF_TRUSTED_ORIGINS обычно содержит https://domain.com — его тоже
    надо разрешить для WS-соединений (браузер шлёт Origin: https://domain.com).
    """

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            origin = None
            for name, value in scope.get("headers", []):
                if name == b"origin":
                    origin = value.decode("latin1")
                    break

            if origin:
                from urllib.parse import urlparse
                parsed = urlparse(origin)
                host = parsed.hostname or ""

                allowed = list(settings.ALLOWED_HOSTS)
                for trusted in getattr(settings, "CSRF_TRUSTED_ORIGINS", []):
                    tp = urlparse(trusted)
                    if tp.hostname:
                        allowed.append(tp.hostname)

                def _host_ok(h, allowed_list):
                    for a in allowed_list:
                        if a == "*":
                            return True
                        if a.startswith(".") and (h == a[1:] or h.endswith(a)):
                            return True
                        if h == a:
                            return True
                    return False

                if not _host_ok(host, allowed):
                    await send({"type": "websocket.close", "code": 403})
                    return

        await self.application(scope, receive, send)


application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": ExpandedOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    ),
})
