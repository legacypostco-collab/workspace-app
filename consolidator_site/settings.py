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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
    "http://127.0.0.1,http://localhost,https://*.localhost.run,https://*.lhr.life",
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "files",
    "catalog",
    "offers",
    "imports",
    "projections",
    "dashboard",
    "marketplace",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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

if DB_NAME:
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": DB_NAME,
            "USER": DB_USER,
            "PASSWORD": DB_PASSWORD,
            "HOST": DB_HOST,
            "PORT": DB_PORT,
            "CONN_MAX_AGE": int(_env("DB_CONN_MAX_AGE", "60")),
            "OPTIONS": {"connect_timeout": int(_env("DB_CONNECT_TIMEOUT", "5"))},
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
]
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
SERVE_MEDIA = _env_bool("SERVE_MEDIA", DEBUG)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/"
USE_HTTPS = _env_bool("USE_HTTPS", False)
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", USE_HTTPS)
CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", USE_HTTPS)
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
}

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    }
}

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
