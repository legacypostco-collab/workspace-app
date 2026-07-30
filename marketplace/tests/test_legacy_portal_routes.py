from django.contrib.auth.models import User
from django.test import TestCase

from assistant.actions import execute
from marketplace.models import Brand, Category, Part, UserProfile


class RemovedLegacyPortalRoutesTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("legacy_route_buyer")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.client.force_login(self.buyer)

    def test_old_role_portals_are_gone_for_authenticated_users(self):
        for url in (
            "/buyer/",
            "/buyer/orders/",
            "/seller/",
            "/seller/products/",
            "/operator/",
            "/operator/logist/",
            "/operator/payments/escrow/",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response["Cache-Control"], "no-store")

    def test_chat_workspace_remains_available(self):
        response = self.client.get("/chat/?workspace=1")
        self.assertEqual(response.status_code, 200)

    def test_old_admin_panel_is_gone_for_every_role(self):
        for user in (
            self.buyer,
            User.objects.create_superuser(
                "legacy_route_admin",
                "legacy-admin@example.com",
                "strong-test-password",
            ),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get("/admin-panel/orders/42/")
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response["Cache-Control"], "no-store")

    def test_public_directories_preserve_the_requested_action(self):
        cases = {
            "/catalog/?q=ABC-123": (
                "run=search_parts",
                "query=ABC-123",
            ),
            "/directory/brands/?q=cat": (
                "run=browse_brands",
                "query=cat",
            ),
            "/directory/categories/": ("run=browse_categories",),
            "/directory/suppliers/": ("run=top_suppliers",),
            "/orders/42/": ("run=get_order_detail", "order_id=42"),
            "/orders/42/invoice/": (
                "run=list_order_documents",
                "order_id=42",
            ),
        }
        for url, expected_parts in cases.items():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/chat/?"))
                for part in expected_parts:
                    self.assertIn(part, response.url)

    def test_removed_order_mutations_cannot_be_replayed(self):
        response = self.client.post("/orders/42/reserve-paid/")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_brand_and_category_actions_open_filtered_catalog(self):
        brand = Brand.objects.create(name="Audit Brand", slug="audit-brand")
        category = Category.objects.create(
            name="Audit Category",
            slug="audit-category",
        )
        Part.objects.create(
            title="Audit part",
            slug="audit-part",
            oem_number="AUDIT-1",
            price="100.00",
            stock_quantity=1,
            brand=brand,
            category=category,
        )

        brand_result = execute("browse_brands", {}, self.buyer, "buyer")
        category_result = execute("browse_categories", {}, self.buyer, "buyer")

        brand_row = brand_result.cards[0]["data"]["items"][0]
        category_row = category_result.cards[0]["data"]["items"][0]
        self.assertEqual(brand_row["action"], "search_parts")
        self.assertEqual(brand_row["params"], {"brand": brand.name})
        self.assertEqual(category_row["action"], "search_parts")
        self.assertEqual(category_row["params"], {"category": category.name})
