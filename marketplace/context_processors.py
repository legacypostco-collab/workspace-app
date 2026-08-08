from django.conf import settings


def privacy_controls(_request):
    return {
        "cookie_consent_version": settings.COOKIE_CONSENT_VERSION,
        "analytics_enabled": settings.ANALYTICS_ENABLED,
    }
