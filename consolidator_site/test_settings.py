"""Настройки для тестов — используют SQLite :memory: вместо PostgreSQL."""
import os

# Форсируем SQLite до импорта settings, чтобы _load_env_file не перетёр.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DB_NAME"] = ""
# Тесты не прод: не падаем, если django-csp не установлен в окружении.
os.environ["CSP_STRICT"] = "0"

from consolidator_site.settings import *  # noqa: F401, F403, E402

STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

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
