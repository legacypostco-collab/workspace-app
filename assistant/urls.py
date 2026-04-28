from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"conversations", views.ConversationViewSet, basename="conversation")

urlpatterns = [
    path("", include(router.urls)),
    path("chat/", views.ChatView.as_view(), name="assistant-chat"),
    path("feedback/", views.FeedbackView.as_view(), name="assistant-feedback"),
    path("suggest/", views.SuggestView.as_view(), name="assistant-suggest"),
    path("widget-config/", views.WidgetConfigView.as_view(), name="assistant-widget-config"),
]
