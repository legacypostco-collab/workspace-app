from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from marketplace.models import Part, SellerImportRun

from .models import DashboardProjection


def refresh_supplier_dashboard_projection(supplier: User) -> DashboardProjection:
    latest_import = SellerImportRun.objects.filter(seller=supplier).order_by("-created_at").first()
    now = timezone.now()
    recent_boundary = now - timedelta(hours=24)

    total_parts = Part.objects.filter(seller=supplier).count()
    active_parts = Part.objects.filter(seller=supplier, is_active=True).count()
    recent_updates_24h = Part.objects.filter(seller=supplier, data_updated_at__gte=recent_boundary).count()
    import_runs_total = SellerImportRun.objects.filter(seller=supplier).count()
    import_runs_failed = SellerImportRun.objects.filter(seller=supplier, status="failed").count()

    projection, _ = DashboardProjection.objects.update_or_create(
        supplier=supplier,
        defaults={
            "last_import_run": latest_import,
            "total_parts": total_parts,
            "active_parts": active_parts,
            "recent_updates_24h": recent_updates_24h,
            "import_runs_total": import_runs_total,
            "import_runs_failed": import_runs_failed,
            "last_import_created": latest_import.created_count if latest_import else 0,
            "last_import_updated": latest_import.updated_count if latest_import else 0,
            "last_import_errors": latest_import.error_count if latest_import else 0,
        },
    )
    return projection
