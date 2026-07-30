from urllib.parse import urlencode

from django.contrib.auth import views as auth_views
from django.http import HttpResponseGone
from django.shortcuts import redirect
from django.urls import path

from . import feedback as _feedback
from . import gdpr as _gdpr
from . import health as _health
from . import views


def _chat_url(action=None, **params):
    query = {"new": "1"}
    if action:
        query["run"] = action
    query.update({key: value for key, value in params.items() if value not in (None, "")})
    return f"/chat/?{urlencode(query)}"


def legacy_to_chat(request, *args, **kwargs):
    return redirect("/chat/")


def public_workspace(request, *args, **kwargs):
    return redirect("/chat/?workspace=1")


def catalog_to_chat(request, *args, **kwargs):
    return redirect(
        _chat_url(
            "search_parts",
            query=request.GET.get("query") or request.GET.get("q"),
            brand=request.GET.get("brand"),
            category=request.GET.get("category"),
        )
    )


def brands_to_chat(request, *args, **kwargs):
    return redirect(
        _chat_url(
            "browse_brands",
            query=request.GET.get("query") or request.GET.get("q"),
        )
    )


def categories_to_chat(request, *args, **kwargs):
    return redirect(
        _chat_url(
            "browse_categories",
            query=request.GET.get("query") or request.GET.get("q"),
        )
    )


def suppliers_to_chat(request, *args, **kwargs):
    return redirect(_chat_url("top_suppliers"))


def part_to_chat(request, slug, *args, **kwargs):
    return redirect(_chat_url("search_parts", query=slug))


def legacy_rfq_to_chat(request, rfq_id, *args, **kwargs):
    return redirect(_chat_url("get_rfq_status", rfq_id=rfq_id))


def order_to_chat(request, order_id, *args, **kwargs):
    return redirect(_chat_url("get_order_detail", order_id=order_id))


def invoice_to_chat(request, order_id, *args, **kwargs):
    return redirect(_chat_url("list_order_documents", order_id=order_id))


def notifications_to_chat(request, *args, **kwargs):
    return redirect(_chat_url("notifications"))


def kyb_to_chat(request, *args, **kwargs):
    return redirect(_chat_url("kyb_status"))


def twofa_to_chat(request, *args, **kwargs):
    return redirect(_chat_url("setup_2fa"))


def removed_portal_route(request, *args, **kwargs):
    response = HttpResponseGone(
        "Этот устаревший кабинет удалён. Используйте рабочее пространство чата."
    )
    response["Cache-Control"] = "no-store"
    return response


def removed_mutation_route(request, *args, **kwargs):
    response = HttpResponseGone(
        "Эта устаревшая операция удалена. Выполните действие в рабочем пространстве чата."
    )
    response["Cache-Control"] = "no-store"
    return response


urlpatterns = [
    path("healthz/", _health.liveness, name="healthz"),
    path("readyz/", _health.readiness, name="readyz"),
    path(
        "password_reset/",
        views.RateLimitedPasswordResetView.as_view(
            email_template_name="registration/password_reset_email.txt",
            subject_template_name="registration/password_reset_subject.txt",
        ),
        name="password_reset",
    ),
    path(
        "password_reset/done/",
        auth_views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path("password-reset/", lambda request: redirect("password_reset")),
    path("password-reset/done/", lambda request: redirect("password_reset_done")),
    path("api/me/export/", _gdpr.export_my_data, name="gdpr_export"),
    path("api/me/delete/", _gdpr.delete_my_account, name="gdpr_delete"),
    path("api/feedback/", _feedback.submit_feedback, name="submit_feedback"),
    path("api/set-language/", views.set_language_api, name="set_language_api"),
    path("api/notifications/", views.notifications_list, name="notifications_api"),
    path(
        "api/notifications/read/",
        views.notifications_mark_read,
        name="notifications_mark_all_read",
    ),
    path(
        "api/notifications/<int:notif_id>/read/",
        views.notifications_mark_read,
        name="notifications_mark_read",
    ),
    path("", views.home, name="home"),
    path("landing/", views.landing_view, name="landing"),
    path("demo-center/", public_workspace, name="demo_center"),
    path("demo/", public_workspace),
    path("terms/", views.terms_view, name="terms"),
    path("privacy/", views.privacy_view, name="privacy"),
    path("cookies/", views.cookies_view, name="cookies"),
    path("help/", views.help_view, name="help"),
    path("faq/", views.help_view),
    path("chat/", views.chat_first_view, name="chat"),
    path("chat/project/<uuid:project_id>/", views.chat_project_view, name="chat_project"),
    path("chat/rfq/<int:rfq_id>/", views.chat_rfq_view, name="chat_rfq"),
    path("chat/proposal/<int:rfq_id>/", legacy_rfq_to_chat, name="chat_proposal"),
    path("i/<str:code>/", views.invite_redirect, name="invite_redirect"),
    path("register/", views.register_view, name="register"),
    path("verify-email/<str:token>/", views.verify_email_view, name="verify_email"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("catalog/", catalog_to_chat, name="catalog"),
    path("parts/<slug:slug>/", part_to_chat, name="part_detail"),
    path("directory/brands/", brands_to_chat, name="brands_directory"),
    path("brands/", brands_to_chat),
    path("directory/suppliers/", suppliers_to_chat, name="suppliers_directory"),
    path("suppliers/", suppliers_to_chat),
    path("directory/categories/", categories_to_chat, name="categories_directory"),
    path("categories/", categories_to_chat),
    path("rfq/", lambda request: redirect(_chat_url("get_rfq_status")), name="rfq_list"),
    path("rfq/new/", lambda request: redirect(_chat_url("create_rfq")), name="rfq_new"),
    path("rfq/<int:rfq_id>/", legacy_rfq_to_chat, name="rfq_detail"),
    path("rfq/<int:rfq_id>/proposal/", legacy_rfq_to_chat, name="rfq_proposal"),
    path(
        "rfq/<int:rfq_id>/proposal/pdf/",
        legacy_rfq_to_chat,
        name="rfq_proposal_pdf",
    ),
    path(
        "rfq/<int:rfq_id>/proposal/logistics/",
        legacy_rfq_to_chat,
        name="rfq_logistics_estimate",
    ),
    path("rfq/<int:rfq_id>/checkout/", legacy_rfq_to_chat, name="rfq_checkout"),
    path("compare/", lambda request: redirect(_chat_url("compare_products")), name="compare"),
    path("comparison/", lambda request: redirect(_chat_url("compare_products"))),
    path("cart/", lambda request: redirect(_chat_url("get_my_deals")), name="cart"),
    path("checkout/", lambda request: redirect(_chat_url("get_my_deals")), name="checkout"),
    path("dashboard/", legacy_to_chat, name="dashboard"),
    path("orders/<int:order_id>/", order_to_chat, name="order_detail"),
    path("orders/<int:order_id>/invoice/", invoice_to_chat, name="order_invoice"),
    path(
        "orders/<int:order_id>/invoice/pdf/",
        invoice_to_chat,
        name="order_invoice_pdf",
    ),
    path("notifications/", notifications_to_chat, name="notifications"),
    path("kyb/", kyb_to_chat, name="kyb"),
    path("2fa/", twofa_to_chat, name="twofa_setup"),
    path("reports/kpi/export.csv", views.kpi_reports_export_csv, name="kpi_reports_export_csv"),
    path("reports/claims/export.csv", views.claims_export_csv, name="claims_export_csv"),
    path("payments/callback/", views.payment_callback, name="payment_callback"),
    path("orders/<int:order_id>/reserve-paid/", removed_mutation_route, name="order_mark_reserve_paid"),
    path("orders/<int:order_id>/final-paid/", removed_mutation_route, name="order_mark_final_paid"),
    path("orders/<int:order_id>/mid-paid/", removed_mutation_route, name="order_mark_mid_paid"),
    path("orders/<int:order_id>/customs-paid/", removed_mutation_route, name="order_mark_customs_paid"),
    path("orders/<int:order_id>/confirm-quality/", removed_mutation_route, name="order_confirm_quality"),
    path("orders/<int:order_id>/documents/add/", removed_mutation_route, name="order_add_document"),
    path("orders/<int:order_id>/claims/open/", removed_mutation_route, name="order_open_claim"),
    path("claims/<int:claim_id>/status/", removed_mutation_route, name="order_update_claim_status"),
    path("buyer/", removed_portal_route),
    path("buyer/<path:legacy_path>", removed_portal_route),
    path("seller/", removed_portal_route),
    path("seller/<path:legacy_path>", removed_portal_route),
    path("operator/", removed_portal_route),
    path("operator/<path:legacy_path>", removed_portal_route),
    path("admin-panel/", removed_portal_route),
    path("admin-panel/<path:legacy_path>", removed_portal_route),
]
