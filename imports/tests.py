import json

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from files.models import StoredFile
from files.storage import store_generated_file_bytes
from marketplace.models import UserProfile

from catalog.models import Product
from offers.models import SupplierOffer

from .models import ImportErrorReport, ImportJob, ImportRow


class SupplierImportApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="seller_import_api", password="pass12345")
        UserProfile.objects.create(user=self.user, role="seller")
        self.client.login(username="seller_import_api", password="pass12345")

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

    def test_google_sheet_invalid_url_returns_400(self):
        response = self.client.post(
            reverse("supplier_import_google_sheet"),
            data={"url": "https://example.com/not-sheet"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ImportJob.objects.count(), 0)

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
