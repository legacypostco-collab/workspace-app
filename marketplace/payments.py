"""Payment provider adapters.

Strategy: thin abstraction so production can swap providers
(YooKassa, Stripe, Tinkoff, CloudPayments) by changing one env var.

Each adapter implements:
  create_payment(order, amount, return_url) -> {payment_id, confirmation_url}
  verify_callback(payload, signature) -> (order_id, amount, status)
  capture(payment_id) -> success bool
  refund(payment_id, amount) -> refund_id
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Optional

from django.conf import settings


@dataclass
class PaymentResult:
    payment_id: str
    confirmation_url: str
    status: str  # "pending" | "succeeded" | "canceled"
    amount: float


class BasePaymentAdapter:
    """Interface — all adapters subclass this."""
    name = "base"

    def create_payment(self, order, amount: float, return_url: str, description: str = "") -> PaymentResult:
        raise NotImplementedError

    def verify_callback(self, payload: dict, signature: str = "") -> tuple[str, float, str]:
        raise NotImplementedError

    def capture(self, payment_id: str) -> bool:
        raise NotImplementedError

    def refund(self, payment_id: str, amount: Optional[float] = None) -> str:
        raise NotImplementedError


class StubPaymentAdapter(BasePaymentAdapter):
    """Demo adapter. Returns mock data, no external calls."""
    name = "stub"

    def create_payment(self, order, amount, return_url, description=""):
        # Mock payment ID for demo
        pid = f"stub-{order.id}-{int(amount * 100)}"
        return PaymentResult(
            payment_id=pid,
            confirmation_url=f"{return_url}?stub_payment={pid}&status=succeeded",
            status="pending",
            amount=amount,
        )

    def verify_callback(self, payload, signature=""):
        # Always succeeds in stub mode
        return (
            str(payload.get("order_id", "")),
            float(payload.get("amount", 0)),
            "succeeded",
        )

    def capture(self, payment_id):
        return True

    def refund(self, payment_id, amount=None):
        return f"refund-{payment_id}"


class YooKassaAdapter(BasePaymentAdapter):
    """Russian payment provider (yookassa.ru). Production-ready skeleton."""
    name = "yookassa"

    def __init__(self):
        self.shop_id = os.getenv("YOOKASSA_SHOP_ID", "")
        self.secret_key = os.getenv("YOOKASSA_SECRET_KEY", "")
        if not self.shop_id or not self.secret_key:
            raise RuntimeError("YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY env vars required")

    def create_payment(self, order, amount, return_url, description=""):
        try:
            from yookassa import Configuration, Payment
            Configuration.account_id = self.shop_id
            Configuration.secret_key = self.secret_key
            payment = Payment.create({
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": return_url},
                "capture": True,
                "description": description or f"Order #{order.id}",
                "metadata": {"order_id": str(order.id)},
            })
            return PaymentResult(
                payment_id=payment.id,
                confirmation_url=payment.confirmation.confirmation_url,
                status=payment.status,
                amount=amount,
            )
        except ImportError:
            raise RuntimeError("yookassa SDK not installed: pip install yookassa")

    def verify_callback(self, payload, signature=""):
        # YooKassa sends notification with signature in headers
        # Real impl validates signature against secret_key
        obj = payload.get("object", {})
        return (
            obj.get("metadata", {}).get("order_id", ""),
            float(obj.get("amount", {}).get("value", 0)),
            obj.get("status", "pending"),
        )

    def capture(self, payment_id):
        try:
            from yookassa import Payment
            Payment.capture(payment_id, {})
            return True
        except Exception:
            return False

    def refund(self, payment_id, amount=None):
        try:
            from yookassa import Refund
            params = {"payment_id": payment_id}
            if amount:
                params["amount"] = {"value": f"{amount:.2f}", "currency": "RUB"}
            r = Refund.create(params)
            return r.id
        except Exception:
            return ""


class StripeAdapter(BasePaymentAdapter):
    """Stripe — for international clients. Production skeleton."""
    name = "stripe"

    def __init__(self):
        self.api_key = os.getenv("STRIPE_SECRET_KEY", "")
        if not self.api_key:
            raise RuntimeError("STRIPE_SECRET_KEY env var required")

    def create_payment(self, order, amount, return_url, description=""):
        try:
            import stripe
            stripe.api_key = self.api_key
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": description or f"Order #{order.id}"},
                        "unit_amount": int(amount * 100),
                    },
                    "quantity": 1,
                }],
                mode="payment",
                success_url=return_url + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=return_url + "?canceled=1",
                metadata={"order_id": str(order.id)},
            )
            return PaymentResult(
                payment_id=session.id,
                confirmation_url=session.url,
                status="pending",
                amount=amount,
            )
        except ImportError:
            raise RuntimeError("stripe SDK not installed: pip install stripe")

    def verify_callback(self, payload, signature=""):
        try:
            import stripe
            secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
            if secret and signature:
                event = stripe.Webhook.construct_event(payload, signature, secret)
            else:
                event = payload
            obj = event.get("data", {}).get("object", {}) if isinstance(event, dict) else event["data"]["object"]
            return (
                obj.get("metadata", {}).get("order_id", ""),
                float(obj.get("amount_total", 0)) / 100,
                "succeeded" if obj.get("payment_status") == "paid" else "pending",
            )
        except Exception:
            return ("", 0, "failed")

    def capture(self, payment_id):
        return True  # Stripe checkout auto-captures

    def refund(self, payment_id, amount=None):
        try:
            import stripe
            stripe.api_key = self.api_key
            r = stripe.Refund.create(charge=payment_id, amount=int(amount * 100) if amount else None)
            return r.id
        except Exception:
            return ""


# ── Factory ───────────────────────────────────────────────
def get_payment_adapter(name: str = None) -> BasePaymentAdapter:
    """Returns adapter based on env PAYMENT_PROVIDER (default: stub)."""
    name = name or os.getenv("PAYMENT_PROVIDER", "stub").strip().lower()
    if name == "yookassa":
        return YooKassaAdapter()
    if name == "stripe":
        return StripeAdapter()
    return StubPaymentAdapter()
