from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from marketplace.models import Part, UserProfile
from marketplace.services.imports import UploadLimitError, process_seller_csv_upload


class ImportServiceTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="svc_seller", password="test12345")
        profile, _ = UserProfile.objects.get_or_create(user=self.seller)
        profile.role = "seller"
        profile.save()

    def test_process_upload_preview(self):
        data = "Part Number,Description,Unitprice\nRE1,MAIN SWITCH,295.00\n"
        upload = SimpleUploadedFile("parts.csv", data.encode("utf-8"), content_type="text/csv")
        result = process_seller_csv_upload(
            seller=self.seller,
            upload=upload,
            category_name="Epiroc",
            default_stock=10,
            import_mode="preview",
        )
        self.assertEqual(result.created, 1)
        self.assertEqual(result.updated, 0)

    def test_process_upload_apply_uses_qty_and_price(self):
        data = "Part Number,Description,Unitprice,Stock\nRE1,MAIN SWITCH,295.00,7\n"
        upload = SimpleUploadedFile("parts.csv", data.encode("utf-8"), content_type="text/csv")
        result = process_seller_csv_upload(
            seller=self.seller,
            upload=upload,
            category_name="Epiroc",
            default_stock=10,
            import_mode="apply",
        )
        self.assertEqual(result.created, 1)
        part = Part.objects.get(oem_number="RE1")
        self.assertEqual(part.stock_quantity, 7)
        self.assertEqual(part.price, Decimal("295.00"))

    @override_settings(MAX_IMPORT_ROWS=1)
    def test_process_upload_limits_rows(self):
        data = "Part Number,Description,Unitprice\nRE1,MAIN SWITCH,295.00\nRE2,SENSOR,120.00\n"
        upload = SimpleUploadedFile("parts.csv", data.encode("utf-8"), content_type="text/csv")
        with self.assertRaises(UploadLimitError):
            process_seller_csv_upload(
                seller=self.seller,
                upload=upload,
                category_name="Epiroc",
                default_stock=10,
                import_mode="preview",
            )
