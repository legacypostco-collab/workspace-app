from datetime import timedelta

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from imports.models import ImportPreviewSession
from marketplace.models import Category, Order, OrderItem, Part, RFQ, RFQItem, SellerImportRun, UserProfile
from marketplace.services.imports import process_seller_csv_upload
from marketplace.views import seller_order_detail, seller_orders, seller_product_detail, seller_request_detail, seller_request_list, seller_rfq_inbox
from projections.models import DashboardProjection


class SellerPortalSmokeTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username="seller_smoke",
            email="seller_smoke@example.com",
            password="pass12345",
        )
        UserProfile.objects.create(user=self.seller, role="seller", company_name="Smoke Supplier")
        self.client.login(username="seller_smoke", password="pass12345")
        self.factory = RequestFactory()

    def test_seller_csv_template_download(self):
        response = self.client.get(reverse("seller_csv_template"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("PartNumber", response.content.decode("utf-8"))

    def test_seller_price_export_download(self):
        response = self.client.get(reverse("seller_price_export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("Unitprice", response.content.decode("utf-8"))

    def test_seller_bulk_upload_preview_creates_import_run(self):
        csv_payload = (
            "PartNumber,Name,WarehouseAddress,Price_FOB_SEA,Quantity\n"
            "TEST-001,Smoke part,Shanghai CN,123.45,5\n"
        )
        upload = SimpleUploadedFile(
            "smoke_upload.csv",
            csv_payload.encode("utf-8"),
            content_type="text/csv",
        )
        response = self.client.post(
            reverse("seller_bulk_upload"),
            data={
                "file": upload,
                "category": "Epiroc",
                "default_stock": 20,
                "import_mode": "preview",
            },
        )
        self.assertEqual(response.status_code, 302)
        preview = ImportPreviewSession.objects.filter(supplier=self.seller).order_by("-id").first()
        self.assertIsNotNone(preview)
        self.assertEqual(response["Location"], f"{reverse('seller_product_list')}?preview_id={preview.id}")
        projection = DashboardProjection.objects.filter(supplier=self.seller).first()
        self.assertIsNone(projection)

    def test_seller_import_errors_csv(self):
        run = SellerImportRun.objects.create(
            seller=self.seller,
            filename="broken.csv",
            mode="preview",
            status="failed",
            error_count=1,
            skipped_invalid_count=1,
            errors=[{"row": 3, "error_type": "missing_required_field", "reason": "Пустой Part Number", "hint": "Заполните PartNumber"}],
        )
        response = self.client.get(reverse("seller_import_errors_csv", args=[run.id]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("row_number,original_data,error_type,error_message,fix_suggestion", body)
        self.assertIn("Пустой Part Number", body)

    def test_import_updates_part_data_timestamp(self):
        category = Category.objects.create(name="Smoke", slug="smoke")
        part = Part.objects.create(
            seller=self.seller,
            title="Old part",
            slug="old-part",
            oem_number="TEST-001",
            description="Old description",
            price="10.00",
            stock_quantity=1,
            category=category,
        )
        old_timestamp = timezone.now() - timedelta(days=3)
        Part.objects.filter(id=part.id).update(data_updated_at=old_timestamp)

        upload = SimpleUploadedFile(
            "update.csv",
            b"PartNumber,Name,WarehouseAddress,Price_FOB_SEA,Quantity\nTEST-001,Updated part,Shanghai CN,123.45,5\n",
            content_type="text/csv",
        )
        result = process_seller_csv_upload(
            seller=self.seller,
            upload=upload,
            category_name="Smoke",
            default_stock=20,
            import_mode="apply",
        )
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.total_rows, 1)
        self.assertEqual(result.processed_rows, 1)
        self.assertEqual(result.failed_rows, 0)
        self.assertEqual(result.success_rate, 100)
        part.refresh_from_db()
        self.assertGreater(part.data_updated_at, old_timestamp)

    def test_import_error_contains_fix_hint(self):
        upload = SimpleUploadedFile(
            "bad_rows.csv",
            b"PartNumber,Name,WarehouseAddress,Price_FOB_SEA\n,Missing part,Shanghai CN,11.00\n",
            content_type="text/csv",
        )
        result = process_seller_csv_upload(
            seller=self.seller,
            upload=upload,
            category_name="Smoke",
            default_stock=20,
            import_mode="preview",
        )
        self.assertEqual(result.failed_rows, 1)
        self.assertTrue(result.errors)
        self.assertEqual(result.errors[0].get("code"), "missing_part_number")
        self.assertIn("Заполните колонку", result.errors[0].get("hint", ""))

    def test_import_accepts_partnumber_and_price_exw_columns(self):
        upload = SimpleUploadedFile(
            "supplier_actual.csv",
            b"PartNumber,CrossNumber,Brand,Name,Quantity,WarehouseAddress,Price_EXW,Price_FOB_SEA\n561-50-82311,5615082311,Komatsu,BUSHING,8,Shanghai CN,100,120\n",
            content_type="text/csv",
        )
        result = process_seller_csv_upload(
            seller=self.seller,
            upload=upload,
            category_name="Komatsu",
            default_stock=20,
            import_mode="apply",
        )
        self.assertEqual(result.total_rows, 1)
        self.assertEqual(result.processed_rows, 1)
        self.assertEqual(result.failed_rows, 0)
        self.assertEqual(result.created, 1)

    def test_seller_rfq_inbox_shows_only_matching_supplier_rfqs(self):
        category = Category.objects.create(name="Inbox", slug="inbox")
        seller_part = Part.objects.create(
            seller=self.seller,
            title="Seller part",
            slug="seller-part",
            oem_number="SELL-001",
            description="Inbox part",
            price="50.00",
            stock_quantity=3,
            category=category,
        )
        other_seller = User.objects.create_user(username="other_seller", password="pass12345")
        UserProfile.objects.create(user=other_seller, role="seller", company_name="Other Supplier")
        other_part = Part.objects.create(
            seller=other_seller,
            title="Other part",
            slug="other-part",
            oem_number="OTHER-001",
            description="Other inbox part",
            price="60.00",
            stock_quantity=2,
            category=category,
        )

        visible_rfq = RFQ.objects.create(customer_name="Visible Buyer", customer_email="visible@example.com")
        RFQItem.objects.create(rfq=visible_rfq, query="SELL-001", quantity=2, matched_part=seller_part, state="auto_matched")

        hidden_rfq = RFQ.objects.create(customer_name="Hidden Buyer", customer_email="hidden@example.com")
        RFQItem.objects.create(rfq=hidden_rfq, query="OTHER-001", quantity=1, matched_part=other_part, state="auto_matched")

        request = self.factory.get(reverse("seller_rfq_inbox"))
        request.user = self.seller
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))

        response = seller_rfq_inbox(request)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn(f"RFQ #{visible_rfq.id}", body)
        self.assertNotIn(f"RFQ #{hidden_rfq.id}", body)

    def test_seller_request_list_and_detail_pages(self):
        category = Category.objects.create(name="Requests", slug="requests")
        seller_part = Part.objects.create(
            seller=self.seller,
            title="Request part",
            slug="request-part",
            oem_number="REQ-001",
            description="Request part",
            price="80.00",
            stock_quantity=4,
            category=category,
        )
        rfq = RFQ.objects.create(customer_name="Request Buyer", customer_email="request@example.com", company_name="Request Co")
        RFQItem.objects.create(rfq=rfq, query="REQ-001", quantity=3, matched_part=seller_part, state="auto_matched")

        list_request = self.factory.get(reverse("seller_request_list"))
        list_request.user = self.seller
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(list_request)
        list_request.session.save()
        setattr(list_request, "_messages", FallbackStorage(list_request))

        list_response = seller_request_list(list_request)
        self.assertEqual(list_response.status_code, 200)
        self.assertIn(f"RFQ #{rfq.id}", list_response.content.decode("utf-8"))

        detail_request = self.factory.get(reverse("seller_request_detail", args=[rfq.id]))
        detail_request.user = self.seller
        session_middleware.process_request(detail_request)
        detail_request.session.save()
        setattr(detail_request, "_messages", FallbackStorage(detail_request))

        detail_response = seller_request_detail(detail_request, rfq.id)
        self.assertEqual(detail_response.status_code, 200)
        body = detail_response.content.decode("utf-8")
        self.assertIn("Request part", body)
        self.assertIn("REQ-001", body)

    def test_seller_bulk_action_updates_selected_parts(self):
        category = Category.objects.create(name="Bulk", slug="bulk")
        part = Part.objects.create(
            seller=self.seller,
            title="Bulk part",
            slug="bulk-part",
            oem_number="BULK-001",
            description="Bulk test part",
            price="70.00",
            stock_quantity=4,
            category=category,
            availability_status="active",
            is_active=True,
        )
        response = self.client.post(
            reverse("seller_parts_bulk_action"),
            data={
                "action": "status",
                "availability_status": "limited",
                "part_ids": [part.id],
                "return_qs": "q=bulk",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('seller_product_list')}?q=bulk")
        part.refresh_from_db()
        self.assertEqual(part.availability_status, "limited")

    def test_seller_product_detail_page_opens_for_own_part(self):
        category = Category.objects.create(name="Detail Product", slug="detail-product")
        part = Part.objects.create(
            seller=self.seller,
            title="Detailed part",
            slug="detailed-part",
            oem_number="DET-PROD-001",
            description="Detailed product page",
            price="150.00",
            stock_quantity=6,
            category=category,
        )

        request = self.factory.get(reverse("seller_product_detail", args=[part.id]))
        request.user = self.seller
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))

        response = seller_product_detail(request, part.id)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Detailed part", body)
        self.assertIn("DET-PROD-001", body)

    def test_seller_orders_page_shows_only_own_orders(self):
        category = Category.objects.create(name="Orders", slug="orders")
        own_part = Part.objects.create(
            seller=self.seller,
            title="Own order part",
            slug="own-order-part",
            oem_number="ORD-001",
            description="Own order part",
            price="90.00",
            stock_quantity=2,
            category=category,
        )
        other_seller = User.objects.create_user(username="seller_other_orders", password="pass12345")
        UserProfile.objects.create(user=other_seller, role="seller", company_name="Other Orders Seller")
        other_part = Part.objects.create(
            seller=other_seller,
            title="Other order part",
            slug="other-order-part",
            oem_number="ORD-999",
            description="Other order part",
            price="120.00",
            stock_quantity=3,
            category=category,
        )

        own_order = Order.objects.create(
            customer_name="Own Buyer",
            customer_email="own_buyer@example.com",
            customer_phone="+1000000001",
            delivery_address="Riyadh",
            status="pending",
            payment_status="awaiting_reserve",
            total_amount="180.00",
        )
        OrderItem.objects.create(order=own_order, part=own_part, quantity=2, unit_price="90.00")

        foreign_order = Order.objects.create(
            customer_name="Foreign Buyer",
            customer_email="foreign_buyer@example.com",
            customer_phone="+1000000002",
            delivery_address="Jeddah",
            status="pending",
            payment_status="awaiting_reserve",
            total_amount="120.00",
        )
        OrderItem.objects.create(order=foreign_order, part=other_part, quantity=1, unit_price="120.00")

        request = self.factory.get(reverse("seller_orders"))
        request.user = self.seller
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))

        response = seller_orders(request)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn(f"Заказ #{own_order.id}", body)
        self.assertNotIn(f"Заказ #{foreign_order.id}", body)

    def test_seller_order_detail_page_opens_for_own_order(self):
        category = Category.objects.create(name="Order Detail", slug="order-detail")
        own_part = Part.objects.create(
            seller=self.seller,
            title="Own detail part",
            slug="own-detail-part",
            oem_number="DET-001",
            description="Own detail part",
            price="95.00",
            stock_quantity=2,
            category=category,
        )
        own_order = Order.objects.create(
            customer_name="Detail Buyer",
            customer_email="detail_buyer@example.com",
            customer_phone="+1000000003",
            delivery_address="Jeddah",
            status="confirmed",
            payment_status="reserve_paid",
            total_amount="190.00",
        )
        OrderItem.objects.create(order=own_order, part=own_part, quantity=2, unit_price="95.00")

        request = self.factory.get(reverse("seller_order_detail", args=[own_order.id]))
        request.user = self.seller
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))

        response = seller_order_detail(request, own_order.id)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn(f"Заказ #{own_order.id}", body)
        self.assertIn("Own detail part", body)
