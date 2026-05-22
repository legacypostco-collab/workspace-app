"""Shared guard: запрещает запуск seed_*-команд в production.

Использование в каждой seed-команде:
    from assistant.management._seed_guard import ensure_dev_only
    class Command(BaseCommand):
        def handle(self, *args, **opts):
            ensure_dev_only(self)
            ...

Прод-разрешение: env-флаг ALLOW_SEED_IN_PROD=1 (для разовых миграций).
"""
import os

from django.conf import settings
from django.core.management.base import CommandError


def ensure_dev_only(cmd) -> None:
    """Throws CommandError if DEBUG=False and ALLOW_SEED_IN_PROD не выставлен."""
    if settings.DEBUG:
        return
    if os.environ.get("ALLOW_SEED_IN_PROD", "").strip() in ("1", "true", "yes"):
        cmd.stdout.write(cmd.style.WARNING(
            "⚠  ALLOW_SEED_IN_PROD set — seed-команда выполняется в production. "
            "Будь уверен что это намеренно."
        ))
        return
    raise CommandError(
        f"{cmd.__class__.__module__} disabled in production "
        "(DEBUG=False). Set ALLOW_SEED_IN_PROD=1 to override for one-off use."
    )
