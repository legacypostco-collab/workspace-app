from django.urls import path

from . import views

app_name = "control"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("search/", views.search, name="search"),
    path("notifications/", views.notifications, name="notifications"),
    path(
        "notifications/<int:notification_id>/open/",
        views.notification_open,
        name="notification_open",
    ),
    path("finance/", views.finance, name="finance"),
    path("finance/<int:invoice_id>/", views.invoice_detail, name="invoice_detail"),
    path("orders/", views.orders, name="orders"),
    path("orders/<int:order_id>/", views.order_detail, name="order_detail"),
    path("requests/<int:rfq_id>/", views.rfq_detail, name="rfq_detail"),
    path("users/", views.users, name="users"),
    path("users/<int:user_id>/", views.user_detail, name="user_detail"),
    path("moderation/", views.moderation, name="moderation"),
    path(
        "moderation/companies/<int:user_id>/",
        views.verification_detail,
        name="verification_detail",
    ),
    path("catalog/", views.catalog, name="catalog"),
    path("support/", views.support, name="support"),
    path("support/<uuid:conversation_id>/", views.support_detail, name="support_detail"),
    path("audit/", views.audit, name="audit"),
    path("settings/", views.platform_settings, name="settings"),
]
