"""Настройки для тестов — используют SQLite :memory: вместо PostgreSQL."""
import os

# Форсируем SQLite до импорта settings, чтобы _load_env_file не перетёр.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DB_NAME"] = ""

from consolidator_site.settings import *  # noqa: F401, F403, E402

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Отключаем HTTPS-redirect и HSTS для тестов
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Ускорение хэшей в тестах
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]
