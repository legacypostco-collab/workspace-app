from django.urls import path

from . import api_views

urlpatterns = [
    path("categories/", api_views.api_categories, name="api_categories"),
    path("parts/", api_views.api_parts, name="api_parts"),
    path("parts/<int:part_id>/", api_views.api_part_detail, name="api_part_detail"),
    path("orders/my/", api_views.api_my_orders, name="api_my_orders"),
    path("seller/parts/", api_views.api_seller_parts, name="api_seller_parts"),
    path("dashboard/summary/", api_views.api_dashboard_summary, name="api_dashboard_summary"),
]

