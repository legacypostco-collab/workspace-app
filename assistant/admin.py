from django.contrib import admin

from .models import (
    Conversation, Feedback, KnowledgeChunk, Message,
    Wallet, WalletTopupRequest, WalletTx,
)


@admin.register(WalletTopupRequest)
class WalletTopupRequestAdmin(admin.ModelAdmin):
    list_display = ["reference_code", "user", "amount", "currency", "method",
                    "status", "created_at", "confirmed_at"]
    list_filter = ["status", "method", "created_at"]
    search_fields = ["reference_code", "user__username", "user__email"]
    readonly_fields = ["reference_code", "created_at", "updated_at",
                        "user_claim_at", "confirmed_at", "cancelled_at",
                        "payment_details"]
    actions = ["mark_paid_action", "mark_failed_action"]
    list_select_related = ["user", "confirmed_by"]

    @admin.action(description="Подтвердить оплату → зачислить на депозит")
    def mark_paid_action(self, request, queryset):
        ok = 0
        for req in queryset.exclude(status__in=["paid", "cancelled", "failed", "expired"]):
            req.mark_paid(by_user=request.user)
            ok += 1
        self.message_user(request, f"Подтверждено и зачислено: {ok}")

    @admin.action(description="Отклонить заявку")
    def mark_failed_action(self, request, queryset):
        from django.utils import timezone
        n = queryset.exclude(status__in=["paid", "cancelled"]).update(
            status="failed", cancelled_at=timezone.now(),
        )
        self.message_user(request, f"Отклонено: {n}")


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["user", "balance", "currency", "updated_at"]
    search_fields = ["user__username", "user__email"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(WalletTx)
class WalletTxAdmin(admin.ModelAdmin):
    list_display = ["wallet", "kind", "amount", "balance_after",
                    "order_id", "created_at"]
    list_filter = ["kind", "created_at"]
    search_fields = ["wallet__user__username", "description", "order_id"]
    readonly_fields = ["created_at"]


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
