from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from imports.models import ImportJob
from marketplace.models import Category, Order, OrderItem, Part, RFQ, RFQItem, UserProfile

from .models import DashboardProjection
from .services import DashboardProjectionBuilder, DashboardStateResolver


class DashboardStateResolverTests(TestCase):
    def test_onboarding_state(self):
        from .services import DashboardMetrics

        state = DashboardStateResolver().resolve(
            DashboardMetrics(
                new_rfqs_count=0,
                overdue_rfqs_count=0,
                orders_in_progress_count=0,
                orders_sla_risk_count=0,
                products_updated_24h=0,
                import_errors_count=0,
                imports_total=0,
                failed_imports_30d=0,
                last_catalog_update_at=None,
                stale_products_count=0,
                low_completeness_count=0,
                has_successful_import=False,
                catalog_items_count=0,
            )
        )
        self.assertEqual(state, DashboardProjection.State.ONBOARDING)

    def test_critical_state(self):
        from .services import DashboardMetrics

        state = DashboardStateResolver().resolve(
            DashboardMetrics(
                new_rfqs_count=2,
                overdue_rfqs_count=1,
                orders_in_progress_count=1,
                orders_sla_risk_count=0,
                products_updated_24h=0,
                import_errors_count=0,
                imports_total=1,
                failed_imports_30d=0,
                last_catalog_update_at=None,
                stale_products_count=0,
                low_completeness_count=0,
                has_successful_import=True,
                catalog_items_count=1,
            )
        )
        self.assertEqual(state, DashboardProjection.State.CRITICAL)


class SupplierDashboardApiTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="seller_dash_api", password="pass12345")
        self.profile = UserProfile.objects.create(
            user=self.seller,
            role="seller",
            company_name="Dash Supplier",
            can_manage_assortment=True,
            can_manage_pricing=True,
            can_manage_orders=True,
            can_view_analytics=False,
        )
        self.client.login(username="seller_dash_api", password="pass12345")
        self.category = Category.objects.create(name="Dash Category", slug="dash-category")

    def test_supplier_dashboard_endpoint_returns_screen_payload(self):
        part = Part.objects.create(
            seller=self.seller,
            title="Dash Part",
            slug="dash-part",
            oem_number="DASH-001",
            description="Dashboard part",
            price="100.00",
            stock_quantity=3,
            category=self.category,
            data_updated_at=timezone.now(),
        )
        rfq = RFQ.objects.create(customer_name="Buyer", customer_email="buyer@example.com", status="new")
        RFQItem.objects.create(rfq=rfq, query="DASH-001", quantity=1, matched_part=part, state="auto_matched")
        order = Order.objects.create(
            customer_name="Order Buyer",
            customer_email="order@example.com",
            customer_phone="+1000000007",
            delivery_address="Riyadh",
            status="confirmed",
            payment_status="reserve_paid",
            sla_status="at_risk",
            total_amount="100.00",
        )
        OrderItem.objects.create(order=order, part=part, quantity=1, unit_price="100.00")
        ImportJob.objects.create(
            supplier=self.seller,
            source_type=ImportJob.SourceType.CSV,
            status=ImportJob.Status.PARTIAL_SUCCESS,
            total_rows=5,
            valid_rows=3,
            error_rows=2,
        )

        response = self.client.get("/api/v1/supplier/dashboard")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("header", body)
        self.assertIn("widgets", body)
        self.assertIn("profile_access", body)
        self.assertIn("account_health", body)
        self.assertIn("quick_actions", body)
        self.assertEqual(body["header"]["company_name"], "Dash Supplier")
        self.assertEqual(body["dashboard_state"], "critical")

    def test_quick_actions_are_filtered_by_permissions(self):
        response = self.client.get("/api/v1/supplier/dashboard")
        self.assertEqual(response.status_code, 200)
        actions = response.json()["quick_actions"]
        analytics = next((item for item in actions if item["key"] == "analytics"), None)
        self.assertIsNotNone(analytics)
        self.assertFalse(analytics["enabled"])
        self.assertIn("can_view_analytics", analytics["reason"])

    def test_projection_builder_refreshes_metrics_after_import(self):
        ImportJob.objects.create(
            supplier=self.seller,
            source_type=ImportJob.SourceType.CSV,
            status=ImportJob.Status.FAILED,
            total_rows=4,
            valid_rows=0,
            error_rows=4,
        )
        projection = DashboardProjectionBuilder().build(supplier=self.seller, user=self.seller)
        self.assertEqual(projection.imports_total, 1)
        self.assertEqual(projection.failed_imports_30d, 1)
        self.assertGreaterEqual(projection.import_errors_count, 4)
