from django.contrib import admin

from .models import (
    Brand,
    Category,
    Order,
    OrderClaim,
    OrderDocument,
    OrderEvent,
    OrderItem,
    Part,
    RFQ,
    RFQItem,
    SupplierRatingEvent,
    UserProfile,
    WebhookDeliveryLog,
)


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "region", "is_component_manufacturer")
    list_filter = ("region", "is_component_manufacturer")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "seller",
        "brand",
        "oem_number",
        "price",
        "currency",
        "stock_quantity",
        "availability_status",
        "condition",
        "is_active",
    )
    list_filter = ("condition", "availability_status", "is_active", "category", "brand")
    search_fields = ("title", "oem_number")
    prepopulated_fields = {"slug": ("title",)}


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


class OrderDocumentInline(admin.TabularInline):
    model = OrderDocument
    extra = 0


class OrderClaimInline(admin.TabularInline):
    model = OrderClaim
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "buyer",
        "customer_name",
        "customer_email",
        "status",
        "sla_status",
        "sla_breaches_count",
        "logistics_cost",
        "total_amount",
        "created_at",
    )
    list_filter = ("status", "sla_status")
    inlines = [OrderItemInline, OrderDocumentInline, OrderClaimInline]


@admin.register(OrderEvent)
class OrderEventAdmin(admin.ModelAdmin):
    list_display = ("order", "event_type", "source", "actor", "created_at")
    list_filter = ("event_type", "source", "created_at")
    search_fields = ("order__id", "actor__username")


@admin.register(OrderDocument)
class OrderDocumentAdmin(admin.ModelAdmin):
    list_display = ("order", "doc_type", "title", "uploaded_by", "created_at")
    list_filter = ("doc_type", "created_at")
    search_fields = ("order__id", "title", "file_url")


@admin.register(OrderClaim)
class OrderClaimAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "title", "status", "opened_by", "resolved_by", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("order__id", "title", "description")


class RFQItemInline(admin.TabularInline):
    model = RFQItem
    extra = 0
    autocomplete_fields = ("matched_part",)
    readonly_fields = ("confidence", "decision_reason", "recommended_supplier_status")


@admin.register(RFQ)
class RFQAdmin(admin.ModelAdmin):
    list_display = ("id", "customer_name", "company_name", "mode", "urgency", "status", "created_at")
    list_filter = ("mode", "urgency", "status")
    search_fields = ("customer_name", "customer_email", "company_name")
    inlines = [RFQItemInline]


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "role",
        "department",
        "company_name",
        "rating_score",
        "supplier_status",
        "can_manage_assortment",
        "can_manage_pricing",
        "can_manage_orders",
        "can_view_analytics",
    )
    list_filter = ("role", "department", "supplier_status", "can_manage_assortment", "can_manage_pricing", "can_manage_orders")
    readonly_fields = ("rating_score", "supplier_status", "last_rating_recalculated_at")
    filter_horizontal = ("allowed_brands",)
    actions = ("apply_department_permission_template",)

    @admin.action(description="Применить шаблон прав по отделу")
    def apply_department_permission_template(self, request, queryset):
        templates = {
            "director": {
                "can_manage_assortment": True,
                "can_manage_pricing": True,
                "can_manage_orders": True,
                "can_manage_drawings": True,
                "can_view_analytics": True,
                "can_manage_team": True,
            },
            "sales": {
                "can_manage_assortment": True,
                "can_manage_pricing": True,
                "can_manage_orders": False,
                "can_manage_drawings": False,
                "can_view_analytics": True,
                "can_manage_team": False,
            },
            "logistics": {
                "can_manage_assortment": False,
                "can_manage_pricing": False,
                "can_manage_orders": True,
                "can_manage_drawings": False,
                "can_view_analytics": True,
                "can_manage_team": False,
            },
            "finance": {
                "can_manage_assortment": False,
                "can_manage_pricing": True,
                "can_manage_orders": False,
                "can_manage_drawings": False,
                "can_view_analytics": True,
                "can_manage_team": False,
            },
            "engineering": {
                "can_manage_assortment": True,
                "can_manage_pricing": False,
                "can_manage_orders": False,
                "can_manage_drawings": True,
                "can_view_analytics": False,
                "can_manage_team": False,
            },
            "viewer": {
                "can_manage_assortment": False,
                "can_manage_pricing": False,
                "can_manage_orders": False,
                "can_manage_drawings": False,
                "can_view_analytics": True,
                "can_manage_team": False,
            },
        }

        updated = 0
        skipped = 0
        for profile in queryset:
            template = templates.get(profile.department)
            if not template:
                skipped += 1
                continue
            for field, value in template.items():
                setattr(profile, field, value)
            profile.save(
                update_fields=[
                    "can_manage_assortment",
                    "can_manage_pricing",
                    "can_manage_orders",
                    "can_manage_drawings",
                    "can_view_analytics",
                    "can_manage_team",
                ]
            )
            updated += 1

        self.message_user(
            request,
            f"Шаблоны применены: {updated}. Пропущено: {skipped}.",
        )


@admin.register(SupplierRatingEvent)
class SupplierRatingEventAdmin(admin.ModelAdmin):
    list_display = ("supplier", "event_type", "impact_score", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("supplier__username", "supplier__email")


@admin.register(WebhookDeliveryLog)
class WebhookDeliveryLogAdmin(admin.ModelAdmin):
    list_display = ("order", "endpoint", "success", "attempt", "status_code", "created_at")
    list_filter = ("success", "status_code", "created_at")
    search_fields = ("order__id", "endpoint", "error")
