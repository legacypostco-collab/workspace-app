from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"conversations", views.ConversationViewSet, basename="conversation")

urlpatterns = [
    path("", include(router.urls)),
    path("chat/", views.ChatView.as_view(), name="assistant-chat"),
    path("action/", views.ActionView.as_view(), name="assistant-action"),
    path("feedback/", views.FeedbackView.as_view(), name="assistant-feedback"),
    path("suggest/", views.SuggestView.as_view(), name="assistant-suggest"),
    path("widget-config/", views.WidgetConfigView.as_view(), name="assistant-widget-config"),
    # Projects
    path("projects/", views.ProjectListView.as_view(), name="assistant-projects"),
    path("projects/<uuid:project_id>/", views.ProjectDetailView.as_view(), name="assistant-project-detail"),
    path("projects/<uuid:project_id>/chats/", views.ProjectChatView.as_view(), name="assistant-project-chat"),
]
