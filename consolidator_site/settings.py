import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "dev-secret-change-in-production"
DEBUG = True


def _env_list(name: str, default: str = "") -> list[str]:
    raw = (os.getenv(name, default) or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


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
    "marketplace",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
            ],
        },
    },
]

WSGI_APPLICATION = "consolidator_site.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 60,
        },
    }
}

AUTH_PASSWORD_VALIDATORS = []

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
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/"

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
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
