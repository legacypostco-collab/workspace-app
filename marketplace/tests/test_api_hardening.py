import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from marketplace.models import Category, Part, UserProfile


class ApiHardeningTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="api_user", password="test12345")
        self.seller = User.objects.create_user(username="seller_user", password="test12345")
        seller_profile, _ = UserProfile.objects.get_or_create(user=self.seller)
        seller_profile.role = "seller"
        seller_profile.can_manage_assortment = True
        seller_profile.can_manage_pricing = True
        seller_profile.save()

        self.category = Category.objects.create(name="Engine", slug="engine")
        self.part = Part.objects.create(
            seller=self.seller,
            category=self.category,
            title="Main Switch",
            slug="main-switch",
            oem_number="RE48786",
            price=Decimal("295.00"),
            stock_quantity=10,
            condition="oem",
            is_active=True,
        )

    def test_quote_preview_valid_and_qty_sum(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/v1/quote/preview/",
            data=json.dumps({"items": [{"part_id": self.part.id, "qty": 3}]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["items"][0]["line_total"], "885.00")
        self.assertEqual(payload["total"], "885.00")

    def test_quote_preview_invalid_payload(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/v1/quote/preview/",
            data=json.dumps({"items": [{"part_id": self.part.id, "qty": 0}]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(MAX_QUOTE_ITEMS=1)
    def test_quote_preview_limit(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/v1/quote/preview/",
            data=json.dumps(
                {
                    "items": [
                        {"part_id": self.part.id, "qty": 1},
                        {"part_id": self.part.id, "qty": 1},
                    ]
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)

    def test_update_template_invalid_payload_returns_400(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/v1/template/update/",
            data=json.dumps({"template": "invalid-one"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(MAX_IMPORT_FILE_BYTES=100)
    def test_import_oversize_returns_413(self):
        self.client.force_login(self.seller)
        csv_data = ("Part Number,Description,Unitprice\n" + ("A,desc,1.0\n" * 100)).encode("utf-8")
        file = SimpleUploadedFile("import.csv", csv_data, content_type="text/csv")
        response = self.client.post(
            "/seller/upload/",
            data={"file": file, "category": "Epiroc", "default_stock": 1, "import_mode": "preview"},
        )
        self.assertEqual(response.status_code, 413)

    @override_settings(MAX_IMPORT_ROWS=1)
    def test_import_too_many_rows_returns_413(self):
        self.client.force_login(self.seller)
        csv_data = "Part Number,Description,Unitprice\nA,desc,1.0\nB,desc,1.5\n".encode("utf-8")
        file = SimpleUploadedFile("import.csv", csv_data, content_type="text/csv")
        response = self.client.post(
            "/seller/upload/",
            data={"file": file, "category": "Epiroc", "default_stock": 1, "import_mode": "preview"},
        )
        self.assertEqual(response.status_code, 413)
