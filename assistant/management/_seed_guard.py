"""Shared guard: запрещает запуск seed_*-команд в production.

Использование в каждой seed-команде:
    from assistant.management._seed_guard import ensure_dev_only
    class Command(BaseCommand):
        def handle(self, *args, **opts):
            ensure_dev_only(self)
            ...

"""
import os

from django.conf import settings
from django.core.management.base import CommandError


def add_seed_password_argument(parser) -> None:
    parser.add_argument(
        "--password",
        help="Пароль тестовых учетных записей. Можно передать через DEMO_PASSWORD.",
    )


def require_seed_password(options) -> str:
    password = (options.get("password") or os.environ.get("DEMO_PASSWORD") or "").strip()
    if len(password) < 10:
        raise CommandError(
            "Для тестовых учетных записей передайте --password или DEMO_PASSWORD "
            "длиной не менее 10 символов."
        )
    return password


def ensure_dev_only(cmd) -> None:
    """Raise CommandError whenever a seed command runs with DEBUG=False."""
    if settings.DEBUG:
        return
    raise CommandError(
        f"{cmd.__class__.__module__} disabled in production (DEBUG=False)."
    )
