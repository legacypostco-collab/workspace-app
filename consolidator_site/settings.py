import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


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
        SECRET_KEY = "dev-secret-change-in-production"
    else:
        raise RuntimeError("SECRET_KEY is required")


ALLOWED_HOSTS = _env_list(
    "ALLOWED_HOSTS",
    "127.0.0.1,localhost,.localhost.run,.lhr.life",
)
CSRF_TRUSTED_ORIGINS = _env_list(
    "CSRF_TRUSTED_ORIGINS",
    "http://127.0.0.1,http://127.0.0.1:8001,http://localhost,http://localhost:8001,https://*.localhost.run,https://*.lhr.life",
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
                "marketplace.context_processors.auth_meta",
                "marketplace.context_processors.seller_context",
                "marketplace.context_processors.buyer_context",
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
    ("zh-hans", "中文"),
    ("es", "Español"),
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
# Главный UI — chat-first. Старые кабинеты (/dashboard, /buyer, /seller,
# /operator, /admin_panel) считаются deprecated и редиректят на /chat/
# через marketplace.middleware.LegacyCabinetRedirectMiddleware.
LOGIN_REDIRECT_URL = "/chat/"
LOGOUT_REDIRECT_URL = "/"

# ── Email backend ─────────────────────────────────────────
# Production: set EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD env vars (SMTP)
# Local dev: emails print to console
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", True)
    EMAIL_USE_SSL = _env_bool("EMAIL_USE_SSL", False)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Consolidator Parts <noreply@consolidator.parts>")
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
ADMINS = [tuple(a.split(":", 1)) for a in _env_list("ADMINS", "") if ":" in a]
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24  # 24 hours
USE_HTTPS = _env_bool("USE_HTTPS", False)
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

BEHIND_PROXY = _env_bool("BEHIND_PROXY", False)
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
CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = ("'self'", "'unsafe-inline'")  # FIXME: remove unsafe-inline после миграции onclick
CSP_STYLE_SRC = ("'self'", "'unsafe-inline'")   # inline <style> в шаблонах
CSP_IMG_SRC = ("'self'", "data:", "https:")     # base64 images в landing
CSP_FONT_SRC = ("'self'", "data:", "https:")    # google fonts через css
CSP_CONNECT_SRC = ("'self'", "wss:", "https:")  # WebSocket + AI APIs
CSP_FRAME_ANCESTORS = ("'none'",)               # эквивалент X-Frame-Options: DENY
try:
    import csp  # noqa: F401
    MIDDLEWARE.insert(
        MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
        "csp.middleware.CSPMiddleware",
    )
except ImportError:
    pass

# ── KYB feature flag ───────────────────────────────────────────────
# По умолчанию в production выключаем onboarding (мок-API могут автоодобрить
# опасные компании). В dev — включено. Переключается env KYB_ENABLED=1.
KYB_ENABLED = _env_bool("KYB_ENABLED", DEBUG)

# ── AI cost controls ───────────────────────────────────────────────
# Дневной лимит на пользователя ($, после превышения AI handler'ы возвращают
# текстовый refuse без round-trip к Anthropic/OpenAI). Хранится в кэше.
AI_DAILY_BUDGET_USD = float(_env("AI_DAILY_BUDGET_USD", "5.00"))

# ── AI Assistant ──────────────────────────────────────────
# Telegram-нотификации для оператора (escalations + critical alerts).
# Получить токен у @BotFather. Без токена — send_telegram() тихо no-op.
# Базовый URL для ссылок в email/Telegram-уведомлениях.
# Без него фолбэк на ALLOWED_HOSTS[0] (см. assistant/channels.py:_build_email_link).
# Prod: https://consolidator.parts · Dev: http://localhost:8001
SITE_URL = os.getenv("SITE_URL", "").strip()

# ──────────────────────────────────────────────────────────────
# Реквизиты для пополнения депозита (банковский перевод).
# Сейчас платежи идут на нашу дубайскую компанию INNOVATION IDEA FZ LLC.
# В production переопределяй через env, если поменяется юр.лицо/счёт.
# ──────────────────────────────────────────────────────────────
TOPUP_BANK_BENEFICIARY      = os.getenv("TOPUP_BANK_BENEFICIARY",      "INNOVATION IDEA FZ LLC")
TOPUP_BANK_BENEFICIARY_ADDR = os.getenv("TOPUP_BANK_BENEFICIARY_ADDR",
    "Compass Building, Al Shohada Road, Al Hamra Industrial Zone-FZ, "
    "Ras Al Khaimah, UAE, P.O. Box 10055")
TOPUP_BANK_TRADE_LICENSE    = os.getenv("TOPUP_BANK_TRADE_LICENSE",    "5022051")  # RAKEZ
TOPUP_BANK_TAX_NO           = os.getenv("TOPUP_BANK_TAX_NO",           "104683265300001")
TOPUP_BANK_NAME             = os.getenv("TOPUP_BANK_NAME",             "Emirates NBD (Gold & Diamond Park Branch, Dubai)")
TOPUP_BANK_BRANCH_CODE      = os.getenv("TOPUP_BANK_BRANCH_CODE",      "0919")
TOPUP_BANK_SWIFT            = os.getenv("TOPUP_BANK_SWIFT",            "UNILAEAD")
TOPUP_BANK_IBAN             = os.getenv("TOPUP_BANK_IBAN",             "AE34 0470 0000 0020 0830 094")
TOPUP_BANK_ACCOUNT          = os.getenv("TOPUP_BANK_ACCOUNT",          "200830094")
TOPUP_BANK_CURRENCY         = os.getenv("TOPUP_BANK_CURRENCY",         "AED")  # счёт в дирхамах; USD-переводы конвертируются банком
# Контактная информация для подтверждения / вопросов по платежу
TOPUP_BANK_CONTACT_NAME     = os.getenv("TOPUP_BANK_CONTACT_NAME",     "Ali Abdul Rahman Mohammad Awwad")
TOPUP_BANK_CONTACT_PHONE    = os.getenv("TOPUP_BANK_CONTACT_PHONE",    "+971 551009394")
TOPUP_BANK_CONTACT_EMAIL    = os.getenv("TOPUP_BANK_CONTACT_EMAIL",    "contact@innovationidea.ae")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Secret в URL вебхука (защита от подделки). Длинная random-строка.
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
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514").strip()
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
TEUSTAT_STRICT_MODE = os.getenv("TEUSTAT_STRICT_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
LOGISTICS_PROVIDER = os.getenv("LOGISTICS_PROVIDER", "teustat").strip().lower() or "teustat"
LOGISTICS_STRICT_MODE = os.getenv("LOGISTICS_STRICT_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}

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
PAYMENT_CALLBACK_SECRET = os.getenv("PAYMENT_CALLBACK_SECRET", "").strip()

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
# Default periodic tasks (можно переопределить через admin → django-celery-beat).
# Это «hardcoded fallback», beat ещё подтянет всё из БД.
CELERY_BEAT_SCHEDULE = {
    "kyb_weekly_monitor": {
        "task": "assistant.tasks.kyb_weekly_monitor",
        # Раз в неделю — понедельник 03:00 Europe/Moscow
        "schedule": 60 * 60 * 24 * 7,
    },
    "prune_old_audit_monthly": {
        "task": "assistant.tasks.prune_old_audit",
        "schedule": 60 * 60 * 24 * 30,  # раз в 30 дней
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
