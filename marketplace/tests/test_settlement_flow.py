import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assistant.actions import can_execute, cancel_order, confirm_delivery
from assistant.documents import sign_document
from assistant.settlement_actions import settlement_finance_queue
from assistant.settlements import (
    SettlementError,
    confirm_bank_payment,
    issue_invoice,
    prepare_settlement_package,
    report_invoice_paid,
    reverse_bank_payment,
)
from marketplace.models import (
    Category,
    CompanyVerification,
    Notification,
    Order,
    OrderDocument,
    OrderEvent,
    OrderItem,
    Part,
    SettlementContract,
    SettlementInvoice,
    SettlementPayment,
    UserProfile,
)
from marketplace.order_access import (
    buyer_can_access_document,
    buyer_visible_documents,
    seller_can_access_document,
    seller_visible_documents,
)

SETTLEMENT_SETTINGS = {
    "SETTLEMENT_MODE": "invoice_contract",
    "SETTLEMENT_REQUIRED": True,
    "LEGACY_WALLET_UI_ENABLED": False,
    "PLATFORM_LEGAL_NAME": "ООО Консолидатор Партс",
    "PLATFORM_LEGAL_ADDRESS": "Москва, Примерная улица, 1",
    "PLATFORM_TAX_ID": "7700000000",
    "PLATFORM_REGISTRATION_NO": "1207700000000",
    "PLATFORM_BANK_NAME": "Тестовый банк",
    "PLATFORM_BANK_ACCOUNT": "40702810000000000001",
    "PLATFORM_BANK_SWIFT": "TESTBANK",
    "PLATFORM_SIGNATORY": "Иванов Иван Иванович",
    "PLATFORM_SIGNATORY_TITLE": "Генеральный директор",
}


@override_settings(**SETTLEMENT_SETTINGS)
class SettlementFlowTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        media_settings = self.settings(MEDIA_ROOT=self.media.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)

        users = get_user_model()
        self.buyer = users.objects.create_user("settlement_buyer")
        self.seller_one = users.objects.create_user("settlement_seller_one")
        self.seller_two = users.objects.create_user("settlement_seller_two")
        self.finance = users.objects.create_user("settlement_finance")
        self.operator = users.objects.create_user("settlement_operator")
        self._party(self.buyer, "buyer", "ООО Покупатель", "7800000001")
        self._party(self.seller_one, "seller", "ООО Продавец Один", "7800000002")
        self._party(self.seller_two, "seller", "ООО Продавец Два", "7800000003")
        UserProfile.objects.update_or_create(
            user=self.finance,
            defaults={"role": "operator", "operator_role": "payment"},
        )
        UserProfile.objects.update_or_create(
            user=self.operator,
            defaults={"role": "operator", "operator_role": "general"},
        )
        category = Category.objects.create(name="Тест", slug="settlement-test")
        part_one = Part.objects.create(
            title="Деталь один",
            slug="settlement-part-one",
            oem_number="SET-001",
            price=Decimal("100.00"),
            stock_quantity=20,
            seller=self.seller_one,
            category=category,
        )
        part_two = Part.objects.create(
            title="Деталь два",
            slug="settlement-part-two",
            oem_number="SET-002",
            price=Decimal("200.00"),
            stock_quantity=20,
            seller=self.seller_two,
            category=category,
        )
        self.order = Order.objects.create(
            customer_name="ООО Покупатель",
            customer_email="buyer@example.test",
            customer_phone="+70000000000",
            delivery_address="Москва, Складская улица, 1",
            buyer=self.buyer,
            total_amount=Decimal("3000.00"),
            reserve_amount=Decimal("300.00"),
            reserve_percent=Decimal("10.00"),
            status="pending",
            payment_status="awaiting_reserve",
        )
        OrderItem.objects.create(
            order=self.order,
            part=part_one,
            quantity=10,
            unit_price=Decimal("100.00"),
        )
        OrderItem.objects.create(
            order=self.order,
            part=part_two,
            quantity=10,
            unit_price=Decimal("200.00"),
        )

    def _party(self, user, role, legal_name, tax_id):
        UserProfile.objects.update_or_create(
            user=user,
            defaults={
                "role": role,
                "company_name": legal_name,
                "tax_id": tax_id,
                "contact_name": "Ответственный сотрудник",
                "position": "Директор",
                "country": "RU",
            },
        )
        CompanyVerification.objects.create(
            user=user,
            status="verified",
            legal_name=legal_name,
            inn=tax_id,
            ogrn=f"1{tax_id}00",
            legal_address="Москва, Юридическая улица, 1",
            bank_name="Банк контрагента",
            bank_account=f"40702810{tax_id}01"[:30],
            bik="044500000",
            director_name="Ответственный сотрудник",
            country="RU",
        )

    def test_package_is_idempotent_and_documents_are_party_scoped(self):
        package = prepare_settlement_package(self.order, self.buyer)

        self.assertEqual(SettlementContract.objects.count(), 3)
        self.assertEqual(SettlementInvoice.objects.count(), 6)
        self.assertEqual(package["buyer_reserve_invoice"].amount, Decimal("300.00"))
        self.assertEqual(package["buyer_final_invoice"].amount, Decimal("2700.00"))
        seller_amounts = {
            (invoice.seller_id, invoice.stage): invoice.amount
            for invoice in package["seller_invoices"]
        }
        self.assertEqual(seller_amounts[(self.seller_one.id, "reserve")], Decimal("100.00"))
        self.assertEqual(seller_amounts[(self.seller_one.id, "final")], Decimal("900.00"))
        self.assertEqual(seller_amounts[(self.seller_two.id, "reserve")], Decimal("200.00"))
        self.assertEqual(seller_amounts[(self.seller_two.id, "final")], Decimal("1800.00"))

        buyer_document = package["buyer_contract"].document
        seller_one_document = next(
            contract.document
            for contract in package["seller_contracts"]
            if contract.seller_id == self.seller_one.id
        )
        seller_two_document = next(
            contract.document
            for contract in package["seller_contracts"]
            if contract.seller_id == self.seller_two.id
        )
        self.assertEqual(buyer_document.audience, "buyer")
        self.assertEqual(seller_one_document.audience, "seller")
        self.assertTrue(buyer_can_access_document(self.buyer, buyer_document))
        self.assertFalse(buyer_can_access_document(self.buyer, seller_one_document))
        self.assertTrue(seller_can_access_document(self.seller_one, seller_one_document))
        self.assertFalse(seller_can_access_document(self.seller_one, seller_two_document))
        self.assertFalse(seller_can_access_document(self.seller_one, buyer_document))
        self.assertNotIn(
            seller_one_document.id,
            buyer_visible_documents(self.order, self.buyer).values_list("id", flat=True),
        )
        self.assertNotIn(
            seller_two_document.id,
            seller_visible_documents(self.order, self.seller_one).values_list("id", flat=True),
        )
        for document in OrderDocument.objects.exclude(file_obj=""):
            with document.file_obj.open("rb") as pdf:
                self.assertEqual(pdf.read(4), b"%PDF")

        prepare_settlement_package(self.order, self.buyer)
        self.assertEqual(SettlementContract.objects.count(), 3)
        self.assertEqual(SettlementInvoice.objects.count(), 6)
        self.assertEqual(OrderDocument.objects.count(), 4)

    def test_incoming_confirmation_activates_seller_invoices_and_order(self):
        package = prepare_settlement_package(self.order, self.buyer)
        reserve = package["buyer_reserve_invoice"]
        report_invoice_paid(reserve, self.buyer)
        reserve.refresh_from_db()
        self.assertEqual(reserve.status, "awaiting_confirmation")

        confirm_bank_payment(
            invoice=reserve,
            actor=self.finance,
            amount=Decimal("100.00"),
            bank_reference="BANK-IN-RESERVE-1",
        )
        reserve.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(reserve.status, "partially_paid")
        self.assertEqual(self.order.payment_status, "awaiting_reserve")

        confirm_bank_payment(
            invoice=reserve,
            actor=self.finance,
            amount=Decimal("200.00"),
            bank_reference="BANK-IN-RESERVE-2",
        )
        reserve.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(reserve.status, "paid")
        self.assertEqual(self.order.payment_status, "reserve_paid")
        self.assertEqual(self.order.status, "reserve_paid")
        seller_reserve_invoices = SettlementInvoice.objects.filter(
            direction="payable", stage="reserve"
        )
        self.assertEqual(
            set(seller_reserve_invoices.values_list("status", flat=True)),
            {"draft"},
        )
        self.assertFalse(any(item.document_id for item in seller_reserve_invoices))

        seller_invoice = seller_reserve_invoices.get(seller=self.seller_one)
        seller_contract = next(
            contract
            for contract in package["seller_contracts"]
            if contract.seller_id == self.seller_one.id
        )
        sign_document(
            {"document_id": seller_contract.document_id},
            self.seller_one,
            "seller",
        )
        sign_document(
            {"document_id": seller_contract.document_id},
            self.finance,
            "operator_payment",
        )
        seller_invoice.refresh_from_db()
        self.assertEqual(seller_invoice.status, "issued")
        self.assertIsNotNone(seller_invoice.document_id)
        confirm_bank_payment(
            invoice=seller_invoice,
            actor=self.finance,
            amount=seller_invoice.amount,
            bank_reference="BANK-OUT-SELLER-1",
        )
        seller_invoice.refresh_from_db()
        self.assertEqual(seller_invoice.status, "paid")
        self.assertEqual(
            SettlementPayment.objects.get(bank_reference="BANK-OUT-SELLER-1").direction,
            "outgoing",
        )

        with self.assertRaises(SettlementError):
            confirm_bank_payment(
                invoice=seller_reserve_invoices.get(seller=self.seller_two),
                actor=self.finance,
                amount=Decimal("10.00"),
                bank_reference="BANK-OUT-SELLER-1",
            )

    def test_final_payment_completes_order_and_opens_final_seller_invoices(self):
        package = prepare_settlement_package(self.order, self.buyer)
        reserve = package["buyer_reserve_invoice"]
        confirm_bank_payment(
            invoice=reserve,
            actor=self.finance,
            amount=reserve.amount,
            bank_reference="BANK-IN-FULL-RESERVE",
        )
        self.order.refresh_from_db()
        self.order.status = "ready_to_ship"
        self.order.save(update_fields=["status"])

        final_invoice = issue_invoice(package["buyer_final_invoice"], self.finance)
        report_invoice_paid(final_invoice, self.buyer)
        confirm_bank_payment(
            invoice=final_invoice,
            actor=self.finance,
            amount=final_invoice.amount,
            bank_reference="BANK-IN-FINAL",
        )
        self.order.refresh_from_db()
        final_invoice.refresh_from_db()
        self.assertEqual(final_invoice.status, "paid")
        self.assertEqual(self.order.payment_status, "paid")
        self.assertIsNotNone(self.order.final_paid_at)
        self.assertEqual(
            set(
                SettlementInvoice.objects.filter(
                    direction="payable", stage="final"
                ).values_list("status", flat=True)
            ),
            {"draft"},
        )

    def test_final_and_seller_invoices_follow_contract_sequence(self):
        package = prepare_settlement_package(self.order, self.buyer)
        with self.assertRaises(SettlementError):
            issue_invoice(package["buyer_final_invoice"], self.finance)
        with self.assertRaises(SettlementError):
            issue_invoice(
                next(
                    invoice for invoice in package["seller_invoices"]
                    if invoice.seller_id == self.seller_one.id
                    and invoice.stage == "reserve"
                ),
                self.finance,
            )

    def test_invoice_mode_hides_legacy_wallet_mutations(self):
        self.assertFalse(can_execute("topup_wallet", "buyer"))
        self.assertFalse(can_execute("submit_wallet_transfer", "buyer"))
        self.assertFalse(can_execute("op_confirm_topup", "operator_payment"))
        self.assertFalse(can_execute("generate_invoice_pdf", "buyer"))
        self.assertFalse(can_execute("generate_invoice_pdf", "operator_payment"))
        self.assertFalse(can_execute("op_payments_dashboard", "operator"))
        self.assertTrue(can_execute("op_payments_dashboard", "operator_payment"))
        self.assertTrue(can_execute("settlement_my_documents", "buyer"))
        self.assertTrue(can_execute("settlement_finance_queue", "operator_payment"))
        self.assertTrue(can_execute("settlement_payment_detail", "operator_payment"))
        self.assertFalse(can_execute("settlement_finance_queue", "operator"))
        self.assertFalse(can_execute("settlement_confirm_payment", "operator"))

    def test_finance_queue_preserves_order_filter_for_report(self):
        prepare_settlement_package(self.order, self.buyer)

        result = settlement_finance_queue(
            {"order_id": self.order.id}, self.finance, "operator_payment"
        )

        self.assertEqual(result.actions[0]["action"], "settlement_report")
        self.assertEqual(result.actions[0]["params"], {"order_id": self.order.id})

    def test_contract_signatures_follow_document_audience(self):
        package = prepare_settlement_package(self.order, self.buyer)
        buyer_contract = package["buyer_contract"]
        buyer_result = sign_document(
            {"document_id": buyer_contract.document_id}, self.buyer, "buyer"
        )
        self.assertNotIn("Нет доступа", buyer_result.text)
        sign_document(
            {"document_id": buyer_contract.document_id},
            self.finance,
            "operator_payment",
        )
        buyer_contract.refresh_from_db()
        self.assertEqual(buyer_contract.status, "active")
        self.assertIsNotNone(buyer_contract.signed_at)

        seller_contract = next(
            item
            for item in package["seller_contracts"]
            if item.seller_id == self.seller_one.id
        )
        denied = sign_document(
            {"document_id": seller_contract.document_id}, self.buyer, "buyer"
        )
        self.assertIn("Нет доступа", denied.text)
        denied_operator = sign_document(
            {"document_id": seller_contract.document_id},
            self.operator,
            "operator",
        )
        self.assertFalse(denied_operator.action_succeeded)
        self.assertIn("финансовый оператор", denied_operator.text)
        Notification.objects.filter(user=self.buyer).delete()
        sign_document(
            {"document_id": seller_contract.document_id}, self.seller_one, "seller"
        )
        self.assertFalse(Notification.objects.filter(user=self.buyer).exists())
        sign_document(
            {"document_id": seller_contract.document_id},
            self.finance,
            "operator_payment",
        )
        seller_contract.refresh_from_db()
        self.assertEqual(seller_contract.status, "active")

    def test_reversal_rolls_back_incoming_status_and_blocks_seller_payment(self):
        package = prepare_settlement_package(self.order, self.buyer)
        reserve = package["buyer_reserve_invoice"]
        payment = confirm_bank_payment(
            invoice=reserve,
            actor=self.finance,
            amount=reserve.amount,
            bank_reference="BANK-REVERSAL-1",
        )
        reverse_bank_payment(
            payment=payment,
            actor=self.finance,
            reason="Платёж ошибочно сопоставлен с заказом",
        )
        reserve.refresh_from_db()
        self.order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(payment.status, "reversed")
        self.assertEqual(reserve.status, "issued")
        self.assertEqual(reserve.paid_amount, Decimal("0.00"))
        self.assertEqual(self.order.payment_status, "awaiting_reserve")
        self.assertEqual(self.order.status, "pending")
        seller_invoice = SettlementInvoice.objects.get(
            direction="payable", stage="reserve", seller=self.seller_one
        )
        with self.assertRaises(SettlementError):
            confirm_bank_payment(
                invoice=seller_invoice,
                actor=self.finance,
                amount=seller_invoice.amount,
                bank_reference="BANK-BLOCKED-OUTGOING",
            )

    def test_financial_service_rejects_ordinary_operator(self):
        invoice = prepare_settlement_package(
            self.order, self.buyer
        )["buyer_reserve_invoice"]
        with self.assertRaisesRegex(SettlementError, "финансовый оператор"):
            confirm_bank_payment(
                invoice=invoice,
                actor=self.operator,
                amount=invoice.amount,
                bank_reference="BANK-DENIED-CONFIRM",
            )
        payment = confirm_bank_payment(
            invoice=invoice,
            actor=self.finance,
            amount=invoice.amount,
            bank_reference="BANK-FINANCE-CONFIRM",
        )
        with self.assertRaisesRegex(SettlementError, "финансовый оператор"):
            reverse_bank_payment(
                payment=payment,
                actor=self.operator,
                reason="Обычный оператор не должен отменять проводку",
            )

    def test_buyer_can_report_another_transfer_after_partial_payment(self):
        invoice = prepare_settlement_package(
            self.order, self.buyer
        )["buyer_reserve_invoice"]
        confirm_bank_payment(
            invoice=invoice,
            actor=self.finance,
            amount=Decimal("100.00"),
            bank_reference="BANK-PARTIAL-FIRST",
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "partially_paid")
        report_invoice_paid(invoice, self.buyer)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "awaiting_confirmation")
        self.assertEqual(invoice.paid_amount, Decimal("100.00"))

    def test_final_payment_cannot_be_reversed_after_dispatch_started(self):
        package = prepare_settlement_package(self.order, self.buyer)
        reserve = package["buyer_reserve_invoice"]
        confirm_bank_payment(
            invoice=reserve,
            actor=self.finance,
            amount=reserve.amount,
            bank_reference="BANK-DISPATCH-RESERVE",
        )
        self.order.refresh_from_db()
        self.order.status = "ready_to_ship"
        self.order.save(update_fields=["status"])
        final_invoice = issue_invoice(package["buyer_final_invoice"], self.finance)
        payment = confirm_bank_payment(
            invoice=final_invoice,
            actor=self.finance,
            amount=final_invoice.amount,
            bank_reference="BANK-DISPATCH-FINAL",
        )
        self.order.status = "transit_abroad"
        self.order.save(update_fields=["status"])

        with self.assertRaisesRegex(SettlementError, "после начала отгрузки"):
            reverse_bank_payment(
                payment=payment,
                actor=self.finance,
                reason="Проверка запрета после начала логистики",
            )
        payment.refresh_from_db()
        final_invoice.refresh_from_db()
        self.assertEqual(payment.status, "confirmed")
        self.assertEqual(final_invoice.status, "paid")

    def test_overdue_watcher_notifies_once(self):
        from marketplace.tasks import mark_overdue_settlement_invoices

        invoice = prepare_settlement_package(
            self.order, self.buyer
        )["buyer_reserve_invoice"]
        invoice.due_date = timezone.localdate() - timedelta(days=1)
        invoice.save(update_fields=["due_date"])
        first = mark_overdue_settlement_invoices.run()
        second = mark_overdue_settlement_invoices.run()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "overdue")
        self.assertIn("Marked 1", first)
        self.assertIn("Marked 0", second)
        self.assertEqual(
            Notification.objects.filter(
                user=self.buyer, title="Счёт просрочен"
            ).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(
                user=self.finance, title="Просрочен расчётный счёт"
            ).count(),
            1,
        )

    def test_finance_can_export_register_but_buyer_cannot(self):
        package = prepare_settlement_package(self.order, self.buyer)
        invoice = package["buyer_reserve_invoice"]
        confirm_bank_payment(
            invoice=invoice,
            actor=self.finance,
            amount=Decimal("50.00"),
            bank_reference="=FORMULA-MUST-BE-ESCAPED",
        )
        url = reverse("assistant-settlement-report")
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.finance)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn("CP-INV-", content)
        self.assertIn("'=FORMULA-MUST-BE-ESCAPED", content)

    def test_payment_proof_is_finance_only_and_cannot_be_replaced(self):
        invoice = prepare_settlement_package(
            self.order, self.buyer
        )["buyer_reserve_invoice"]
        payment = confirm_bank_payment(
            invoice=invoice,
            actor=self.finance,
            amount=Decimal("50.00"),
            bank_reference="BANK-WITH-PROOF",
        )
        url = reverse(
            "assistant-settlement-payment-proof",
            kwargs={"payment_id": payment.id},
        )
        proof = SimpleUploadedFile(
            "payment.pdf",
            b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF",
            content_type="application/pdf",
        )
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.post(url, {"file": proof}).status_code, 403)
        self.client.force_login(self.operator)
        proof = SimpleUploadedFile(
            "payment.pdf",
            b"%PDF-1.4\n%%EOF",
            content_type="application/pdf",
        )
        self.assertEqual(self.client.post(url, {"file": proof}).status_code, 403)

        self.client.force_login(self.finance)
        proof = SimpleUploadedFile(
            "payment.pdf",
            b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF",
            content_type="application/pdf",
        )
        self.assertEqual(self.client.post(url, {"file": proof}).status_code, 201)
        payment.refresh_from_db()
        self.assertTrue(payment.proof_file.name.endswith("payment.pdf"))
        self.assertTrue(
            OrderEvent.objects.filter(
                order=self.order,
                event_type="document_uploaded",
                meta__settlement_payment_id=payment.id,
            ).exists()
        )
        self.assertEqual(self.client.get(url).status_code, 200)
        duplicate = SimpleUploadedFile(
            "replacement.pdf",
            b"%PDF-1.4\n%%EOF",
            content_type="application/pdf",
        )
        self.assertEqual(self.client.post(url, {"file": duplicate}).status_code, 409)

    def test_delivery_closes_order_without_automatic_seller_payment(self):
        package = prepare_settlement_package(self.order, self.buyer)
        reserve = package["buyer_reserve_invoice"]
        confirm_bank_payment(
            invoice=reserve,
            actor=self.finance,
            amount=reserve.amount,
            bank_reference="BANK-DELIVERY-RESERVE",
        )
        self.order.refresh_from_db()
        self.order.status = "ready_to_ship"
        self.order.save(update_fields=["status"])
        final_invoice = issue_invoice(package["buyer_final_invoice"], self.finance)
        confirm_bank_payment(
            invoice=final_invoice,
            actor=self.finance,
            amount=final_invoice.amount,
            bank_reference="BANK-DELIVERY-FINAL",
        )
        self.order.refresh_from_db()
        self.order.status = "delivered"
        self.order.save(update_fields=["status"])

        from unittest.mock import patch

        with patch("assistant.actions._stage_checklist", return_value=[]):
            result = confirm_delivery(
                {"order_id": self.order.id, "confirmed": True},
                self.buyer,
                "buyer",
            )
        self.assertTrue(result.action_succeeded)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")
        self.assertFalse(
            SettlementPayment.objects.filter(direction="outgoing").exists()
        )
        self.assertTrue(
            OrderEvent.objects.filter(
                order=self.order,
                event_type="status_changed",
                meta__settlement_mode="invoice_contract",
            ).exists()
        )

    def test_payment_audit_events_cover_report_confirm_and_reversal(self):
        invoice = prepare_settlement_package(
            self.order, self.buyer
        )["buyer_reserve_invoice"]
        report_invoice_paid(invoice, self.buyer)
        payment = confirm_bank_payment(
            invoice=invoice,
            actor=self.finance,
            amount=invoice.amount,
            bank_reference="BANK-AUDIT-EVENTS",
        )
        reverse_bank_payment(
            payment=payment,
            actor=self.finance,
            reason="Ошибочное сопоставление платежа",
        )
        self.assertEqual(
            set(
                OrderEvent.objects.filter(
                    order=self.order,
                    event_type__in={
                        "payment_reported", "payment_confirmed", "payment_reversed"
                    },
                ).values_list("event_type", flat=True)
            ),
            {"payment_reported", "payment_confirmed", "payment_reversed"},
        )

    def test_unpaid_order_cancellation_preserves_cancelled_documents(self):
        prepare_settlement_package(self.order, self.buyer)
        result = cancel_order(
            {"order_id": self.order.id},
            self.buyer,
            "buyer",
        )
        self.assertTrue(result.action_succeeded)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertEqual(
            set(
                SettlementInvoice.objects.filter(order=self.order).values_list(
                    "status", flat=True
                )
            ),
            {"cancelled"},
        )
        self.assertEqual(
            set(
                SettlementContract.objects.filter(order=self.order).values_list(
                    "status", flat=True
                )
            ),
            {"cancelled"},
        )
        self.assertTrue(OrderDocument.objects.filter(order=self.order).exists())
