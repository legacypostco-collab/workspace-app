from django.conf import settings
from django.db import models


class DashboardProjection(models.Model):
    supplier = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dashboard_projection",
    )
    last_import_run = models.ForeignKey(
        "marketplace.SellerImportRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dashboard_projections",
    )
    total_parts = models.PositiveIntegerField(default=0)
    active_parts = models.PositiveIntegerField(default=0)
    recent_updates_24h = models.PositiveIntegerField(default=0)
    import_runs_total = models.PositiveIntegerField(default=0)
    import_runs_failed = models.PositiveIntegerField(default=0)
    last_import_created = models.PositiveIntegerField(default=0)
    last_import_updated = models.PositiveIntegerField(default=0)
    last_import_errors = models.PositiveIntegerField(default=0)
    refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["refreshed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_id}:{self.refreshed_at.isoformat()}"
