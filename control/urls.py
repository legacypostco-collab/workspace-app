from django.urls import path

from . import views

app_name = "control"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("search/", views.search, name="search"),
    path("finance/", views.finance, name="finance"),
    path("finance/<int:invoice_id>/", views.invoice_detail, name="invoice_detail"),
    path("orders/", views.orders, name="orders"),
    path("orders/<int:order_id>/", views.order_detail, name="order_detail"),
    path("users/", views.users, name="users"),
    path("users/<int:user_id>/", views.user_detail, name="user_detail"),
    path("moderation/", views.moderation, name="moderation"),
    path("catalog/", views.catalog, name="catalog"),
    path("support/", views.support, name="support"),
    path("audit/", views.audit, name="audit"),
    path("settings/", views.platform_settings, name="settings"),
]
