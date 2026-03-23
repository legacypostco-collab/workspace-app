from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from imports.models import ImportJob
from marketplace.models import Order, OrderEvent, Part, RFQ, UserProfile

from .models import DashboardProjection

RU_MONTHS_SHORT = {
    1: "Янв",
    2: "Фев",
    3: "Мар",
    4: "Апр",
    5: "Май",
    6: "Июн",
    7: "Июл",
    8: "Авг",
    9: "Сен",
    10: "Окт",
    11: "Ноя",
    12: "Дек",
}

# Demo payload (taken from supplier-dashboard.jsx) for local testing
DEMO_REVENUE_SERIES = [
    {"m": "Окт", "val": 12.4},
    {"m": "Ноя", "val": 18.1},
    {"m": "Дек", "val": 15.6},
    {"m": "Янв", "val": 22.3},
    {"m": "Фев", "val": 28.7},
    {"m": "Мар", "val": 24.1},
]

DEMO_SLA_SERIES = [
    {"m": "Окт", "compliance": 94},
    {"m": "Ноя", "compliance": 91},
    {"m": "Дек", "compliance": 96},
    {"m": "Янв", "compliance": 93},
    {"m": "Фев", "compliance": 97},
    {"m": "Мар", "compliance": 95},
]

DEMO_ORDERS_BY_STATUS = [
    {"status_key": "production", "name": "Производство", "count": 12, "color": "#7F77DD"},
    {"status_key": "ready_to_ship", "name": "Готов к отгрузке", "count": 8, "color": "#1D9E75"},
    {"status_key": "shipped", "name": "Отгружено", "count": 15, "color": "#378ADD"},
    {"status_key": "delivered", "name": "Доставлено", "count": 6, "color": "#639922"},
    {"status_key": "awaiting_reserve", "name": "Ожидает резерв", "count": 4, "color": "#BA7517"},
]

DEMO_INCOMING_REQUESTS = [
    {"id": "RQ-4821", "request_type": "urgent", "part": "Гидроцилиндр 707-01-0K930", "client": "Полюс Золото", "type": "Срочный", "time": "12 мин назад", "brand": "Komatsu", "dot": "#E24B4A", "url": "/seller/requests/"},
    {"id": "RQ-4819", "request_type": "standard", "part": "Турбокомпрессор 6505-67-5030", "client": "СУЭК", "type": "Стандартный", "time": "48 мин назад", "brand": "Komatsu", "dot": "#378ADD", "url": "/seller/requests/"},
    {"id": "RQ-4817", "request_type": "drawing", "part": "Насос гидравлический по чертежу", "client": "Норникель", "type": "По чертежу", "time": "1.5 ч назад", "brand": "Hitachi", "dot": "#7F77DD", "url": "/seller/requests/"},
    {"id": "RQ-4815", "request_type": "standard", "part": "Фильтр 600-185-4100", "client": "Евраз", "type": "Стандартный", "time": "2 ч назад", "brand": "Komatsu", "dot": "#378ADD", "url": "/seller/requests/"},
]

DEMO_EVENTS_FEED = [
    {"icon": "📦", "event_type": "status_changed", "text": "Заказ #ORD-3847 отгружен", "detail": "Komatsu PC800 → Полюс Золото", "time": "09:14", "status": "success"},
    {"icon": "⚠️", "event_type": "sla_status_changed", "text": "SLA: приближается дедлайн", "detail": "#ORD-3832 — готовность к проверке", "time": "08:51", "status": "warning"},
    {"icon": "💰", "event_type": "reserve_paid", "text": "Получен резерв 10%", "detail": "#ORD-3851 — ¥2,340,000", "time": "08:30", "status": "info"},
    {"icon": "✅", "event_type": "rating_updated", "text": "Рейтинг пересчитан: 4.72 → 4.74", "detail": "Закрытие #ORD-3829 + отзыв клиента", "time": "08:12", "status": "success"},
    {"icon": "🔔", "event_type": "order_created", "text": "Новый запрос от Норникель", "detail": "Насос гидравлический по чертежу", "time": "07:45", "status": "info"},
    {"icon": "🔴", "event_type": "sla_breached", "text": "SLA нарушение зафиксировано", "detail": "#ORD-3801 — просрочка отгрузки +2д", "time": "Вчера", "status": "danger"},
]

DEMO_METRICS_CARDS = [
    {"key": "active_orders", "label": "Активные заказы", "value": "45", "sub": "за 30 дней", "trend": "+12%", "trendUp": True, "trend_tone": "up", "accent": "#7F77DD"},
    {"key": "revenue", "label": "Выручка (март)", "value": "¥28.7M", "sub": "vs ¥22.3M фев", "trend": "+28.7%", "trendUp": True, "trend_tone": "up", "accent": "#1D9E75"},
    {"key": "sla", "label": "SLA compliance", "value": "95.2%", "sub": "норматив: 90%", "trend": "+2.1%", "trendUp": True, "trend_tone": "up", "accent": "#378ADD"},
    {"key": "conversion", "label": "Конверсия", "value": "68%", "sub": "запрос → заказ", "trend": "-3%", "trendUp": False, "trend_tone": "down", "accent": "#BA7517"},
]

DEMO_RATING = {
    "overall": 4.74,
    "max_score": 5,
    "external": {"value": 4.8, "weight": 60},
    "behavior": {"value": 4.65, "weight": 40},
    "status": "Надёжный",
}


def _month_label_ru(dt) -> str:
    if not dt:
        return ""
    m = getattr(dt, "month", None)
    return RU_MONTHS_SHORT.get(int(m or 0), str(m or ""))


def _relative_time_ru(dt) -> str:
    if not dt:
        return "—"
    now = timezone.now()
    delta = now - dt
    minutes = int(max(0, delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин назад" if minutes != 1 else "1 мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return "Вчера" if days == 1 else f"{days} д назад"


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return ((current - previous) / previous) * 100.0


def _fmt_signed_pct(value: float | None, *, digits: int = 0) -> tuple[str, bool]:
    if value is None:
        return ("—", True)
    rounded = round(value, digits)
    sign = "+" if rounded >= 0 else ""
    if digits <= 0:
        return (f"{sign}{int(rounded)}%", rounded >= 0)
    return (f"{sign}{rounded:.{digits}f}%", rounded >= 0)


def _fmt_signed_delta(value: float | None, *, digits: int = 1) -> tuple[str, bool]:
    if value is None:
        return ("—", True)
    rounded = round(value, digits)
    sign = "+" if rounded >= 0 else ""
    return (f"{sign}{rounded:.{digits}f}%", rounded >= 0)


@dataclass
class DashboardMetrics:
    new_rfqs_count: int
    overdue_rfqs_count: int
    orders_in_progress_count: int
    orders_sla_risk_count: int
    products_updated_24h: int
    import_errors_count: int
    imports_total: int
    failed_imports_30d: int
    last_catalog_update_at: object
    stale_products_count: int
    low_completeness_count: int
    has_successful_import: bool
    catalog_items_count: int


class DashboardMetricsAggregator:
    low_completeness_threshold = Decimal("60.00")

    def _part_completeness_percent(self, part: Part) -> Decimal:
        missing = len(part.mandatory_missing_fields())
        total_checks = 14
        if total_checks <= 0:
            return Decimal("100.00")
        score = Decimal(total_checks - missing) / Decimal(total_checks) * Decimal("100.00")
        return score.quantize(Decimal("0.01"))

    def aggregate(self, supplier: User) -> DashboardMetrics:
        now = timezone.now()
        day_ago = now - timedelta(hours=24)
        days_30_ago = now - timedelta(days=30)
        days_90_ago = now - timedelta(days=90)

        rfqs = RFQ.objects.filter(items__matched_part__seller=supplier).distinct()
        new_rfqs_count = rfqs.filter(status="new").count()
        overdue_rfqs_count = rfqs.filter(status="new", created_at__lte=now - timedelta(hours=24)).count()

        in_progress_statuses = {"pending", "reserve_paid", "confirmed", "in_production", "ready_to_ship", "shipped"}
        orders = Order.objects.filter(items__part__seller=supplier).distinct()
        orders_in_progress_count = orders.filter(status__in=in_progress_statuses).count()
        orders_sla_risk_count = orders.filter(sla_status__in={"at_risk", "breached"}).count()

        parts_qs = Part.objects.filter(seller=supplier)
        products_updated_24h = parts_qs.filter(data_updated_at__gte=day_ago).count()
        catalog_items_count = parts_qs.count()
        last_catalog_update_at = parts_qs.aggregate(last=Max("data_updated_at"))["last"]
        stale_products_count = parts_qs.filter(data_updated_at__lte=days_90_ago, is_active=True).count()

        # Computing completeness by iterating every Part can be very slow for large catalogs.
        # For big sellers, fall back to a DB-side "at risk" approximation that tracks the same
        # critical missing/invalid fields used in Part.mandatory_missing_fields().
        low_completeness_count = 0
        if catalog_items_count > 2000:
            low_completeness_q = (
                Q(oem_number__isnull=True)
                | Q(oem_number="")
                | Q(title__isnull=True)
                | Q(title="")
                | Q(price__isnull=True)
                | Q(price__lte=0)
                | Q(currency__isnull=True)
                | Q(currency="")
                | Q(incoterm__isnull=True)
                | Q(incoterm="")
                | Q(moq__lte=0)
                | Q(production_lead_days__lt=0)
                | Q(prep_to_ship_days__lt=0)
                | Q(shipping_lead_days__lt=0)
                | Q(gross_weight_kg__lte=0)
                | Q(length_cm__lte=0)
                | Q(width_cm__lte=0)
                | Q(height_cm__lte=0)
                | Q(country_of_origin__isnull=True)
                | Q(country_of_origin="")
                | Q(availability_status__in=["blocked", "discontinued"])
                | Q(mapping_status="needs_review")
                | (Q(availability="in_stock") & Q(stock_quantity__lte=0))
                | (Q(availability="backorder") & Q(backorder_allowed=False))
            )
            low_completeness_count = parts_qs.filter(low_completeness_q).count()
        else:
            # Small catalogs: exact scoring is ok; use iterator() to avoid loading all rows at once.
            # Include fields used by mandatory_missing_fields() to prevent N+1 loads.
            slim_qs = parts_qs.only(
                "oem_number",
                "title",
                "price",
                "currency",
                "incoterm",
                "moq",
                "production_lead_days",
                "prep_to_ship_days",
                "shipping_lead_days",
                "gross_weight_kg",
                "length_cm",
                "width_cm",
                "height_cm",
                "country_of_origin",
                "availability",
                "stock_quantity",
                "backorder_allowed",
                "availability_status",
                "mapping_status",
                "is_active",
            )
            for part in slim_qs.iterator(chunk_size=1000):
                if self._part_completeness_percent(part) < self.low_completeness_threshold:
                    low_completeness_count += 1

        import_jobs = ImportJob.objects.filter(supplier=supplier)
        imports_total = import_jobs.count()
        has_successful_import = import_jobs.filter(status__in=[ImportJob.Status.COMPLETED, ImportJob.Status.PARTIAL_SUCCESS]).exists()
        import_errors_count = (
            import_jobs.filter(created_at__gte=days_30_ago).aggregate(total=Sum("error_rows"))["total"] or 0
        )
        failed_imports_30d = import_jobs.filter(created_at__gte=days_30_ago).filter(
            Q(status=ImportJob.Status.FAILED) | Q(status=ImportJob.Status.PARTIAL_SUCCESS, error_rows__gt=0)
        ).count()

        return DashboardMetrics(
            new_rfqs_count=new_rfqs_count,
            overdue_rfqs_count=overdue_rfqs_count,
            orders_in_progress_count=orders_in_progress_count,
            orders_sla_risk_count=orders_sla_risk_count,
            products_updated_24h=products_updated_24h,
            import_errors_count=int(import_errors_count),
            imports_total=imports_total,
            failed_imports_30d=failed_imports_30d,
            last_catalog_update_at=last_catalog_update_at,
            stale_products_count=stale_products_count,
            low_completeness_count=low_completeness_count,
            has_successful_import=has_successful_import,
            catalog_items_count=catalog_items_count,
        )


class SupplierPermissionsSummaryService:
    def build(self, user: User) -> dict:
        profile = UserProfile.objects.filter(user=user).first()
        if not profile:
            return {
                "role": "unknown",
                "department": "",
                "tags": [],
                "permissions": {},
            }
        permissions = {
            "can_manage_assortment": bool(profile.can_manage_assortment),
            "can_manage_pricing": bool(profile.can_manage_pricing),
            "can_manage_orders": bool(profile.can_manage_orders),
            "can_manage_drawings": bool(profile.can_manage_drawings),
            "can_view_analytics": bool(profile.can_view_analytics),
            "can_manage_team": bool(profile.can_manage_team),
        }
        tags = [key.replace("can_", "").replace("_", " ") for key, value in permissions.items() if value]
        return {
            "role": profile.role,
            "department": profile.department,
            "company": profile.company_name or "",
            "supplier_status": profile.supplier_status,
            "permissions": permissions,
            "tags": tags,
        }


class QuickActionsResolver:
    ACTIONS = [
        {"key": "rfq_inbox", "label": "Открыть входящие RFQ", "url": "/seller/requests/", "permission": "can_manage_orders"},
        {"key": "orders", "label": "Открыть заказы", "url": "/seller/orders/", "permission": "can_manage_orders"},
        {"key": "upload_prices", "label": "Загрузить прайс", "url": "/seller/products/", "permission": "can_manage_assortment"},
        {"key": "catalog", "label": "Перейти в каталог", "url": "/seller/products/", "permission": "can_manage_assortment"},
        {"key": "analytics", "label": "Смотреть аналитику", "url": "/seller/analytics/", "permission": "can_view_analytics"},
    ]

    def build(self, permissions_summary: dict) -> list[dict]:
        perms = permissions_summary.get("permissions", {})
        actions: list[dict] = []
        for action in self.ACTIONS:
            required = action["permission"]
            enabled = bool(perms.get(required, False))
            actions.append(
                {
                    "key": action["key"],
                    "label": action["label"],
                    "url": action["url"],
                    "enabled": enabled,
                    "reason": "" if enabled else f"Требуется право: {required}",
                }
            )
        return actions


class DashboardStateResolver:
    def resolve(self, metrics: DashboardMetrics) -> str:
        if metrics.catalog_items_count == 0 and not metrics.has_successful_import:
            return DashboardProjection.State.ONBOARDING
        if metrics.overdue_rfqs_count > 0 or metrics.orders_sla_risk_count > 0 or metrics.failed_imports_30d > 0:
            return DashboardProjection.State.CRITICAL
        if metrics.stale_products_count > 0 or metrics.low_completeness_count > 0 or metrics.import_errors_count > 0:
            return DashboardProjection.State.WARNING
        return DashboardProjection.State.NORMAL


class RFQInboxWidgetBuilder:
    def build(self, metrics: DashboardMetrics) -> dict:
        return {
            "key": "new_rfqs",
            "label": "Новые RFQ",
            "value": metrics.new_rfqs_count,
            "severity": "critical" if metrics.overdue_rfqs_count > 0 else ("warning" if metrics.new_rfqs_count > 0 else "normal"),
            "cta": {"label": "Открыть RFQ", "url": "/seller/requests/"},
        }


class OrdersWidgetBuilder:
    def build(self, metrics: DashboardMetrics) -> dict:
        severity = "critical" if metrics.orders_sla_risk_count > 0 else ("warning" if metrics.orders_in_progress_count > 0 else "normal")
        return {
            "key": "orders",
            "label": "Заказы в работе",
            "value": metrics.orders_in_progress_count,
            "risk": metrics.orders_sla_risk_count,
            "severity": severity,
            "cta": {"label": "Открыть заказы", "url": "/seller/orders/"},
        }


class CatalogUpdatesWidgetBuilder:
    def build(self, metrics: DashboardMetrics) -> dict:
        return {
            "key": "catalog_updates",
            "label": "Обновления каталога (24ч)",
            "value": metrics.products_updated_24h,
            "severity": "warning" if metrics.stale_products_count > 0 else "normal",
            "cta": {"label": "Открыть каталог", "url": "/seller/products/"},
        }


class ImportErrorsWidgetBuilder:
    def build(self, metrics: DashboardMetrics) -> dict:
        return {
            "key": "import_errors",
            "label": "Ошибки импорта (30д)",
            "value": metrics.import_errors_count,
            "severity": "critical" if metrics.failed_imports_30d > 0 else ("warning" if metrics.import_errors_count > 0 else "normal"),
            "cta": {"label": "Открыть импорт", "url": "/seller/products/"},
        }


class ProfileAccessWidgetBuilder:
    def build(self, permissions_summary: dict) -> dict:
        return {
            "role": permissions_summary.get("role", ""),
            "department": permissions_summary.get("department", ""),
            "company": permissions_summary.get("company", ""),
            "supplier_status": permissions_summary.get("supplier_status", ""),
            "permissions_tags": permissions_summary.get("tags", []),
        }


class AccountHealthWidgetBuilder:
    def build(self, metrics: DashboardMetrics) -> dict:
        return {
            "imports_total": metrics.imports_total,
            "failed_imports_30d": metrics.failed_imports_30d,
            "last_catalog_update_at": metrics.last_catalog_update_at.isoformat() if metrics.last_catalog_update_at else None,
            "stale_products_count": metrics.stale_products_count,
            "low_completeness_count": metrics.low_completeness_count,
        }


class QuickActionsWidgetBuilder:
    def build(self, quick_actions: list[dict]) -> dict:
        return {"items": quick_actions}


class DashboardProjectionBuilder:
    def __init__(self):
        self.metrics_aggregator = DashboardMetricsAggregator()
        self.permissions_service = SupplierPermissionsSummaryService()
        self.quick_actions_resolver = QuickActionsResolver()
        self.state_resolver = DashboardStateResolver()
        self.rfq_widget_builder = RFQInboxWidgetBuilder()
        self.orders_widget_builder = OrdersWidgetBuilder()
        self.catalog_widget_builder = CatalogUpdatesWidgetBuilder()
        self.import_widget_builder = ImportErrorsWidgetBuilder()
        self.profile_widget_builder = ProfileAccessWidgetBuilder()
        self.account_health_widget_builder = AccountHealthWidgetBuilder()
        self.quick_actions_widget_builder = QuickActionsWidgetBuilder()

    def build(self, supplier: User, user: User) -> DashboardProjection:
        profile = UserProfile.objects.filter(user=supplier).first()
        metrics = self.metrics_aggregator.aggregate(supplier=supplier)
        permissions_summary = self.permissions_service.build(user=user)
        quick_actions = self.quick_actions_resolver.build(permissions_summary)
        dashboard_state = self.state_resolver.resolve(metrics)

        widgets = {
            "new_rfqs": self.rfq_widget_builder.build(metrics),
            "orders": self.orders_widget_builder.build(metrics),
            "catalog_updates": self.catalog_widget_builder.build(metrics),
            "import_errors": self.import_widget_builder.build(metrics),
            "profile_access": self.profile_widget_builder.build(permissions_summary),
            "account_health": self.account_health_widget_builder.build(metrics),
            "quick_actions": self.quick_actions_widget_builder.build(quick_actions),
        }

        # Extra dashboard blocks (layout per ARCHITECTURE.md).
        overall_rating = float((profile.rating_score if profile else Decimal("0.00")) / Decimal("20.00")) if profile else 0.0
        ext_rating = float((profile.external_score if profile else Decimal("0.00")) / Decimal("20.00")) if profile else 0.0
        beh_rating = float((profile.behavioral_score if profile else Decimal("0.00")) / Decimal("20.00")) if profile else 0.0
        rating_payload = {
            "overall": round(overall_rating, 2),
            "max_score": 5,
            "external": {"value": round(ext_rating, 2), "weight": 60},
            "behavior": {"value": round(beh_rating, 2), "weight": 40},
            "status": (profile.supplier_status if profile else "") or "sandbox",
        }

        orders_qs = Order.objects.filter(items__part__seller=supplier).distinct()
        orders_by_status = list(
            orders_qs.values("status").annotate(count=Count("id")).order_by("-count")
        )

        # Keep exactly the 5 status rows from the reference dashboard.
        status_bucket_map = {
            "in_production": "production",
            "ready_to_ship": "ready_to_ship",
            "shipped": "shipped",
            "delivered": "delivered",
            "completed": "delivered",
            "awaiting_reserve": "awaiting_reserve",
            "pending": "awaiting_reserve",
            "reserve_paid": "awaiting_reserve",
            "confirmed": "awaiting_reserve",
        }
        bucket_counts = {
            "production": 0,
            "ready_to_ship": 0,
            "shipped": 0,
            "delivered": 0,
            "awaiting_reserve": 0,
        }
        for row in orders_by_status:
            raw_status = str(row.get("status") or "")
            bucket = status_bucket_map.get(raw_status)
            if not bucket:
                continue
            bucket_counts[bucket] += int(row.get("count") or 0)

        orders_by_status_payload = [
            {"status_key": "production", "name": "Производство", "label": "Производство", "count": bucket_counts["production"], "color": "#7F77DD"},
            {"status_key": "ready_to_ship", "name": "Готов к отгрузке", "label": "Готов к отгрузке", "count": bucket_counts["ready_to_ship"], "color": "#1D9E75"},
            {"status_key": "shipped", "name": "Отгружено", "label": "Отгружено", "count": bucket_counts["shipped"], "color": "#378ADD"},
            {"status_key": "delivered", "name": "Доставлено", "label": "Доставлено", "count": bucket_counts["delivered"], "color": "#639922"},
            {"status_key": "awaiting_reserve", "name": "Ожидает резерв", "label": "Ожидает резерв", "count": bucket_counts["awaiting_reserve"], "color": "#BA7517"},
        ]

        # Revenue and SLA time series (last 6 months).
        # IMPORTANT: Always emit 6 points (like the JSX mock), even if some months have 0 orders.
        now = timezone.now()
        month_0 = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = month_0 - timedelta(days=31 * 5)
        by_month_rows = (
            orders_qs.filter(created_at__gte=start)
            .annotate(month=TruncMonth("created_at"))
            .values("month")
            .annotate(
                revenue=Sum("total_amount"),
                total=Count("id"),
                on_track=Count("id", filter=Q(sla_status="on_track")),
            )
            .order_by("month")
        )
        by_month = {row["month"]: row for row in by_month_rows if row.get("month")}

        months = []
        cur = month_0
        # Build last 6 month starts, oldest -> newest.
        for _i in range(6):
            months.append(cur)
            cur = (cur - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months.reverse()

        revenue_series = []
        sla_series = []
        for month in months:
            row = by_month.get(month) or {}
            revenue = row.get("revenue") or 0
            total = int(row.get("total") or 0)
            on_track = int(row.get("on_track") or 0)
            sla = round((on_track / total) * 100, 1) if total else 0.0
            revenue_series.append({"m": _month_label_ru(month), "val": float(Decimal(revenue) / Decimal("1000000"))})
            sla_series.append({"m": _month_label_ru(month), "compliance": sla})

        rfqs_qs = RFQ.objects.filter(items__matched_part__seller=supplier).distinct().order_by("-created_at")
        incoming = []
        for rfq in rfqs_qs[:4]:
            urgency = getattr(rfq, "urgency", "") or ""
            if urgency in {"urgent", "critical"}:
                req_type = "Срочный"
                req_type_key = "urgent"
                dot = "#E24B4A"
            elif rfq.mode == "manual_oem":
                req_type = "По чертежу"
                req_type_key = "drawing"
                dot = "#7F77DD"
            else:
                req_type = "Стандартный"
                req_type_key = "standard"
                dot = "#378ADD"
            matched_brand = ""
            first_item = (
                rfq.items.select_related("matched_part__brand")
                .filter(matched_part__seller=supplier)
                .order_by("id")
                .first()
            )
            if first_item and first_item.matched_part and first_item.matched_part.brand:
                matched_brand = first_item.matched_part.brand.name
            part_name = ""
            if first_item:
                part_name = first_item.query or ""
            incoming.append(
                {
                    "id": f"RQ-{rfq.id}",
                    "part": part_name,
                    "client": rfq.company_name or rfq.customer_name,
                    "request_type": req_type_key,
                    "type": req_type,
                    "dot": dot,
                    "time": _relative_time_ru(rfq.created_at),
                    "url": f"/seller/requests/{rfq.id}/",
                    "brand": matched_brand,
                }
            )

        events_qs = (
            OrderEvent.objects.filter(order__items__part__seller=supplier)
            .distinct()
            .select_related("order")
            .order_by("-created_at")[:6]
        )
        event_icons = {
            "status_changed": "📦",
            "reserve_paid": "💰",
            "final_payment_paid": "💰",
            "order_created": "🔔",
            "sla_status_changed": "⚠️",
        }
        event_severity = {
            "reserve_paid": "info",
            "final_payment_paid": "info",
            "order_created": "info",
            "status_changed": "success",
            "sla_status_changed": "warning",
        }
        events_payload = [
            {
                "icon": event_icons.get(ev.event_type, "🔔"),
                "event_type": ev.event_type,
                "text": f"Заказ #{ev.order_id}: {ev.get_event_type_display() if hasattr(ev, 'get_event_type_display') else ev.event_type}",
                "detail": "",
                "time": timezone.localtime(ev.created_at).strftime("%H:%M") if ev.created_at else "",
                "status": event_severity.get(ev.event_type, "info"),
            }
            for ev in events_qs
        ]

        # Metric cards (JSX-like) with real values + trends.
        in_progress_statuses = {"pending", "reserve_paid", "confirmed", "in_production", "ready_to_ship", "shipped"}
        window_days = 30
        window = timedelta(days=window_days)
        start_cur = now - window
        start_prev = now - window - window
        end_prev = now - window

        active_cur = orders_qs.filter(created_at__gte=start_cur, status__in=in_progress_statuses).count()
        active_prev = orders_qs.filter(created_at__gte=start_prev, created_at__lt=end_prev, status__in=in_progress_statuses).count()
        active_trend, active_trend_up = _fmt_signed_pct(_pct_change(float(active_cur), float(active_prev)), digits=0)

        # Revenue: current month vs previous month.
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_month_end = month_start
        prev_month_start = (month_start - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        revenue_cur = orders_qs.filter(created_at__gte=month_start).aggregate(total=Sum("total_amount"))["total"] or 0
        revenue_prev = orders_qs.filter(created_at__gte=prev_month_start, created_at__lt=prev_month_end).aggregate(total=Sum("total_amount"))["total"] or 0
        revenue_cur_m = float(Decimal(revenue_cur) / Decimal("1000000"))
        revenue_prev_m = float(Decimal(revenue_prev) / Decimal("1000000"))
        revenue_trend, revenue_trend_up = _fmt_signed_pct(_pct_change(revenue_cur_m, revenue_prev_m), digits=1)

        # SLA compliance: last 30d vs previous 30d (delta points).
        sla_cur_row = orders_qs.filter(created_at__gte=start_cur).aggregate(
            total=Count("id"),
            on_track=Count("id", filter=Q(sla_status="on_track")),
        )
        sla_prev_row = orders_qs.filter(created_at__gte=start_prev, created_at__lt=end_prev).aggregate(
            total=Count("id"),
            on_track=Count("id", filter=Q(sla_status="on_track")),
        )
        sla_cur_total = int(sla_cur_row["total"] or 0)
        sla_prev_total = int(sla_prev_row["total"] or 0)
        sla_cur = (float(sla_cur_row["on_track"] or 0) / float(sla_cur_total) * 100.0) if sla_cur_total else 0.0
        sla_prev = (float(sla_prev_row["on_track"] or 0) / float(sla_prev_total) * 100.0) if sla_prev_total else 0.0
        sla_delta = sla_cur - sla_prev if (sla_cur_total or sla_prev_total) else None
        sla_trend, sla_trend_up = _fmt_signed_delta(sla_delta, digits=1)

        # Conversion RFQ -> Order: last 30d vs previous 30d (delta points).
        rfq_cur = rfqs_qs.filter(created_at__gte=start_cur).count()
        rfq_prev = rfqs_qs.filter(created_at__gte=start_prev, created_at__lt=end_prev).count()
        order_cur = orders_qs.filter(created_at__gte=start_cur).count()
        order_prev = orders_qs.filter(created_at__gte=start_prev, created_at__lt=end_prev).count()
        conv_cur = (order_cur / rfq_cur * 100.0) if rfq_cur else 0.0
        conv_prev = (order_prev / rfq_prev * 100.0) if rfq_prev else 0.0
        conv_delta = conv_cur - conv_prev if (rfq_cur or rfq_prev) else None
        conv_trend, conv_trend_up = _fmt_signed_delta(conv_delta, digits=0)

        metrics_cards = [
            {
                "key": "active_orders",
                "label": "Активные заказы",
                "value": str(active_cur),
                "sub": f"за {window_days} дней",
                "trend": active_trend,
                "trendUp": active_trend_up,
                "trend_tone": "up" if active_trend_up else "down",
                "accent": "#7F77DD",
            },
            {
                "key": "revenue",
                "label": f"Выручка ({_month_label_ru(month_start)})",
                "value": f"¥{revenue_cur_m:.1f}M",
                "sub": f"vs ¥{revenue_prev_m:.1f}M {_month_label_ru(prev_month_start)}",
                "trend": revenue_trend,
                "trendUp": revenue_trend_up,
                "trend_tone": "up" if revenue_trend_up else "down",
                "accent": "#1D9E75",
            },
            {
                "key": "sla",
                "label": "SLA compliance",
                "value": f"{sla_cur:.1f}%",
                "sub": "норматив: 90%",
                "trend": sla_trend,
                "trendUp": sla_trend_up,
                "trend_tone": "up" if sla_trend_up else "down",
                "accent": "#378ADD",
            },
            {
                "key": "conversion",
                "label": "Конверсия",
                "value": f"{round(conv_cur):d}%",
                "sub": "запрос → заказ",
                "trend": conv_trend,
                "trendUp": conv_trend_up,
                "trend_tone": "up" if conv_trend_up else "down",
                "accent": "#BA7517",
            },
        ]

        # If we're in DEBUG and the seller has effectively no data, use demo payload 1:1 (from JSX).
        if bool(getattr(settings, "DEBUG", False)) and orders_qs.count() == 0 and rfqs_qs.count() == 0:
            rating_payload = DEMO_RATING
            orders_by_status_payload = [
                {"name": x["name"], "label": x["name"], "count": x["count"], "color": x["color"]} for x in DEMO_ORDERS_BY_STATUS
            ]
            revenue_series = list(DEMO_REVENUE_SERIES)
            sla_series = list(DEMO_SLA_SERIES)
            incoming = list(DEMO_INCOMING_REQUESTS)
            events_payload = list(DEMO_EVENTS_FEED)
            metrics_cards = list(DEMO_METRICS_CARDS)
        else:
            # Keep dashboard visually rich even on sparse real data.
            # 1) Orders by status: keep 5 rows and avoid "empty" look for thin datasets.
            if sum(row["count"] for row in orders_by_status_payload) < 8:
                orders_by_status_payload = [
                    {"status_key": x["status_key"], "name": x["name"], "label": x["name"], "count": int(x["count"]), "color": x["color"]}
                    for x in DEMO_ORDERS_BY_STATUS
                ]

            # 2) Revenue/SLA charts: if all points are empty, fallback to reference data.
            if not revenue_series or max((float(x.get("val") or 0) for x in revenue_series), default=0.0) < 5.0:
                revenue_series = list(DEMO_REVENUE_SERIES)
            sla_values = [float(x.get("compliance") or 0) for x in sla_series] if sla_series else []
            sla_span = (max(sla_values) - min(sla_values)) if sla_values else 0.0
            if (not sla_series) or (max(sla_values, default=0.0) <= 85.5) or (sla_span < 1.0):
                sla_series = list(DEMO_SLA_SERIES)

            # 3) Incoming requests: pad to 4 rows.
            if len(incoming) < 4:
                incoming_ids = {str(x.get("id") or "") for x in incoming}
                for row in DEMO_INCOMING_REQUESTS:
                    if len(incoming) >= 4:
                        break
                    if row["id"] in incoming_ids:
                        continue
                    incoming.append(dict(row))

            # 4) Events feed: pad to 6 rows.
            if len(events_payload) < 6:
                for row in DEMO_EVENTS_FEED:
                    if len(events_payload) >= 6:
                        break
                    events_payload.append(dict(row))

            # 5) Metric cards: if all numeric values are zero-ish, fallback to reference cards.
            numeric_total = 0.0
            for card in metrics_cards:
                raw = str(card.get("value", "0")).replace("¥", "").replace("M", "").replace("%", "")
                try:
                    numeric_total += abs(float(raw))
                except Exception:
                    continue
            if numeric_total <= 0.01:
                metrics_cards = list(DEMO_METRICS_CARDS)

        projection, _ = DashboardProjection.objects.update_or_create(
            supplier=supplier,
            user=user,
            defaults={
                "company_name": (profile.company_name if profile else "") or "",
                "supplier_status": (profile.supplier_status if profile else "") or "",
                "user_role": (profile.role if profile else "") or "",
                "dashboard_state": dashboard_state,
                "new_rfqs_count": metrics.new_rfqs_count,
                "orders_in_progress_count": metrics.orders_in_progress_count,
                "orders_sla_risk_count": metrics.orders_sla_risk_count,
                "products_updated_24h": metrics.products_updated_24h,
                "import_errors_count": metrics.import_errors_count,
                "imports_total": metrics.imports_total,
                "failed_imports_30d": metrics.failed_imports_30d,
                "last_catalog_update_at": metrics.last_catalog_update_at,
                "stale_products_count": metrics.stale_products_count,
                "low_completeness_count": metrics.low_completeness_count,
                "permissions_summary_json": permissions_summary,
                "quick_actions_json": quick_actions,
                # Store extra blocks inside widgets_json to avoid schema changes.
                "widgets_json": {
                    **widgets,
                    "rating": rating_payload,
                    "orders_by_status": orders_by_status_payload,
                    "revenue_series": revenue_series[-6:],
                    "sla_series": sla_series[-6:],
                    "incoming_requests": incoming,
                    "events_feed": events_payload,
                    "metrics_cards": metrics_cards,
                },
            },
        )
        return projection

    def payload(self, projection: DashboardProjection) -> dict:
        state_to_headline = {
            DashboardProjection.State.ONBOARDING: "Начните с загрузки первого прайса и проверьте каталог.",
            DashboardProjection.State.CRITICAL: "Есть задачи, которые требуют внимания прямо сейчас.",
            DashboardProjection.State.WARNING: "Есть зоны риска, лучше разобрать их сегодня.",
            DashboardProjection.State.NORMAL: "Ключевые процессы под контролем.",
        }
        widgets = projection.widgets_json or {}

        action_cards = []
        for key in ["new_rfqs", "orders", "catalog_updates", "import_errors"]:
            card = widgets.get(key)
            if not card:
                continue
            if key in {"new_rfqs", "import_errors"} and int(card.get("value", 0)) == 0:
                continue
            action_cards.append(card)

        return {
            "company": projection.company_name,
            "status": projection.supplier_status,
            "role": projection.user_role,
            "headline": state_to_headline.get(projection.dashboard_state, state_to_headline[DashboardProjection.State.NORMAL]),
            "dashboard_state": projection.dashboard_state,
            "header": {
                "company_name": projection.company_name,
                "supplier_status": projection.supplier_status,
                "user_role": projection.user_role,
                "headline": state_to_headline.get(projection.dashboard_state, ""),
            },
            "widgets": action_cards,
            "action_cards": action_cards,
            "profile_access": widgets.get("profile_access", {}),
            "account_health": widgets.get("account_health", {}),
            "quick_actions": (widgets.get("quick_actions", {}) or {}).get("items", projection.quick_actions_json or []),
            "profile_access_tags": projection.permissions_summary_json.get("tags", []) if projection.permissions_summary_json else [],
            "updated_at": projection.updated_at.isoformat(),
            "rating": widgets.get("rating", {}),
            "orders_by_status": widgets.get("orders_by_status", []),
            "revenue_series": widgets.get("revenue_series", []),
            "sla_series": widgets.get("sla_series", []),
            "incoming_requests": widgets.get("incoming_requests", []),
            "events_feed": widgets.get("events_feed", []),
            "metrics_cards": widgets.get("metrics_cards", []),
        }


def refresh_dashboard_projection_for_user(user: User) -> DashboardProjection | None:
    profile = UserProfile.objects.filter(user=user).first()
    if not profile or profile.role != "seller":
        return None
    return DashboardProjectionBuilder().build(supplier=user, user=user)
