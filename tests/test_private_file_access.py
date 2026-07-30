import shutil
import tempfile
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from marketplace.models import (
    Category,
    CompanyVerification,
    Notification,
    Order,
    OrderDocument,
    OrderItem,
    Part,
    PricelistImport,
    UserProfile,
    UserRole,
)


class PrivateFileAccessTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="private-media-tests-")
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media_root,
            SERVE_MEDIA=True,
            ENABLE_VIRUS_SCAN=False,
        )
        self.settings_override.enable()
        self.owner = User.objects.create_user(
            username="private_file_owner",
            password="pass12345",
        )
        self.other = User.objects.create_user(
            username="private_file_other",
            password="pass12345",
        )
        self.other_seller = User.objects.create_user(
            username="private_file_other_seller",
            password="pass12345",
        )
        UserProfile.objects.create(user=self.owner, role="seller")
        UserProfile.objects.create(user=self.other, role="buyer")
        UserProfile.objects.create(user=self.other_seller, role="seller")

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_kyb_document_requires_owner_and_raw_media_is_blocked(self):
        verification = CompanyVerification.objects.create(user=self.owner)
        verification.doc_charter.save(
            "charter.pdf",
            ContentFile(b"%PDF-1.4\nprivate"),
            save=True,
        )
        protected_url = (
            f"/api/assistant/kyb/{self.owner.id}/doc/charter/file/"
        )

        self.client.force_login(self.owner)
        owner_response = self.client.get(protected_url)
        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response["Cache-Control"], "private, no-store")

        raw_response = self.client.get(verification.doc_charter.url)
        self.assertEqual(raw_response.status_code, 404)

        self.client.force_login(self.other)
        other_response = self.client.get(protected_url)
        self.assertEqual(other_response.status_code, 403)

    def test_pricelist_output_requires_owner_and_uses_protected_url(self):
        pricelist = PricelistImport.objects.create(
            seller=self.owner,
            filename="source.csv",
        )
        pricelist.output_file.save(
            "result.xlsx",
            ContentFile(b"private output"),
            save=True,
        )
        protected_url = (
            f"/api/assistant/upload-pricelist/{pricelist.id}/output-file/"
        )

        self.client.force_login(self.owner)
        owner_response = self.client.get(protected_url)
        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.get(pricelist.output_file.url).status_code, 404)

        self.client.force_login(self.other)
        self.assertEqual(self.client.get(protected_url).status_code, 403)

        self.client.force_login(self.other_seller)
        self.assertEqual(self.client.get(protected_url).status_code, 404)

    def test_pricelist_fallback_preview_escapes_cell_html(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Name"])
        sheet.append(['<img src=x onerror="alert(1)">'])
        output = BytesIO()
        workbook.save(output)
        workbook.close()

        pricelist = PricelistImport.objects.create(
            seller=self.owner,
            filename="source.csv",
        )
        pricelist.output_file.save(
            "result.xlsx",
            ContentFile(output.getvalue()),
            save=True,
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            f"/api/assistant/upload-pricelist/{pricelist.id}/output-preview/"
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;img src=x", body)
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])

    def test_seller_cannot_read_another_seller_document_in_shared_order(self):
        category = Category.objects.create(
            name="Shared order category",
            slug="shared-order-category",
        )
        owner_part = Part.objects.create(
            seller=self.owner,
            category=category,
            title="Owner item",
            slug="private-owner-item",
            oem_number="PRIVATE-OWNER",
            price="100.00",
        )
        other_part = Part.objects.create(
            seller=self.other_seller,
            category=category,
            title="Other seller item",
            slug="private-other-item",
            oem_number="PRIVATE-OTHER",
            price="200.00",
        )
        order = Order.objects.create(
            buyer=self.other,
            customer_name="Shared buyer",
            customer_email="shared@example.com",
            customer_phone="+70000000000",
            delivery_address="Test address",
        )
        OrderItem.objects.create(
            order=order,
            part=owner_part,
            quantity=1,
            unit_price="100.00",
        )
        OrderItem.objects.create(
            order=order,
            part=other_part,
            quantity=1,
            unit_price="200.00",
        )
        document = OrderDocument.objects.create(
            order=order,
            doc_type="invoice",
            title="Other seller private invoice",
            uploaded_by=self.other_seller,
        )
        document.file_obj.save(
            "other-seller-invoice.pdf",
            ContentFile(b"%PDF-1.4\nprivate seller document"),
            save=True,
        )
        protected_url = (
            f"/api/assistant/orders/{order.id}/documents/{document.id}/file/"
        )

        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(protected_url).status_code, 403)

        self.client.force_login(self.other_seller)
        self.assertEqual(self.client.get(protected_url).status_code, 200)

        self.client.force_login(self.other)
        self.assertEqual(self.client.get(protected_url).status_code, 200)

    def test_legacy_kyb_form_rejects_executable_disguised_as_pdf(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            "/kyb/",
            data={
                "legal_name": "Test Company",
                "doc_charter": SimpleUploadedFile(
                    "charter.pdf",
                    b"MZ\x90\x00malicious",
                    content_type="application/pdf",
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        verification = CompanyVerification.objects.filter(user=self.owner).first()
        self.assertTrue(
            verification is None or not bool(verification.doc_charter)
        )

    def test_notification_read_endpoint_rejects_get(self):
        self.client.force_login(self.owner)
        response = self.client.get("/api/notifications/read/")
        self.assertEqual(response.status_code, 405)

    def test_notification_targets_are_limited_to_local_paths(self):
        Notification.objects.create(
            user=self.owner,
            kind="system",
            title="<img src=x onerror=alert(1)>",
            body="<script>alert(2)</script>",
            url="javascript:alert(3)",
        )
        self.client.force_login(self.owner)

        api_response = self.client.get("/api/notifications/")
        self.assertEqual(api_response.status_code, 200)
        self.assertEqual(api_response.json()["items"][0]["url"], "")
        assistant_response = self.client.get("/api/assistant/notifications/")
        self.assertEqual(assistant_response.status_code, 200)
        self.assertEqual(assistant_response.json()["items"][0]["url"], "")

        from assistant.views import _safe_local_url
        from marketplace.views import _safe_local_notification_url

        self.assertEqual(_safe_local_notification_url("//evil.example/path"), "")
        self.assertEqual(_safe_local_url("javascript:alert(1)"), "")
        self.assertEqual(
            _safe_local_notification_url("/chat/?conv=123"),
            "/chat/?conv=123",
        )

    def test_buyer_cannot_call_seller_pricelist_upload_directly(self):
        self.client.force_login(self.other)
        response = self.client.post("/api/assistant/upload-pricelist/")
        self.assertEqual(response.status_code, 403)

    def test_project_api_rejects_unbounded_or_structured_fields(self):
        self.client.force_login(self.owner)
        too_many_tags = self.client.post(
            "/api/assistant/projects/",
            data=json.dumps({
                "name": "Security project",
                "tags": [f"tag-{index}" for index in range(21)],
            }),
            content_type="application/json",
        )
        self.assertEqual(too_many_tags.status_code, 400)

        structured_name = self.client.post(
            "/api/assistant/projects/",
            data=json.dumps({"name": {"nested": "value"}}),
            content_type="application/json",
        )
        self.assertEqual(structured_name.status_code, 400)

        valid = self.client.post(
            "/api/assistant/projects/",
            data=json.dumps({
                "name": "Security project",
                "tags": ["audit", "audit", " access "],
            }),
            content_type="application/json",
        )
        self.assertEqual(valid.status_code, 201)
        project_id = valid.json()["id"]

        oversized_update = self.client.patch(
            f"/api/assistant/projects/{project_id}/update/",
            data=json.dumps({"description": "x" * 5_001}),
            content_type="application/json",
        )
        self.assertEqual(oversized_update.status_code, 400)

    def test_action_api_rejects_unstructured_or_oversized_commands(self):
        structured_params = self.client.post(
            "/api/assistant/action/",
            data=json.dumps({
                "action": "search_parts",
                "params": ["not", "an", "object"],
            }),
            content_type="application/json",
        )
        self.assertEqual(structured_params.status_code, 400)

        oversized_action = self.client.post(
            "/api/assistant/action/",
            data=json.dumps({"action": "x" * 129, "params": {}}),
            content_type="application/json",
        )
        self.assertEqual(oversized_action.status_code, 400)

    def test_secondary_seller_role_is_used_by_dashboard_and_context(self):
        UserRole.objects.create(
            user=self.other,
            role="seller",
            is_enabled=True,
        )
        profile = self.other.profile
        profile.can_manage_assortment = True
        profile.save(update_fields=["can_manage_assortment"])

        self.client.force_login(self.other)
        session = self.client.session
        session["assistant_role_override"] = "seller"
        session.save()

        projection = SimpleNamespace(updated_at=timezone.now())
        with (
            patch(
                "dashboard.api.DashboardProjectionBuilder.build",
                return_value=projection,
            ),
            patch(
                "dashboard.api.DashboardProjectionBuilder.payload",
                return_value={"role": "seller"},
            ),
        ):
            response = self.client.get("/api/v1/supplier/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "seller")

    def test_order_documents_reject_external_links_and_hide_legacy_urls(self):
        category = Category.objects.create(
            name="Private document category",
            slug="private-document-category",
        )
        part = Part.objects.create(
            category=category,
            seller=self.owner,
            title="Private document part",
            slug="private-document-part",
            oem_number="PRIVATE-DOC-1",
            price="10.00",
            stock_quantity=1,
        )
        order = Order.objects.create(
            buyer=self.other,
            customer_name="Private document buyer",
            customer_email="buyer@example.com",
            customer_phone="+70000000000",
            delivery_address="Private address",
        )
        OrderItem.objects.create(
            order=order,
            part=part,
            quantity=1,
            unit_price="10.00",
        )

        self.client.force_login(self.other)
        response = self.client.post(
            f"/orders/{order.id}/documents/add/",
            data={
                "doc_type": "invoice",
                "title": "External invoice",
                "file_url": "https://files.example/private-invoice.pdf",
            },
        )
        self.assertEqual(response.status_code, 410)
        self.assertFalse(order.documents.exists())

        legacy = OrderDocument.objects.create(
            order=order,
            doc_type="invoice",
            title="Legacy external invoice",
            file_url="https://files.example/legacy-invoice.pdf",
            uploaded_by=self.owner,
        )
        from assistant.documents import _doc_url

        self.assertEqual(_doc_url(legacy), "")

        self.client.force_login(self.owner)
        api_response = self.client.get(f"/api/v1/seller/orders/{order.id}/")
        self.assertEqual(api_response.status_code, 200)
        self.assertEqual(api_response.json()["documents"][0]["file_url"], "")
