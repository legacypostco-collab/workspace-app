from django.apps import AppConfig


class AssistantConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "assistant"
    verbose_name = "AI Assistant"

    def ready(self):
        # Wire up signals
        try:
            from . import signals  # noqa
        except ImportError:
            pass
        # Регистрируем seller-actions (импорт триггерит @register декораторы)
        try:
            from . import seller_actions  # noqa
        except ImportError:
            pass
        # Регистрируем operator-actions
        try:
            from . import operator_actions  # noqa
        except ImportError:
            pass
        # Регистрируем onboarding actions (KYB wizard)
        try:
            from . import onboarding  # noqa
        except ImportError:
            pass
        # Регистрируем negotiation actions (Quote multi-round)
        try:
            from . import negotiation  # noqa
        except ImportError:
            pass
