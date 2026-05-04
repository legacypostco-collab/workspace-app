from django.contrib import admin

from .models import Conversation, Feedback, KnowledgeChunk, Message


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "role", "title", "is_active", "updated_at"]
    list_filter = ["role", "is_active", "created_at"]
    search_fields = ["user__username", "title"]
    raw_id_fields = ["user"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ["id", "conversation", "role", "tokens_used", "created_at"]
    list_filter = ["role", "created_at"]
    search_fields = ["content"]
    readonly_fields = ["id", "created_at", "tokens_used"]
    raw_id_fields = ["conversation"]


@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(admin.ModelAdmin):
    list_display = ["id", "source_type", "source_id", "title", "language", "is_active", "indexed_at"]
    list_filter = ["source_type", "language", "is_active"]
    search_fields = ["title", "content", "source_id"]
    readonly_fields = ["id", "indexed_at"]
    fieldsets = (
        (None, {"fields": ("source_type", "source_id", "title", "is_active")}),
        ("Content", {"fields": ("content", "language", "metadata", "access_roles")}),
        ("Vector", {"fields": ("embedding",), "classes": ("collapse",)}),
        ("Meta", {"fields": ("id", "indexed_at")}),
    )


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ["message", "rating", "created_at"]
    list_filter = ["rating", "created_at"]
