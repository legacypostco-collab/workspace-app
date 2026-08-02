from datetime import timedelta
from decimal import Decimal

import pyotp
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from marketplace.models import CompanyVerification, Notification, UserProfile

from .actions import execute
from .models import (
    Wallet,
    WalletTopupRequest,
    WalletTransfer,
    WalletTx,
    WalletWithdrawalRequest,
)


class WalletTopupTransitionTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.buyer = user_model.objects.create_user(username="topup_state_buyer")
        self.operator = user_model.objects.create_user(username="topup_state_operator")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        UserProfile.objects.create(user=self.operator, role="operator")
        self.wallet = Wallet.for_user(self.buyer)

    def _request(self, status="awaiting_confirmation"):
        return WalletTopupRequest.objects.create(
            user=self.buyer,
            amount=Decimal("250.00"),
            method="bank_wire",
            status=status,
            reference_code=WalletTopupRequest.make_ref(),
        )

    def test_failed_request_cannot_be_credited_later(self):
        request = self._request(status="failed")

        with self.assertRaises(ValueError):
            request.mark_paid(by_user=self.operator)

        request.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(request.status, "failed")
        self.assertEqual(self.wallet.balance, Decimal("0.00"))
        self.assertFalse(WalletTx.objects.filter(kind="topup").exists())

    def test_paid_request_is_idempotent(self):
        request = self._request()

        first = request.mark_paid(by_user=self.operator)
        second = request.mark_paid(by_user=self.operator)

        self.wallet.refresh_from_db()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.wallet.balance, Decimal("250.00"))
        self.assertEqual(WalletTx.objects.filter(kind="topup").count(), 1)


class WalletTransferTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.sender = user_model.objects.create_user(username="transfer_sender")
        self.recipient = user_model.objects.create_user(username="transfer_recipient")
        UserProfile.objects.create(user=self.sender, role="buyer")
        self.recipient_profile = UserProfile.objects.create(
            user=self.recipient,
            role="buyer",
        )
        CompanyVerification.objects.create(user=self.sender, status="verified")
        CompanyVerification.objects.create(user=self.recipient, status="verified")
        from marketplace.models import TwoFactorAuth

        self.totp_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        TwoFactorAuth.objects.create(
            user=self.sender,
            enabled=True,
            secret=self.totp_secret,
        )
        self.sender_wallet = Wallet.for_user(self.sender)
        self.sender_wallet.balance = Decimal("500.00")
        self.sender_wallet.save(update_fields=["balance", "updated_at"])
        self.recipient_wallet = Wallet.for_user(self.recipient)

    def test_confirmed_transfer_moves_funds_once_and_notifies_recipient(self):
        result = execute(
            "submit_wallet_transfer",
            {
                "recipient_role": "buyer",
                "recipient_code": self.recipient_profile.customer_public_code,
                "amount": "125.50",
                "note": "Invoice",
            },
            self.sender,
            "buyer",
        )
        self.assertTrue(result.actions)
        self.assertNotIn(self.recipient.username, result.text)
        transfer = WalletTransfer.objects.get()

        execute(
            "confirm_wallet_transfer",
            {
                "reference": transfer.reference_code,
                "otp_code": pyotp.TOTP(self.totp_secret).now(),
            },
            self.sender,
            "buyer",
        )
        execute(
            "confirm_wallet_transfer",
            {"reference": transfer.reference_code},
            self.sender,
            "buyer",
        )

        self.sender_wallet.refresh_from_db()
        self.recipient_wallet.refresh_from_db()
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, "completed")
        self.assertEqual(self.sender_wallet.balance, Decimal("374.50"))
        self.assertEqual(self.recipient_wallet.balance, Decimal("125.50"))
        self.assertEqual(WalletTx.objects.filter(kind="transfer_out").count(), 1)
        self.assertEqual(WalletTx.objects.filter(kind="transfer_in").count(), 1)
        self.assertEqual(
            Notification.objects.filter(user=self.recipient, kind="payment").count(),
            1,
        )
        notification = Notification.objects.get(user=self.recipient, kind="payment")
        self.assertNotIn(self.sender.username, notification.body)
        self.assertIn("Заказчик CP · ", notification.body)

        history = execute("list_wallet_transfers", {}, self.sender, "buyer")
        self.assertNotIn(self.recipient.username, str(history.cards))
        self.assertIn("Заказчик CP · ", str(history.cards))

    def test_expired_transfer_stays_expired_without_moving_funds(self):
        transfer = WalletTransfer.objects.create(
            sender=self.sender_wallet,
            recipient=self.recipient_wallet,
            amount=Decimal("50.00"),
            currency="USD",
            reference_code=WalletTransfer.make_ref(),
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        completed = transfer.complete()

        self.sender_wallet.refresh_from_db()
        self.recipient_wallet.refresh_from_db()
        transfer.refresh_from_db()
        self.assertEqual(completed.status, "expired")
        self.assertEqual(transfer.status, "expired")
        self.assertEqual(self.sender_wallet.balance, Decimal("500.00"))
        self.assertEqual(self.recipient_wallet.balance, Decimal("0.00"))
        self.assertFalse(WalletTx.objects.filter(kind__startswith="transfer_").exists())

    def test_transfer_is_not_completed_without_valid_second_factor(self):
        execute(
            "submit_wallet_transfer",
            {
                "recipient_role": "buyer",
                "recipient_code": self.recipient_profile.customer_public_code,
                "amount": "50.00",
            },
            self.sender,
            "buyer",
        )
        transfer = WalletTransfer.objects.get()

        result = execute(
            "confirm_wallet_transfer",
            {"reference": transfer.reference_code, "otp_code": "000000"},
            self.sender,
            "buyer",
        )

        transfer.refresh_from_db()
        self.sender_wallet.refresh_from_db()
        self.recipient_wallet.refresh_from_db()
        self.assertEqual(transfer.status, "pending")
        self.assertEqual(self.sender_wallet.balance, Decimal("500.00"))
        self.assertEqual(self.recipient_wallet.balance, Decimal("0.00"))
        self.assertIn("неверен", result.text)

    def test_unverified_recipient_is_not_disclosed_or_created(self):
        CompanyVerification.objects.filter(user=self.recipient).update(status="pending")

        result = execute(
            "submit_wallet_transfer",
            {
                "recipient_role": "buyer",
                "recipient_code": self.recipient_profile.customer_public_code,
                "amount": "50.00",
            },
            self.sender,
            "buyer",
        )

        self.assertFalse(WalletTransfer.objects.exists())
        self.assertIn("не найден", result.text)

    def test_username_cannot_be_used_as_recipient_identifier(self):
        result = execute(
            "submit_wallet_transfer",
            {
                "recipient_role": "buyer",
                "recipient_code": self.recipient.username,
                "amount": "50.00",
            },
            self.sender,
            "buyer",
        )

        self.assertFalse(WalletTransfer.objects.exists())
        self.assertIn("не найден", result.text)

    def test_another_user_cannot_confirm_or_cancel_transfer(self):
        transfer = WalletTransfer.objects.create(
            sender=self.sender_wallet,
            recipient=self.recipient_wallet,
            amount=Decimal("50.00"),
            currency="USD",
            reference_code=WalletTransfer.make_ref(),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        execute(
            "confirm_wallet_transfer",
            {"reference": transfer.reference_code},
            self.recipient,
            "buyer",
        )
        execute(
            "cancel_wallet_transfer",
            {"reference": transfer.reference_code},
            self.recipient,
            "buyer",
        )

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, "pending")


class WalletWithdrawalTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.buyer = user_model.objects.create_user(username="withdrawal_buyer")
        self.operator = user_model.objects.create_user(username="withdrawal_operator")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        UserProfile.objects.create(user=self.operator, role="operator")
        CompanyVerification.objects.create(
            user=self.buyer,
            status="verified",
            bank_name="Test Bank",
            bank_account="12345678901234567890",
        )
        self.wallet = Wallet.for_user(self.buyer)
        self.wallet.balance = Decimal("1000.00")
        self.wallet.save(update_fields=["balance", "updated_at"])

    def _submit(self, amount="250.00"):
        execute(
            "submit_withdrawal",
            {"amount": amount, "note": "Contract payment"},
            self.buyer,
            "buyer",
        )
        return WalletWithdrawalRequest.objects.get()

    def test_user_cancellation_returns_reserved_amount_once(self):
        request = self._submit()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("750.00"))

        execute(
            "cancel_withdrawal",
            {"reference": request.reference_code},
            self.buyer,
            "buyer",
        )
        execute(
            "cancel_withdrawal",
            {"reference": request.reference_code},
            self.buyer,
            "buyer",
        )

        request.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(request.status, "cancelled")
        self.assertEqual(self.wallet.balance, Decimal("1000.00"))
        self.assertEqual(WalletTx.objects.filter(kind="withdrawal_refund").count(), 1)

    def test_operator_can_approve_and_complete_without_second_debit(self):
        request = self._submit()

        execute(
            "op_approve_withdrawal",
            {"withdrawal_id": request.id},
            self.operator,
            "operator_payment",
        )
        execute(
            "op_complete_withdrawal",
            {"withdrawal_id": request.id},
            self.operator,
            "operator_payment",
        )

        request.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(request.status, "completed")
        self.assertEqual(self.wallet.balance, Decimal("750.00"))
        self.assertEqual(request.reviewed_by, self.operator)
        self.assertTrue(
            Notification.objects.filter(user=self.buyer, kind="payment").exists()
        )

    def test_operator_rejection_refunds_reserved_amount(self):
        request = self._submit()

        execute(
            "op_reject_withdrawal",
            {"withdrawal_id": request.id, "reason": "Bank account check failed"},
            self.operator,
            "operator_payment",
        )

        request.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(request.status, "rejected")
        self.assertEqual(self.wallet.balance, Decimal("1000.00"))

    def test_unverified_user_cannot_reserve_withdrawal(self):
        CompanyVerification.objects.filter(user=self.buyer).update(status="pending")

        execute(
            "submit_withdrawal",
            {"amount": "250.00"},
            self.buyer,
            "buyer",
        )

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("1000.00"))
        self.assertFalse(WalletWithdrawalRequest.objects.exists())
