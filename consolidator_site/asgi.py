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

from marketplace.routing import websocket_urlpatterns as marketplace_ws  # noqa: E402
from assistant.routing import websocket_urlpatterns as assistant_ws  # noqa: E402

websocket_urlpatterns = marketplace_ws + assistant_ws

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    ),
})
