from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import auth_views, erp_views, pricelist, qr_scan, tg_views, upload, views

router = DefaultRouter()
router.register(r"conversations", views.ConversationViewSet, basename="conversation")

urlpatterns = [
    path("", include(router.urls)),
    path("chat/", views.ChatView.as_view(), name="assistant-chat"),
    path("action/", views.ActionView.as_view(), name="assistant-action"),
    path("feedback/", views.FeedbackView.as_view(), name="assistant-feedback"),
    path("suggest/", views.SuggestView.as_view(), name="assistant-suggest"),
    path("widget-config/", views.WidgetConfigView.as_view(), name="assistant-widget-config"),
    path("role/", views.RoleSwitchView.as_view(), name="assistant-role"),
    # Telegram bot webhook (prod). Secret в env TELEGRAM_WEBHOOK_SECRET.
    path("tg/webhook/<str:secret>/", tg_views.telegram_webhook, name="tg-webhook"),
    path("upload-spec/", upload.UploadSpecView.as_view(), name="assistant-upload-spec"),
    path("transcribe-audio/", upload.TranscribeAudioView.as_view(), name="assistant-transcribe"),
    path("recognize-photo/", upload.RecognizePhotoView.as_view(), name="assistant-recognize-photo"),
    # Projects
    path("projects/", views.ProjectListView.as_view(), name="assistant-projects"),
    path("projects/<uuid:project_id>/", views.ProjectDetailView.as_view(), name="assistant-project-detail"),
    path("projects/<uuid:project_id>/chats/", views.ProjectChatView.as_view(), name="assistant-project-chat"),
    # RFQ detail (for chat-first /chat/rfq/<id>/ page)
    path("rfq/<int:rfq_id>/", views.RFQDetailView.as_view(), name="assistant-rfq-detail"),
    # Notifications (bell + dropdown)
    path("notifications/", views.NotificationListView.as_view(), name="assistant-notifications"),
    path("notifications/read-all/", views.NotificationMarkReadView.as_view(), name="assistant-notifications-read-all"),
    path("notifications/<int:notif_id>/read/", views.NotificationMarkReadView.as_view(), name="assistant-notifications-read"),
    # Payments webhook (Stripe-compat)
    path("payments/webhook/", views.PaymentsWebhookView.as_view(), name="assistant-payments-webhook"),
    # QR scan (TZ §6.2): /api/assistant/qr/scan/<code>/
    path("qr/scan/<str:code>/", qr_scan.QRScanView.as_view(), name="qr-scan"),
    # Drawings (ТЗ §12.1): защищённый доступ к чертежу + audit log
    path("drawings/<int:drawing_id>/file/", views.DrawingFileView.as_view(), name="drawing-file"),
    # Auth — magic-link & OAuth
    path("auth/magic-link/", auth_views.MagicLinkRequestView.as_view(), name="auth-magic-link-request"),
    path("auth/magic-link/<str:token>/", auth_views.MagicLinkConfirmView.as_view(), name="auth-magic-link-confirm"),
    path("auth/oauth/<str:provider>/", auth_views.OAuthLoginView.as_view(), name="auth-oauth-login"),
    path("auth/oauth/callback/<str:provider>/", auth_views.OAuthCallbackView.as_view(), name="auth-oauth-callback"),
    # ERP двусторонний обмен (ТЗ §17.2): аутентификация по X-Api-Token
    path("erp/sync/parts/", erp_views.sync_parts_push, name="erp-sync-parts"),
    path("erp/sync/orders/", erp_views.sync_orders_pull, name="erp-sync-orders"),
    path("erp/sync/orders/<int:order_id>/ack/", erp_views.sync_order_ack, name="erp-sync-order-ack"),
    path("erp/sync/orders/<int:order_id>/status/", erp_views.sync_order_status, name="erp-sync-order-status"),
    # Pricelist upload в чате (ТЗ: AI-маппинг колонок)
    path("upload-pricelist/", pricelist.PricelistUploadView.as_view(), name="pricelist-upload"),
    path("upload-pricelist/<int:import_id>/commit/", pricelist.PricelistCommitView.as_view(), name="pricelist-commit"),
    path("upload-pricelist/<int:import_id>/cancel/", pricelist.PricelistCancelView.as_view(), name="pricelist-cancel"),
    path("upload-pricelist/<int:import_id>/ai-estimate/", pricelist.PricelistAiEstimateView.as_view(), name="pricelist-ai-estimate"),
    path("upload-pricelist/<int:import_id>/ai-estimate-progress/", pricelist.PricelistAiEstimateProgressView.as_view(), name="pricelist-ai-estimate-progress"),
    path("upload-pricelist/<int:import_id>/import-progress/", pricelist.PricelistImportProgressView.as_view(), name="pricelist-import-progress"),
    path("upload-pricelist/<int:import_id>/smart-questions/", pricelist.PricelistSmartQuestionsView.as_view(), name="pricelist-smart-questions"),
    path("upload-pricelist/<int:import_id>/generate-output/", pricelist.PricelistGenerateOutputView.as_view(), name="pricelist-generate-output"),
    path("upload-pricelist/<int:import_id>/generate-output-progress/", pricelist.PricelistGenerateOutputProgressView.as_view(), name="pricelist-generate-output-progress"),
    path("upload-pricelist/<int:import_id>/output-preview/", pricelist.PricelistOutputPreviewView.as_view(), name="pricelist-output-preview"),
    path("pricelist-template.csv", pricelist.PricelistTemplateView.as_view(), name="pricelist-template"),
    path("pricelist-template.xlsx", pricelist.PricelistTemplateXlsxView.as_view(), name="pricelist-template-xlsx"),
]
