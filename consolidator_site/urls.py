from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path, reverse
from django.utils import timezone
from django.views.decorators.cache import cache_control
from django.views.i18n import JavaScriptCatalog
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.permissions import IsAdminUser


# ── SEO: sitemap.xml + robots.txt ──────────────────────────────
# Минимально достаточный sitemap для индексации публичных страниц.
# Расширяется при появлении новых публичных URL.
@cache_control(public=True, max_age=86400)
def robots_txt(request):
    # SEO P1: Sitemap URL должен быть абсолютным (Google требование).
    base = request.build_absolute_uri("/").rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "Disallow: /chat/\n"           # chat-first SPA, нет смысла индексировать
        "Disallow: /demo-login/\n"
        "Disallow: /password_reset/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return HttpResponse(body, content_type="text/plain")


@cache_control(public=True, max_age=3600)
def sitemap_xml(request):
    base = request.build_absolute_uri("/").rstrip("/")
    today = timezone.now().strftime("%Y-%m-%d")
    urls = [
        # url, changefreq, priority
        ("/",          "weekly",  "1.0"),
        ("/help/",     "weekly",  "0.8"),  # FAQ — стабильно растёт, важно для SEO
        ("/terms/",    "yearly",  "0.3"),
        ("/privacy/",  "yearly",  "0.3"),
    ]
    items = "\n".join(
        f"  <url><loc>{base}{u}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{cf}</changefreq><priority>{p}</priority></url>"
        for u, cf, p in urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}\n"
        "</urlset>\n"
    )
    return HttpResponse(xml, content_type="application/xml")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("jsi18n/", JavaScriptCatalog.as_view(domain="django"), name="javascript-catalog"),
    path("robots.txt", robots_txt, name="robots-txt"),
    path("sitemap.xml", sitemap_xml, name="sitemap-xml"),
    path("api/v1/", include("marketplace.api_urls")),
    path("api/assistant/", include("assistant.urls")),
    # API documentation — staff only (IDOR/info-leak fix from QA report)
    path("api/schema/", SpectacularAPIView.as_view(
        permission_classes=[IsAdminUser]), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(
        url_name="schema", permission_classes=[IsAdminUser]), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(
        url_name="schema", permission_classes=[IsAdminUser]), name="redoc"),
    path("", include("marketplace.urls")),
]

if getattr(settings, "SERVE_MEDIA", False):
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
