import json

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

from files.models import StoredFile
from files.storage import store_generated_file_bytes
from marketplace.models import UserProfile, UserRole

from catalog.models import Product
from offers.models import SupplierOffer

from .models import ImportErrorReport, ImportJob, ImportPreviewSession, ImportRow


class SupplierImportApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="seller_import_api", password="pass12345")
        UserProfile.objects.create(user=self.user, role="seller")
        self.client.force_login(self.user)

    def test_upload_csv_creates_stored_file_and_preview_session(self):
        file_obj = SimpleUploadedFile(
            "prices.csv",
            b"PartNumber,WarehouseAddress,Price_FOB_SEA\nABC-123,Shanghai CN,100.00\n",
            content_type="text/csv",
        )
        response = self.client.post(reverse("supplier_import_file"), data={"file": file_obj})
        self.assertEqual(response.status_code, 201)
        self.assertIn("preview_id", response.json())
        self.assertEqual(response.json()["status"], "draft")
        self.assertIn("detected_columns", response.json())
        self.assertIn("sample_rows", response.json())

        self.assertEqual(StoredFile.objects.count(), 1)
        self.assertEqual(ImportJob.objects.count(), 0)

    @patch("imports.api.ImportParser.build_preview_from_bytes")
    def test_upload_read_error_does_not_expose_internal_path(self, mock_preview):
        mock_preview.side_effect = OSError("/private/data/imports/secret.csv")
        file_obj = SimpleUploadedFile(
            "prices.csv",
            b"PartNumber,Price\nABC-123,100.00\n",
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("supplier_import_file"),
            data={"file": file_obj},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"],
            "Не удалось безопасно прочитать файл.",
        )
        self.assertNotIn("/private/", response.content.decode())
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_google_sheet_invalid_url_returns_400(self):
        response = self.client.post(
            reverse("supplier_import_google_sheet"),
            data={"url": "https://example.com/not-sheet"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ImportJob.objects.count(), 0)

    def test_google_sheet_lookalike_host_is_rejected(self):
        response = self.client.post(
            reverse("supplier_import_google_sheet"),
            data={
                "url": (
                    "https://docs.google.com.evil.example/"
                    "spreadsheets/d/abc123/edit"
                ),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(ImportJob.objects.count(), 0)

    @patch("imports.google_sheets.download_get_with_checked_redirects")
    def test_google_sheet_download_creates_real_preview_and_source_file(self, mock_download):
        mock_download.return_value = (
            200,
            b"PartNumber,WarehouseAddress,Price_FOB_SEA\nABC-123,Shanghai CN,100.00\n",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=7",
        )

        response = self.client.post(
            reverse("supplier_import_google_sheet"),
            data={
                "url": "https://docs.google.com/spreadsheets/d/abc123/edit#gid=7",
            },
        )

        self.assertEqual(response.status_code, 201)
        preview = ImportPreviewSession.objects.get(id=response.json()["preview_id"])
        self.assertEqual(preview.source_type, ImportPreviewSession.SourceType.GOOGLE_SHEET)
        self.assertIsNotNone(preview.source_file)
        self.assertEqual(
            preview.source_file.source_type,
            StoredFile.SourceType.IMPORT_GOOGLE_SHEET,
        )
        self.assertEqual(preview.sample_rows[0]["PartNumber"], "ABC-123")
        self.assertEqual(preview.detected_columns["oem"], "PartNumber")
        requested_url = mock_download.call_args.args[0]
        self.assertIn("format=csv", requested_url)
        self.assertIn("gid=7", requested_url)

    @patch("imports.google_sheets.download_get_with_checked_redirects")
    def test_google_sheet_download_failure_creates_no_preview(self, mock_download):
        mock_download.return_value = (
            403,
            b"",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv",
        )

        response = self.client.post(
            reverse("supplier_import_google_sheet"),
            data={"url": "https://docs.google.com/spreadsheets/d/abc123/edit"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(ImportPreviewSession.objects.count(), 0)
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_csv_upload_rejects_executable_content(self):
        file_obj = SimpleUploadedFile(
            "prices.csv",
            b"MZ\x90\x00malicious",
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("supplier_import_file"),
            data={"file": file_obj},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_csv_upload_rejects_duplicate_headers_without_storing_file(self):
        file_obj = SimpleUploadedFile(
            "prices.csv",
            b"PartNumber,PartNumber,Price_FOB_SEA\nABC-123,ABC-124,100.00\n",
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("supplier_import_file"),
            data={"file": file_obj},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(StoredFile.objects.count(), 0)

    @override_settings(MAX_IMPORT_ROWS=1)
    def test_csv_upload_enforces_row_limit_before_storing_file(self):
        file_obj = SimpleUploadedFile(
            "prices.csv",
            (
                b"PartNumber,WarehouseAddress,Price_FOB_SEA\n"
                b"ABC-123,Shanghai CN,100.00\n"
                b"ABC-124,Shanghai CN,101.00\n"
            ),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("supplier_import_file"),
            data={"file": file_obj},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_strict_import_rejects_buyer_role(self):
        buyer = User.objects.create_user(username="strict_buyer", password="pass12345")
        UserProfile.objects.create(user=buyer, role="buyer")
        self.client.force_login(buyer)
        upload = SimpleUploadedFile(
            "prices.csv",
            b"PartNumber\nABC-123\n",
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("seller_strict_import"),
            data={"file": upload},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_strict_import_rejects_legacy_xls_before_storage(self):
        upload = SimpleUploadedFile(
            "prices.xls",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy",
            content_type="application/vnd.ms-excel",
        )

        response = self.client.post(
            reverse("seller_strict_import"),
            data={"file": upload},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_strict_import_invalid_structure_leaves_no_stored_file(self):
        upload = SimpleUploadedFile(
            "prices.csv",
            b"PartNumber,Price_EXW\nABC-123,100.00\n",
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("seller_strict_import"),
            data={"file": upload},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_enabled_secondary_seller_role_can_use_import_api(self):
        multi_role_user = User.objects.create_user(
            username="buyer_with_seller_role",
            password="pass12345",
        )
        UserProfile.objects.create(user=multi_role_user, role="buyer")
        UserRole.objects.create(
            user=multi_role_user,
            role="seller",
            is_enabled=True,
        )
        self.client.force_login(multi_role_user)
        session = self.client.session
        session["assistant_role_override"] = "seller"
        session.save()
        upload = SimpleUploadedFile(
            "prices.csv",
            b"PartNumber,WarehouseAddress,Price_FOB_SEA\nABC-123,Shanghai CN,100.00\n",
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("supplier_import_file"),
            data={"file": upload},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            ImportPreviewSession.objects.get().supplier,
            multi_role_user,
        )

    @patch("imports.api.process_import_job.delay")
    def test_confirm_mapping_and_start_creates_job(self, mock_delay):
        upload = SimpleUploadedFile(
            "prices.csv",
            b"PartNumber,WarehouseAddress,Price_FOB_SEA\nABC-123,Shanghai CN,100.00\n",
            content_type="text/csv",
        )
        preview_response = self.client.post(reverse("supplier_import_file"), data={"file": upload})
        self.assertEqual(preview_response.status_code, 201)
        preview_id = preview_response.json()["preview_id"]

        confirm_response = self.client.post(
            reverse("supplier_import_preview_confirm_mapping", args=[preview_id]),
            data=json.dumps(
                {
                    "mapping": {
                        "oem": "PartNumber",
                        "warehouse_address": "WarehouseAddress",
                        "price_fob_sea": "Price_FOB_SEA",
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(confirm_response.status_code, 200)
        self.assertEqual(confirm_response.json()["status"], "mapping_confirmed")

        start_response = self.client.post(
            reverse("supplier_import_start"),
            data=json.dumps({"preview_id": preview_id}),
            content_type="application/json",
        )
        self.assertEqual(start_response.status_code, 201)
        self.assertIn("job_id", start_response.json())
        job = ImportJob.objects.get(id=start_response.json()["job_id"])
        self.assertEqual(job.status, ImportJob.Status.QUEUED)
        self.assertEqual(job.column_mapping_json.get("oem"), "PartNumber")
        mock_delay.assert_called_once_with(job.id)

    @patch("imports.api.process_import_job.delay")
    def test_import_list_and_detail_endpoints(self, _mock_delay):
        job = ImportJob.objects.create(
            supplier=self.user,
            source_type=ImportJob.SourceType.GOOGLE_SHEET,
            source_url="https://docs.google.com/spreadsheets/d/abc123/edit#gid=0",
            status=ImportJob.Status.QUEUED,
            total_rows=10,
            valid_rows=7,
            error_rows=3,
            created_products=2,
            updated_offers=5,
        )

        list_response = self.client.get(reverse("supplier_import_list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()["items"]), 1)

        detail_response = self.client.get(reverse("supplier_import_detail", args=[job.id]))
        self.assertEqual(detail_response.status_code, 200)
        body = detail_response.json()
        self.assertEqual(body["id"], job.id)
        self.assertEqual(body["status"], ImportJob.Status.QUEUED)
        self.assertIsNone(body["error_report_url"])

    @patch("imports.api.process_import_job.delay")
    def test_errors_download_endpoint_returns_csv(self, _mock_delay):
        generated = store_generated_file_bytes(
            content=b"row_no,error_code\n2,missing_oem\n",
            original_name="import_errors_1.csv",
            content_type="text/csv",
        )
        stored_file = StoredFile.objects.create(
            supplier=self.user,
            source_type=StoredFile.SourceType.IMPORT_ERROR_REPORT,
            storage_key=generated.storage_key,
            original_name=generated.original_name,
            content_type=generated.content_type,
            size_bytes=generated.size_bytes,
            checksum_sha256=generated.checksum_sha256,
        )
        job = ImportJob.objects.create(
            supplier=self.user,
            source_type=ImportJob.SourceType.CSV,
            status=ImportJob.Status.PARTIAL_SUCCESS,
            total_rows=1,
            valid_rows=0,
            error_rows=1,
        )
        ImportErrorReport.objects.create(job=job, file=stored_file, error_count=1)

        response = self.client.get(reverse("supplier_import_errors_download", args=[job.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("row_no,error_code", response.content.decode("utf-8"))

    def test_import_progress_and_rows_endpoints(self):
        job = ImportJob.objects.create(
            supplier=self.user,
            source_type=ImportJob.SourceType.CSV,
            status=ImportJob.Status.PROCESSING,
            total_rows=10,
            processed_rows=6,
            valid_rows=4,
            error_rows=2,
        )
        ImportRow.objects.create(
            job=job,
            row_no=1,
            status=ImportRow.Status.INVALID,
            validation_status=ImportRow.ValidationStatus.INVALID,
            match_status=ImportRow.MatchStatus.FAILED,
            part_number_raw="BAD-1",
            error_code="missing_required_field",
            error_message="Missing part number",
        )

        progress_response = self.client.get(reverse("supplier_import_progress", args=[job.id]))
        self.assertEqual(progress_response.status_code, 200)
        self.assertEqual(progress_response.json()["processed_rows"], 6)
        self.assertEqual(progress_response.json()["progress_percent"], 60)

        rows_response = self.client.get(reverse("supplier_import_rows", args=[job.id]))
        self.assertEqual(rows_response.status_code, 200)
        self.assertEqual(len(rows_response.json()["items"]), 1)
        self.assertEqual(rows_response.json()["items"][0]["error_code"], "missing_required_field")

    def test_import_rollback_endpoint_deactivates_offers(self):
        product = Product.objects.create(
            part_number="ROLL-001",
            normalized_part_number="ROLL-001",
            name="Rollback Product",
        )
        offer = SupplierOffer.objects.create(
            supplier=self.user,
            product=product,
            condition=SupplierOffer.Condition.OEM,
            price="99.00",
            warehouse_address="Riyadh",
        )
        job = ImportJob.objects.create(
            supplier=self.user,
            source_type=ImportJob.SourceType.CSV,
            status=ImportJob.Status.COMPLETED,
            total_rows=1,
            valid_rows=1,
            error_rows=0,
        )
        ImportRow.objects.create(
            job=job,
            row_no=1,
            status=ImportRow.Status.UPSERTED,
            validation_status=ImportRow.ValidationStatus.VALID,
            match_status=ImportRow.MatchStatus.MATCHED,
            part_number_raw="ROLL-001",
            supplier_offer=offer,
            matched_supplier_offer=offer,
        )

        response = self.client.post(reverse("supplier_import_rollback", args=[job.id]))
        self.assertEqual(response.status_code, 200)
        offer.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(offer.status, SupplierOffer.Status.INACTIVE)
        self.assertTrue(offer.is_hidden)
        self.assertEqual(job.summary_json["rollback"]["rolled_back_offer_count"], 1)
