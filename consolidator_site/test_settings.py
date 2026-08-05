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

# Tests must not depend on a locally running Redis or worker process.
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# File-content validation is covered separately; ordinary upload tests must not
# require a ClamAV daemon on the developer machine or in CI.
ENABLE_VIRUS_SCAN = False
VIRUS_SCAN_REQUIRED = False
KYB_ENABLED = True

# The historical suite still verifies the archived wallet implementation.
# New settlement tests opt into invoice_contract explicitly.
SETTLEMENT_MODE = "legacy_wallet"
SETTLEMENT_REQUIRED = False
LEGACY_WALLET_UI_ENABLED = True
