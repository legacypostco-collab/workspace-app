"""Shared pytest fixtures + Django setup for tests/."""
import os

# Для тестов всегда используем SQLite — PostgreSQL может не быть запущен локально.
# Устанавливаем непустой DATABASE_URL до django.setup(), чтобы _load_env_file
# в settings.py не перетёр его значением из .env (он пропускает непустые переменные).
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "consolidator_site.settings")

import django  # noqa: E402
django.setup()
