from django.conf import settings
from django.contrib.auth import get_user_model, login


class DevAutoLoginMiddleware:
    """Auto-login as demo_seller in DEBUG mode when no user is authenticated."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.DEBUG and not request.user.is_authenticated:
            User = get_user_model()
            try:
                user = User.objects.get(username="demo_seller")
                login(request, user)
            except User.DoesNotExist:
                pass
        return self.get_response(request)
