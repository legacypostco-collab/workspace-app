import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent

# Запущены ли мы под тест-раннером (manage.py test / pytest). Используется,
# напр., чтобы фоновые задачи (импорт прайса) выполнялись ИНЛАЙН внутри
# тестовой транзакции, а не в отдельном потоке (который её не видит).
TESTING = (
    "test" in sys.argv
    or "pytest" in sys.modules
    or bool(os.getenv("PYTEST_CURRENT_TEST"))
)


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        k = key.strip()
        v = value.strip().strip('"').strip("'")
        # Override empty/missing env vars from .env, but never clobber a real
        # value the user set in their shell. setdefault() is wrong here because
        # an inherited empty `KEY=''` (common in zsh dotfiles) would block .env.
        if not os.environ.get(k):
            os.environ[k] = v


_load_env_file(BASE_DIR / ".env")


def _redis_url_for_db(url: str, database: int) -> str:
    """Keep Redis credentials/host while selecting a dedicated logical DB."""
    parsed = urlsplit((url or "").strip())
    if parsed.scheme not in {"redis", "rediss"} or not parsed.netloc:
        return ""
    return parsed._replace(path=f"/{database}").geturl()


DEFAULT_CACHE_URL = (
    os.getenv("DEFAULT_CACHE_URL", "").strip()
    or _redis_url_for_db(
        os.getenv("CELERY_BROKER_URL")
        or os.getenv("CHANNELS_REDIS_URL")
        or os.getenv("PRICELIST_CACHE_URL")
        or "redis://127.0.0.1:6379/2",
        2,
    )
)

# Кэши. В обычном режиме оба кэша общие для всех web/worker-процессов.
# Это важно не только для прогресса импорта, но и для rate-limit, одноразовых
# операций и блокировок: LocMem позволял обходить их через другой процесс.
# В тестах оставляем LocMem, чтобы CI не зависел от внешнего Redis.
CACHES = {
    "default": (
        {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "default-test-cache",
        }
        if TESTING
        else {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": DEFAULT_CACHE_URL,
        }
    ),
    "pricelist": (
        {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "pricelist-locmem",
        }
        if TESTING
        else {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.getenv(
                "PRICELIST_CACHE_URL",
                "redis://127.0.0.1:6379/4",
            ),
        }
    ),
}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_list(name: str, default: str = "") -> list[str]:
    raw = (os.getenv(name, default) or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _env_bool("DEBUG_MODE", False)
SECRET_KEY = _env("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        # FIX (HIGH): больше не используем известный hardcoded fallback —
        # генерим эфемерный ключ для dev (сессии не переживут перезапуск,
        # но это безопаснее чем известный всем 'dev-secret-...').
        import secrets as _secrets
        SECRET_KEY = "dev-ephemeral-" + _secrets.token_urlsafe(48)
        import warnings as _warnings
        _warnings.warn(
            "SECRET_KEY not set — using ephemeral dev key. Sessions won't "
            "persist across restarts. Set SECRET_KEY env var for stable dev.",
            RuntimeWarning, stacklevel=2,
        )
    else:
        raise RuntimeError("SECRET_KEY is required")

# Separate material is recommended so TOTP data can be rotated independently.
# SECRET_KEY remains a compatible fallback for existing installations.
TOTP_ENCRYPTION_KEY = _env("TOTP_ENCRYPTION_KEY", SECRET_KEY)


# FIX (HIGH): .localhost.run / .lhr.life — публичные tunneling-TLD, через них
# можно перехватить CSRF/session если поднять malicious subdomain. По умолчанию
# только локалхост; tunneling нужно явно включать через env.
ALLOWED_HOSTS = _env_list(
    "ALLOWED_HOSTS",
    "127.0.0.1,localhost",
)
CSRF_TRUSTED_ORIGINS = _env_list(
    "CSRF_TRUSTED_ORIGINS",
    (
        "http://127.0.0.1,http://127.0.0.1:8001,"
        "http://localhost,http://localhost:8001"
    )
    if DEBUG
    else "",
)

INSTALLED_APPS = [
    # Daphne MUST come before django.contrib.staticfiles to provide ASGI runserver
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "django_celery_beat",
    "django_celery_results",
    "channels",
    "assistant",
    "files",
    "catalog",
    "offers",
    "imports",
    "projections",
    "dashboard",
    "marketplace",
]

# ── django-axes: brute-force protection ────────────────────────────
# Auto-locks user after N failed login attempts (per IP + per username).
# Active only when 'axes' package is installed (graceful skip in dev if not).
try:
    import axes  # noqa: F401
    INSTALLED_APPS.append("axes")
    AXES_FAILURE_LIMIT = int(_env("AXES_FAILURE_LIMIT", "5"))
    AXES_COOLOFF_TIME = float(_env("AXES_COOLOFF_TIME_HOURS", "0.5"))  # 30 min
    AXES_LOCKOUT_PARAMETERS = ["ip_address", "username"]
    AXES_RESET_ON_SUCCESS = True
    AXES_ENABLE_ADMIN = True
    AXES_VERBOSE = True
    _AXES_ENABLED = True
except ImportError:
    _AXES_ENABLED = False

MIDDLEWARE = [
    "consolidator_site.middleware.SecurityHeadersMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "marketplace.middleware.UserLanguageMiddleware",
    # View-as: оператор → кабинет поставщика для контроля (read-only)
    "marketplace.middleware.OperatorViewAsMiddleware",
    "marketplace.middleware.ActiveRoleContextMiddleware",
    # Старые кабинеты → /chat/ (chat-first — единственный UI)
    "marketplace.middleware.LegacyCabinetRedirectMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
# django-axes hooks middleware последним (после auth) когда установлен
if _AXES_ENABLED:
    MIDDLEWARE.append("axes.middleware.AxesMiddleware")
    AUTHENTICATION_BACKENDS = [
        "axes.backends.AxesStandaloneBackend",
        "django.contrib.auth.backends.ModelBackend",
    ]

ROOT_URLCONF = "consolidator_site.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "consolidator_site.wsgi.application"

DB_ENGINE = _env("DB_ENGINE", "django.db.backends.postgresql")
DB_NAME = _env("DB_NAME", "")
DB_USER = _env("DB_USER", "")
DB_PASSWORD = _env("DB_PASSWORD", "")
DB_HOST = _env("DB_HOST", "127.0.0.1")
DB_PORT = _env("DB_PORT", "5432")

# DATABASE_URL takes priority (production / Heroku-style)
# Examples: postgres://user:pass@host:5432/dbname  /  sqlite:///path/to/db.sqlite3
DATABASE_URL = _env("DATABASE_URL", "")
if DATABASE_URL:
    try:
        import dj_database_url
        DATABASES = {
            "default": dj_database_url.parse(
                DATABASE_URL,
                conn_max_age=int(_env("DB_CONN_MAX_AGE", "60")),
                conn_health_checks=True,
                ssl_require=_env_bool("DB_SSL_REQUIRE", False),
            )
        }
    except ImportError:
        # Fallback to legacy DB_* env vars if dj_database_url not installed
        DATABASE_URL = ""

if not DATABASE_URL:
    if DB_NAME:
        # Postgres/MySQL accept `connect_timeout`; SQLite uses `timeout`
        if "sqlite" in DB_ENGINE:
            _db_options = {"timeout": int(_env("DB_CONNECT_TIMEOUT", "60"))}
        else:
            _db_options = {"connect_timeout": int(_env("DB_CONNECT_TIMEOUT", "5"))}
        DATABASES = {
            "default": {
                "ENGINE": DB_ENGINE,
                "NAME": DB_NAME,
                "USER": DB_USER,
                "PASSWORD": DB_PASSWORD,
                "HOST": DB_HOST,
                "PORT": DB_PORT,
                "CONN_MAX_AGE": int(_env("DB_CONN_MAX_AGE", "60")),
                "OPTIONS": _db_options,
            }
        }
    else:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
                "OPTIONS": {
                    "timeout": 60,
                },
            }
        }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
LANGUAGES = [
    ("ru", "Русский"),
    ("en", "English"),
    ("es", "Español"),
    ("zh-hans", "中文"),
    ("ar", "العربية"),
]
# RTL languages — used by template / middleware to set <html dir="rtl">
LANGUAGES_BIDI = ["ar", "he", "fa", "ur"]
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
SERVE_MEDIA = _env_bool("SERVE_MEDIA", DEBUG)
QR_SECRET = _env("QR_SECRET", "")
HEALTHCHECK_TOKEN = _env("HEALTHCHECK_TOKEN", "")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
# Главный интерфейс — рабочее пространство чата. Старые ролевые кабинеты
# удалены и отвечают 410; переходные /dashboard и /admin_panel ведут в чат.
LOGIN_REDIRECT_URL = "/chat/"
LOGOUT_REDIRECT_URL = "/"

# ── Email backend ─────────────────────────────────────────
# Production: set EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD env vars (SMTP)
# Local dev: emails print to console. Production never writes login/reset
# links to logs if SMTP is missing.
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", True)
    EMAIL_USE_SSL = _env_bool("EMAIL_USE_SSL", False)
elif DEBUG:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL",
    "Consolidator Parts <noreply@consolidatorparts.com>",
)
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
EMAIL_VERIFICATION_REQUIRED = _env_bool("EMAIL_VERIFICATION_REQUIRED", not DEBUG)
ADMINS = [tuple(a.split(":", 1)) for a in _env_list("ADMINS", "") if ":" in a]
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24  # 24 hours
# FIX (HIGH): по умолчанию prod-режим = HTTPS. В DEBUG можно явно отключить
# env var. Это закрывает дыру: раньше USE_HTTPS=False по умолчанию означал
# session/csrf cookies без Secure-флага даже в prod.
USE_HTTPS = _env_bool("USE_HTTPS", not DEBUG)
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", USE_HTTPS)
CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", USE_HTTPS)
# Явная защита cookies (Django дефолты тоже True, но фиксируем для security-audit):
#   HttpOnly  — JS не читает session cookie (защита от XSS-кражи)
#   SameSite=Lax — cookie не идёт на cross-site POST (CSRF mitigation)
# CSRF_COOKIE_HTTPONLY=False — нужно JS-чтение для X-CSRFToken header в fetch.
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", USE_HTTPS)
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = _env_bool("SECURE_HSTS_PRELOAD", False)
SECURE_CONTENT_TYPE_NOSNIFF = _env_bool("SECURE_CONTENT_TYPE_NOSNIFF", True)
SECURE_REFERRER_POLICY = os.getenv("SECURE_REFERRER_POLICY", "same-origin")
X_FRAME_OPTIONS = os.getenv("X_FRAME_OPTIONS", "DENY")
DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    os.getenv("DATA_UPLOAD_MAX_MEMORY_SIZE", str(2 * 1024 * 1024))
)
DATA_UPLOAD_MAX_NUMBER_FIELDS = int(
    os.getenv("DATA_UPLOAD_MAX_NUMBER_FIELDS", "1000")
)
DATA_UPLOAD_MAX_NUMBER_FILES = int(
    os.getenv("DATA_UPLOAD_MAX_NUMBER_FILES", "20")
)
FILE_UPLOAD_MAX_MEMORY_SIZE = int(
    os.getenv("FILE_UPLOAD_MAX_MEMORY_SIZE", str(2 * 1024 * 1024))
)

BEHIND_PROXY = _env_bool("BEHIND_PROXY", False)
TRUSTED_PROXY_NETWORKS = _env_list(
    "TRUSTED_PROXY_NETWORKS",
    "127.0.0.1/32,::1/128",
)
if BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "300/min",
        "quote": "30/min",
        "import": "10/min",
        "lookup": "10/min",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Consolidator Parts API",
    "DESCRIPTION": "B2B marketplace API for industrial spare parts. Endpoints for catalog, RFQ, orders, payments, logistics.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1/",
    "COMPONENT_SPLIT_REQUEST": True,
}

# ── Object Storage (S3 / MinIO / Yandex Object Storage) ─────────
# Если AWS_STORAGE_BUCKET_NAME задан и django-storages установлен —
# media (KYB-доки, чертежи, прайсы, документы заказа) уходит в bucket
# вместо локальной FS. Это требование для horizontal scaling.
AWS_STORAGE_BUCKET_NAME = _env("AWS_STORAGE_BUCKET_NAME", "")
_USE_S3 = bool(AWS_STORAGE_BUCKET_NAME)
if _USE_S3:
    try:
        import storages  # noqa: F401
        AWS_ACCESS_KEY_ID = _env("AWS_ACCESS_KEY_ID")
        AWS_SECRET_ACCESS_KEY = _env("AWS_SECRET_ACCESS_KEY")
        AWS_S3_REGION_NAME = _env("AWS_S3_REGION_NAME", "ru-central1")
        AWS_S3_ENDPOINT_URL = _env("AWS_S3_ENDPOINT_URL", "")  # пусто для AWS, "https://storage.yandexcloud.net" для Yandex
        AWS_S3_CUSTOM_DOMAIN = _env("AWS_S3_CUSTOM_DOMAIN", "")
        AWS_DEFAULT_ACL = None       # bucket policy управляет ACL
        AWS_QUERYSTRING_AUTH = True  # presigned URLs для media (24h по умолчанию)
        AWS_QUERYSTRING_EXPIRE = int(_env("AWS_QUERYSTRING_EXPIRE", "86400"))
        AWS_S3_FILE_OVERWRITE = False  # никогда не перезаписываем существующий файл
        _DEFAULT_STORAGE_BACKEND = "storages.backends.s3boto3.S3Boto3Storage"
    except ImportError:
        _DEFAULT_STORAGE_BACKEND = "django.core.files.storage.FileSystemStorage"
        _USE_S3 = False
else:
    _DEFAULT_STORAGE_BACKEND = "django.core.files.storage.FileSystemStorage"

STORAGES = {
    "default": {
        "BACKEND": _DEFAULT_STORAGE_BACKEND,
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# ── Content Security Policy ────────────────────────────────────────
# Defense-in-depth XSS protection. Активируется через django-csp если
# установлен. Для inline-onclick в chat (~37 шт) пока используем
# 'unsafe-inline' — TODO мигрировать на event delegation чтобы убрать.
# ── Content Security Policy (django-csp 4.0 формат) ──────────────
# FIXME: убрать 'unsafe-inline' из script/style после миграции inline-onclick
# на event delegation (~37 мест в chat-first.js + шаблонах).
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "script-src": ["'self'", "'unsafe-inline'"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "img-src": ["'self'", "data:", "https:"],
        "font-src": ["'self'", "data:"],
        "connect-src": ["'self'", "ws:" if DEBUG else "wss:"],
        "media-src": ["'self'", "blob:"],
        "frame-ancestors": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
    }
}
try:
    import csp  # noqa: F401
    MIDDLEWARE.insert(
        MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
        "csp.middleware.CSPMiddleware",
    )
except ImportError as exc:
    # НЕ тихий пропуск: django-csp заявлен в requirements. Если его нет в
    # боевом окружении — CSP-заголовок молча исчезает (защита «на бумаге»).
    # Строгий режим (падаем явно) включён в проде; тесты/локалка только warn.
    _csp_strict = _env_bool("CSP_STRICT", not DEBUG)
    if _csp_strict:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(
            "django-csp заявлен в requirements, но не установлен в окружении — "
            "CSP-заголовок не будет отдаваться. Установите зависимости: "
            "pip install -r requirements.txt (или CSP_STRICT=0 чтобы отключить)"
        ) from exc
    import warnings
    warnings.warn("django-csp не установлен — CSP-заголовок отключён (dev/test).")

# ── KYB feature flag ───────────────────────────────────────────────
# По умолчанию в production выключаем onboarding. В dev он доступен, но
# внешние проверки всё равно fail-closed и уходят оператору на ручную проверку.
KYB_ENABLED = _env_bool("KYB_ENABLED", DEBUG)
# Детерминированные KYB-ответы разрешены только для изолированных тестов и
# явно запущенных демонстрационных посевов. DEBUG сам по себе их не включает.
KYB_ALLOW_TEST_FIXTURES = _env_bool("KYB_ALLOW_TEST_FIXTURES", False)

# В рабочем режиме каждая загрузка должна пройти ClamAV. Локальную разработку
# можно вести без демона, но отключение сканера в production требует явного
# изменения обеих переменных и должно считаться осознанным снижением защиты.
ENABLE_VIRUS_SCAN = _env_bool("ENABLE_VIRUS_SCAN", not DEBUG)
VIRUS_SCAN_REQUIRED = _env_bool("VIRUS_SCAN_REQUIRED", not DEBUG)

# ── AI cost controls ───────────────────────────────────────────────
# Дневной лимит на пользователя ($, после превышения AI handler'ы возвращают
# текстовый refuse без round-trip к Anthropic/OpenAI). Хранится в кэше.
AI_DAILY_BUDGET_USD = float(_env("AI_DAILY_BUDGET_USD", "5.00"))

# ── AI Assistant ──────────────────────────────────────────
# Telegram-нотификации для оператора (escalations + critical alerts).
# Получить токен у @BotFather. Без токена — send_telegram() тихо no-op.
# Базовый URL для ссылок в email/Telegram-уведомлениях.
# Без него фолбэк на ALLOWED_HOSTS[0] (см. assistant/channels.py:_build_email_link).
# Prod: https://consolidatorparts.com · Dev: http://localhost:8001
SITE_URL = os.getenv("SITE_URL", "").strip()

# Deposit details are deployment secrets. Missing values disable the method.
TOPUP_BANK_BENEFICIARY = os.getenv("TOPUP_BANK_BENEFICIARY", "").strip()
TOPUP_BANK_BENEFICIARY_ADDR = os.getenv("TOPUP_BANK_BENEFICIARY_ADDR", "").strip()
TOPUP_BANK_TRADE_LICENSE = os.getenv("TOPUP_BANK_TRADE_LICENSE", "").strip()
TOPUP_BANK_TAX_NO = os.getenv("TOPUP_BANK_TAX_NO", "").strip()
TOPUP_BANK_NAME = os.getenv("TOPUP_BANK_NAME", "").strip()
TOPUP_BANK_BRANCH_CODE = os.getenv("TOPUP_BANK_BRANCH_CODE", "").strip()
TOPUP_BANK_SWIFT = os.getenv("TOPUP_BANK_SWIFT", "").strip()
TOPUP_BANK_IBAN = os.getenv("TOPUP_BANK_IBAN", "").strip()
TOPUP_BANK_ACCOUNT = os.getenv("TOPUP_BANK_ACCOUNT", "").strip()
TOPUP_BANK_CURRENCY = os.getenv("TOPUP_BANK_CURRENCY", "").strip()
TOPUP_BANK_CONTACT_NAME = os.getenv("TOPUP_BANK_CONTACT_NAME", "").strip()
TOPUP_BANK_CONTACT_PHONE = os.getenv("TOPUP_BANK_CONTACT_PHONE", "").strip()
TOPUP_BANK_CONTACT_EMAIL = os.getenv("TOPUP_BANK_CONTACT_EMAIL", "").strip()
TOPUP_USDT_ADDRESS = os.getenv("TOPUP_USDT_ADDRESS", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
# Secret Telegram sends in X-Telegram-Bot-Api-Secret-Token.
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

# Per-category SLA эскалаций рекламаций (в днях).
# Можно переопределить через env CLAIM_SLA_DEFECT, CLAIM_SLA_LATE и т.д.
# По умолчанию см. DEFAULT_SLA_DAYS в escalate_stale_claims.py
CLAIM_SLA_DAYS = {
    k: int(os.getenv(f"CLAIM_SLA_{k.upper()}", default))
    for k, default in [
        ("missing", "2"), ("defect", "3"), ("damage", "3"),
        ("late", "5"), ("wrong_part", "7"), ("other", "7"),
    ]
}

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()  # sonnet-4-20250514 выводится 2026-06-15
# Цена 1 AI-запроса при покупке с депозита (USD, ≈ себестоимость). Пакеты 50/100.
AI_REQUEST_PRICE_USD = float(os.getenv("AI_REQUEST_PRICE_USD", "0.04"))
# Haiku 4.5 для простых запросов (buyer-chat). 12× дешевле Sonnet.
ANTHROPIC_FAST_MODEL = os.getenv("ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001").strip()
# Если 1 — все роли используют FAST_MODEL (dev / R&D). По умолчанию 0:
# Haiku только для buyer-чата, Sonnet для seller/operator.
ANTHROPIC_FAST_MODE = _env_bool("ANTHROPIC_FAST_MODE", False)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "").strip()
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower()  # openai|voyage|stub|auto

# ── Sentry error tracking ─────────────────────────────────
# Set SENTRY_DSN env var to enable. Auto-captures unhandled exceptions, performance.
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration()],
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.0")),
            send_default_pii=False,
            environment=os.getenv("SENTRY_ENV", "production"),
            release=os.getenv("SENTRY_RELEASE", ""),
        )
    except ImportError:
        pass  # sentry-sdk not installed — skip silently

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}

TEUSTAT_API_URL = os.getenv("TEUSTAT_API_URL", "").strip()
TEUSTAT_API_KEY = os.getenv("TEUSTAT_API_KEY", "").strip()
TEUSTAT_TIMEOUT_SEC = float(os.getenv("TEUSTAT_TIMEOUT_SEC", "8"))
TEUSTAT_CONTRACT_VERSION = os.getenv("TEUSTAT_CONTRACT_VERSION", "teustat_v1").strip() or "teustat_v1"
TEUSTAT_STRICT_MODE = _env_bool("TEUSTAT_STRICT_MODE", not DEBUG)
LOGISTICS_PROVIDER = os.getenv("LOGISTICS_PROVIDER", "teustat").strip().lower() or "teustat"
LOGISTICS_STRICT_MODE = _env_bool("LOGISTICS_STRICT_MODE", not DEBUG)

SEARATES_API_URL = os.getenv("SEARATES_API_URL", "").strip()
SEARATES_API_KEY = os.getenv("SEARATES_API_KEY", "").strip()
SEARATES_TIMEOUT_SEC = float(os.getenv("SEARATES_TIMEOUT_SEC", "8"))

FREIGHTOS_API_URL = os.getenv("FREIGHTOS_API_URL", "").strip()
FREIGHTOS_API_KEY = os.getenv("FREIGHTOS_API_KEY", "").strip()
FREIGHTOS_TIMEOUT_SEC = float(os.getenv("FREIGHTOS_TIMEOUT_SEC", "8"))

XENETA_API_URL = os.getenv("XENETA_API_URL", "").strip()
XENETA_API_KEY = os.getenv("XENETA_API_KEY", "").strip()
XENETA_TIMEOUT_SEC = float(os.getenv("XENETA_TIMEOUT_SEC", "8"))

LOGISTICS_DEFAULT_ORIGIN = os.getenv("LOGISTICS_DEFAULT_ORIGIN", "Shanghai, CN").strip()
LOGISTICS_DEFAULT_DESTINATION = os.getenv("LOGISTICS_DEFAULT_DESTINATION", "Moscow, RU").strip()
LOGISTICS_DEFAULT_MODE = os.getenv("LOGISTICS_DEFAULT_MODE", "sea").strip().lower() or "sea"
LOGISTICS_DEFAULT_INCOTERM = os.getenv("LOGISTICS_DEFAULT_INCOTERM", "FOB").strip().upper() or "FOB"

PAYMENT_PROVIDER_URL = os.getenv("PAYMENT_PROVIDER_URL", "").strip()
PAYMENT_MERCHANT_ID = os.getenv("PAYMENT_MERCHANT_ID", "").strip()
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "USD").strip().upper() or "USD"
WEBHOOK_ENDPOINTS = os.getenv("WEBHOOK_ENDPOINTS", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_TIMEOUT_SEC = float(os.getenv("WEBHOOK_TIMEOUT_SEC", "2"))
WEBHOOK_RETRY_MAX_ATTEMPTS = int(os.getenv("WEBHOOK_RETRY_MAX_ATTEMPTS", "5"))
WEBHOOK_ALLOWED_HOSTS = os.getenv("WEBHOOK_ALLOWED_HOSTS", "").strip()
WEBHOOK_ALLOW_PRIVATE_IPS = _env_bool("WEBHOOK_ALLOW_PRIVATE_IPS", False)
WEBHOOK_ALLOW_INSECURE_HTTP = _env_bool("WEBHOOK_ALLOW_INSECURE_HTTP", False)
LOGISTICS_ALLOWED_HOSTS = os.getenv("LOGISTICS_ALLOWED_HOSTS", "").strip()
LOGISTICS_ALLOW_PRIVATE_IPS = _env_bool("LOGISTICS_ALLOW_PRIVATE_IPS", False)
LOGISTICS_ALLOW_INSECURE_HTTP = _env_bool("LOGISTICS_ALLOW_INSECURE_HTTP", False)
FX_ALLOWED_HOSTS = os.getenv("FX_ALLOWED_HOSTS", "open.er-api.com").strip()
FX_ALLOW_PRIVATE_IPS = _env_bool("FX_ALLOW_PRIVATE_IPS", False)
FX_ALLOW_INSECURE_HTTP = _env_bool("FX_ALLOW_INSECURE_HTTP", False)
GOOGLE_SHEETS_ALLOWED_HOSTS = os.getenv(
    "GOOGLE_SHEETS_ALLOWED_HOSTS",
    "docs.google.com,*.googleusercontent.com",
).strip()
GOOGLE_SHEETS_ALLOW_PRIVATE_IPS = _env_bool("GOOGLE_SHEETS_ALLOW_PRIVATE_IPS", False)
GOOGLE_SHEETS_ALLOW_INSECURE_HTTP = _env_bool("GOOGLE_SHEETS_ALLOW_INSECURE_HTTP", False)
PAYMENT_CALLBACK_SECRET = os.getenv("PAYMENT_CALLBACK_SECRET", "").strip()
PAYMENT_CALLBACK_MAX_BODY_BYTES = int(
    os.getenv("PAYMENT_CALLBACK_MAX_BODY_BYTES", str(64 * 1024))
)

MAX_IMPORT_FILE_BYTES = int(os.getenv("MAX_IMPORT_FILE_BYTES", str(2 * 1024 * 1024)))
MAX_IMPORT_ROWS = int(os.getenv("MAX_IMPORT_ROWS", "5000"))
MAX_QUOTE_ITEMS = int(os.getenv("MAX_QUOTE_ITEMS", "50"))
MAX_ORDER_DOCUMENT_BYTES = int(os.getenv("MAX_ORDER_DOCUMENT_BYTES", str(10 * 1024 * 1024)))
LEGAL_LOOKUP_TIMEOUT_SEC = float(os.getenv("LEGAL_LOOKUP_TIMEOUT_SEC", "2"))
LEGAL_LOOKUP_CIRCUIT_SECONDS = int(os.getenv("LEGAL_LOOKUP_CIRCUIT_SECONDS", "30"))

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0").strip()
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL).strip()
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = _env_bool("CELERY_TASK_EAGER_PROPAGATES", True)
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
# Единственный кодовый источник расписания. DatabaseScheduler дополнительно
# подхватывает управляемые записи django-celery-beat, не затирая этот набор.
from celery.schedules import crontab
CELERY_BEAT_SCHEDULE = {
    "check-sla-breaches-every-15min": {
        "task": "marketplace.tasks.check_sla_breaches",
        "schedule": crontab(minute="*/15"),
    },
    "send-pending-notifications-every-5min": {
        "task": "marketplace.tasks.send_pending_email_notifications",
        "schedule": crontab(minute="*/5"),
    },
    "cleanup-expired-tokens-daily": {
        "task": "marketplace.tasks.cleanup_expired_tokens",
        "schedule": crontab(hour=3, minute=0),
    },
    "reindex-assistant-nightly": {
        "task": "assistant.tasks.reindex_all_task",
        "schedule": crontab(hour=2, minute=30),
    },
    "kyb_weekly_monitor": {
        "task": "assistant.tasks.kyb_weekly_monitor",
        "schedule": crontab(hour=3, minute=0, day_of_week="monday"),
    },
    "prune_old_audit_monthly": {
        "task": "assistant.tasks.prune_old_audit",
        "schedule": crontab(hour=4, minute=0, day_of_month="1"),
        "kwargs": {"days": int(_env("AUDIT_RETENTION_DAYS", "1095"))},
    },
}
CELERY_TASK_TIME_LIMIT = 5 * 60   # hard kill after 5 min
CELERY_TASK_SOFT_TIME_LIMIT = 4 * 60
CELERY_WORKER_PREFETCH_MULTIPLIER = 4
CELERY_WORKER_MAX_TASKS_PER_CHILD = 1000  # restart worker to free memory

# ── Django Channels (WebSocket) ───────────────────────────
ASGI_APPLICATION = "consolidator_site.asgi.application"
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [os.getenv("CHANNELS_REDIS_URL", "redis://127.0.0.1:6379/1")],
            "capacity": 1500,
            "expiry": 60,
        },
    },
} if not _env_bool("CHANNELS_INMEMORY", False) else {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}
