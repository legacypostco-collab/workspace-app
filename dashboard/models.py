from django.conf import settings
from django.db import models


class DashboardProjection(models.Model):
    class State(models.TextChoices):
        NORMAL = "normal", "Normal"
        ONBOARDING = "onboarding", "Onboarding"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    supplier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="supplier_dashboard_projections",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="user_dashboard_projections",
    )
    company_name = models.CharField(max_length=255, blank=True, default="")
    supplier_status = models.CharField(max_length=20, blank=True, default="")
    user_role = models.CharField(max_length=40, blank=True, default="")
    dashboard_state = models.CharField(max_length=20, choices=State.choices, default=State.NORMAL)
    new_rfqs_count = models.PositiveIntegerField(default=0)
    orders_in_progress_count = models.PositiveIntegerField(default=0)
    orders_sla_risk_count = models.PositiveIntegerField(default=0)
    products_updated_24h = models.PositiveIntegerField(default=0)
    import_errors_count = models.PositiveIntegerField(default=0)
    imports_total = models.PositiveIntegerField(default=0)
    failed_imports_30d = models.PositiveIntegerField(default=0)
    last_catalog_update_at = models.DateTimeField(null=True, blank=True)
    stale_products_count = models.PositiveIntegerField(default=0)
    low_completeness_count = models.PositiveIntegerField(default=0)
    permissions_summary_json = models.JSONField(default=dict, blank=True)
    quick_actions_json = models.JSONField(default=list, blank=True)
    widgets_json = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["supplier", "user"], name="uq_dashboard_projection_supplier_user"),
        ]
        indexes = [
            models.Index(fields=["supplier", "updated_at"]),
            models.Index(fields=["user", "updated_at"]),
            models.Index(fields=["dashboard_state"]),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_id}:{self.user_id}:{self.dashboard_state}"

# Create your models here.
