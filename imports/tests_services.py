from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from catalog.models import Product
from files.models import StoredFile
from files.storage import read_stored_file_bytes, store_import_source_file
from marketplace.models import UserProfile
from offers.models import SupplierOffer, SupplierOfferPrice

from .models import ImportErrorReport, ImportJob, ImportRow
from .services import ImportParser, ImportRowPipeline, ImportValidator
from .tasks import process_import_job


class ImportServicesTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="seller_services", password="pass12345")
        UserProfile.objects.create(user=self.seller, role="seller")

    def _create_csv_job(self, content: bytes) -> ImportJob:
        uploaded = SimpleUploadedFile("phase3.csv", content, content_type="text/csv")
        stored = store_import_source_file(uploaded)
        stored_file = StoredFile.objects.create(
            supplier=self.seller,
            source_type=StoredFile.SourceType.IMPORT_CSV,
            storage_key=stored.storage_key,
            original_name=stored.original_name,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256,
        )
        return ImportJob.objects.create(
            supplier=self.seller,
            source_type=ImportJob.SourceType.CSV,
            source_file=stored_file,
            status=ImportJob.Status.QUEUED,
            idempotency_key=stored.checksum_sha256,
        )

    def test_parser_reads_csv_rows(self):
        job = self._create_csv_job(
            b"OEM,WarehouseAddress,Price,Brand\nABC-1,Shanghai CN,10.5,Komatsu\nXYZ-2,Shanghai CN,20.0,CAT\n"
        )
        rows = ImportParser().parse_csv_rows(job.source_file.storage_key)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 2)
        self.assertEqual(rows[0][1]["OEM"], "ABC-1")

    def test_validator_requires_oem_and_positive_price(self):
        validator = ImportValidator()
        invalid = validator.validate({"oem": "", "warehouse_address": "WH", "price": "12.3", "quantity": ""})
        self.assertFalse(invalid.is_valid)
        self.assertEqual(invalid.error_code, "missing_oem")

        invalid_warehouse = validator.validate({"oem": "ABC-1", "warehouse_address": "", "price": "12.3", "quantity": ""})
        self.assertFalse(invalid_warehouse.is_valid)
        self.assertEqual(invalid_warehouse.error_code, "missing_warehouse_address")

        invalid_price = validator.validate({"oem": "ABC-1", "warehouse_address": "WH", "price": "-1", "quantity": ""})
        self.assertFalse(invalid_price.is_valid)
        self.assertEqual(invalid_price.error_code, "invalid_price_value")

        valid = validator.validate({"oem": "ABC-1", "warehouse_address": "WH", "price": "15.90", "quantity": "3"})
        self.assertTrue(valid.is_valid)
        self.assertEqual(str(valid.parsed_price), "15.90")
        self.assertEqual(valid.parsed_quantity, 3)

    def test_pipeline_marks_rows_valid_and_invalid_and_updates_job_counters(self):
        job = self._create_csv_job(
            b"OEM,WarehouseAddress,Price,Brand,Quantity\nABC-1,Shanghai CN,10.5,Komatsu,2\n,Shanghai CN,20.0,CAT,1\nXYZ-2,Shanghai CN,0,CAT,1\n"
        )
        summary = ImportRowPipeline().process_job(job)
        self.assertEqual(summary.total_rows, 3)
        self.assertEqual(summary.valid_rows, 1)
        self.assertEqual(summary.error_rows, 2)
        self.assertEqual(summary.created_products, 1)
        self.assertEqual(summary.updated_offers, 0)

        job.refresh_from_db()
        self.assertEqual(job.total_rows, 3)
        self.assertEqual(job.valid_rows, 1)
        self.assertEqual(job.error_rows, 2)
        self.assertEqual(job.error_count, 2)
        self.assertEqual(job.status, ImportJob.Status.PARTIAL_SUCCESS)
        self.assertEqual(job.created_products, 1)
        self.assertEqual(job.updated_offers, 0)

        statuses = list(ImportRow.objects.filter(job=job).order_by("row_no").values_list("status", flat=True))
        self.assertEqual(statuses, [ImportRow.Status.UPSERTED, ImportRow.Status.INVALID, ImportRow.Status.INVALID])

    def test_pipeline_marks_ambiguous_match_as_invalid(self):
        Product.objects.create(
            oem_raw="ABC-1",
            oem_normalized="ABC-1",
            brand_raw="CAT",
            brand_normalized="CAT",
            name="Cat part",
            created_by_supplier=self.seller,
        )
        Product.objects.create(
            oem_raw="ABC-1",
            oem_normalized="ABC-1",
            brand_raw="KOMATSU",
            brand_normalized="KOMATSU",
            name="Komatsu part",
            created_by_supplier=self.seller,
        )
        job = self._create_csv_job(b"OEM,WarehouseAddress,Price\nABC-1,Shanghai CN,100\n")
        ImportRowPipeline().process_job(job)
        row = ImportRow.objects.get(job=job)
        self.assertEqual(row.status, ImportRow.Status.INVALID)
        self.assertEqual(row.error_code, "ambiguous_match")

    def test_pipeline_updates_existing_supplier_offer(self):
        product = Product.objects.create(
            oem_raw="ABC-1",
            oem_normalized="ABC-1",
            brand_raw="CAT",
            brand_normalized="CAT",
            name="Cat part",
            created_by_supplier=self.seller,
        )
        offer = SupplierOffer.objects.create(
            supplier=self.seller,
            product=product,
            price="10.00",
            quantity=1,
            status=SupplierOffer.Status.ACTIVE,
        )
        job = self._create_csv_job(b"OEM,WarehouseAddress,Price,Brand,Quantity\nABC-1,Shanghai CN,99.90,CAT,8\n")
        summary = ImportRowPipeline().process_job(job)
        self.assertEqual(summary.updated_offers, 1)
        self.assertEqual(summary.created_products, 0)
        offer.refresh_from_db()
        self.assertEqual(str(offer.price), "99.90")
        self.assertEqual(offer.quantity, 8)

    def test_pipeline_duplicate_rows_last_wins(self):
        job = self._create_csv_job(b"OEM,WarehouseAddress,Price\nDUP-1,Shanghai CN,10\nDUP-1,Shanghai CN,20\n")
        ImportRowPipeline().process_job(job)
        rows = list(ImportRow.objects.filter(job=job).order_by("row_no"))
        self.assertEqual(rows[0].status, ImportRow.Status.INVALID)
        self.assertEqual(rows[0].error_code, "duplicate_in_file")
        self.assertEqual(rows[1].status, ImportRow.Status.UPSERTED)

    def test_process_import_job_task_runs_pipeline(self):
        job = self._create_csv_job(b"OEM,WarehouseAddress,Price\nTASK-1,Shanghai CN,42.00\n")
        result = process_import_job.run(job.id)
        self.assertTrue(result["ok"])
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertEqual(job.valid_rows, 1)

    def test_process_import_job_creates_error_report_for_invalid_rows(self):
        job = self._create_csv_job(b"OEM,WarehouseAddress,Price\n,Shanghai CN,12.00\n")
        process_import_job.run(job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)
        report = ImportErrorReport.objects.filter(job=job).first()
        self.assertIsNotNone(report)
        self.assertIsNotNone(report.file_id)
        content = read_stored_file_bytes(report.file.storage_key).decode("utf-8")
        self.assertIn(
            "row_no,error_code,error_message,error_hint,input_oem,input_brand,input_cross_number,input_price,input_quantity",
            content,
        )

    def test_pipeline_accepts_supplier_sample_columns_partnumber_and_price_exw(self):
        job = self._create_csv_job(
            b"PartNumber,Brand,Name,Quantity,WarehouseAddress,Price_EXW\n561-50-82311,Komatsu,BUSHING,8,Shanghai CN,100\n"
        )
        summary = ImportRowPipeline().process_job(job)
        self.assertEqual(summary.total_rows, 1)
        self.assertEqual(summary.valid_rows, 1)
        self.assertEqual(summary.error_rows, 0)
        self.assertEqual(summary.created_products, 1)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        row = ImportRow.objects.get(job=job)
        self.assertEqual(row.normalized_oem, "561-50-82311")
        self.assertEqual(row.normalized_brand, "KOMATSU")

    def test_pipeline_creates_price_scenarios_and_matches_by_cross_number(self):
        product = Product.objects.create(
            oem_raw="BASE-1",
            oem_normalized="BASE-1",
            part_number="BASE-1",
            normalized_part_number="BASE-1",
            brand_raw="KOMATSU",
            brand_normalized="KOMATSU",
            name="Base part",
            created_by_supplier=self.seller,
        )
        from catalog.models import ProductCrossReference

        ProductCrossReference.objects.create(
            product=product,
            cross_number="ALT-1",
            normalized_cross_number="ALT-1",
        )
        job = self._create_csv_job(
            b"PartNumber,CrossNumber,Brand,Condition,WarehouseAddress,Price_EXW,Price_FOB_SEA,Price_FOB_AIR,Quantity\n"
            b"BASE-NEW,ALT-1,Komatsu,OEM,Shanghai CN,100,120,140,5\n"
        )
        summary = ImportRowPipeline().process_job(job)
        self.assertEqual(summary.error_rows, 0)
        row = ImportRow.objects.get(job=job)
        self.assertEqual(row.matched_by, "cross_number")
        offer = SupplierOffer.objects.get(id=row.supplier_offer_id)
        self.assertEqual(offer.condition, SupplierOffer.Condition.OEM)
        scenarios = {
            (p.incoterm_basis, p.transport_mode): str(p.price)
            for p in SupplierOfferPrice.objects.filter(supplier_offer=offer)
        }
        self.assertEqual(scenarios[(SupplierOfferPrice.IncotermBasis.EXW, SupplierOfferPrice.TransportMode.NONE)], "100.00")
        self.assertEqual(scenarios[(SupplierOfferPrice.IncotermBasis.FOB, SupplierOfferPrice.TransportMode.SEA)], "120.00")
        self.assertEqual(scenarios[(SupplierOfferPrice.IncotermBasis.FOB, SupplierOfferPrice.TransportMode.AIR)], "140.00")
