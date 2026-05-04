from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import upload, views

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
    path("upload-spec/", upload.UploadSpecView.as_view(), name="assistant-upload-spec"),
    path("transcribe-audio/", upload.TranscribeAudioView.as_view(), name="assistant-transcribe"),
    path("recognize-photo/", upload.RecognizePhotoView.as_view(), name="assistant-recognize-photo"),
    # Projects
    path("projects/", views.ProjectListView.as_view(), name="assistant-projects"),
    path("projects/<uuid:project_id>/", views.ProjectDetailView.as_view(), name="assistant-project-detail"),
    path("projects/<uuid:project_id>/chats/", views.ProjectChatView.as_view(), name="assistant-project-chat"),
    # RFQ detail (for chat-first /chat/rfq/<id>/ page)
    path("rfq/<int:rfq_id>/", views.RFQDetailView.as_view(), name="assistant-rfq-detail"),
]
