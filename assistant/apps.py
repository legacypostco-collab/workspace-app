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
