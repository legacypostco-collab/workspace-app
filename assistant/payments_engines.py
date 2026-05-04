"""Pluggable payment engines.

`payments.py` API остаётся стабильным; engines подключаются через
переменную окружения PAYMENT_ENGINE:

  PAYMENT_ENGINE=wallet    — встроенная Wallet-эскроу (по умолчанию,
                              работает в demo и на проде если Stripe
                              не настроен)
  PAYMENT_ENGINE=stripe    — реальный Stripe Connect destination_charge.
                              Требует STRIPE_SECRET_KEY и
                              STRIPE_WEBHOOK_SECRET в env.

Оба движка реализуют один и тот же интерфейс — calling-сайты в
actions.py / operator_actions.py не меняются при переключении.
"""
from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal
from typing import Protocol

logger = logging.getLogger(__name__)


# ── Engine interface ─────────────────────────────────────────

class PaymentEngine(Protocol):
    name: str

    def create_intent(self, amount: Decimal, *, order_id: int, payer, kind: str) -> dict: ...
    def confirm_intent(self, intent: dict, payer) -> dict: ...
    def release_to_seller(self, *, order, seller, amount: Decimal) -> dict: ...
    def refund_to_buyer(self, *, order, buyer, amount: Decimal) -> dict: ...


# ── Wallet engine (current Wallet-based stub) ────────────────

class WalletEngine:
    """Текущий встроенный движок: Wallet + WalletTx + sentinel платформенный.

    Работает прямо из коробки в demo. На проде подходит для случаев,
    когда мы держим деньги «у себя на платформе» и не используем Stripe
    (например, B2B со счёт-фактурой и эскроу-агентом отдельно).
    """
    name = "wallet"

    def create_intent(self, amount, *, order_id, payer, kind):
        return {
            "id": "pi_wallet_" + uuid.uuid4().hex[:24],
            "amount": float(amount),
            "currency": "usd",
            "order_id": order_id,
            "payer_id": payer.id,
            "kind": kind,
            "status": "requires_confirmation",
            "engine": "wallet",
        }

    def confirm_intent(self, intent, payer):
        # Импортируем lazy чтобы избежать циклов
        from .payments import _wallet_confirm_intent
        return _wallet_confirm_intent(intent, payer)

    def release_to_seller(self, *, order, seller, amount):
        from .payments import _wallet_release_to_seller
        return _wallet_release_to_seller(order=order, seller=seller, amount=amount)

    def refund_to_buyer(self, *, order, buyer, amount):
        from .payments import _wallet_refund_to_buyer
        return _wallet_refund_to_buyer(order=order, buyer=buyer, amount=amount)


# ── Stripe engine (Stripe Connect destination_charge) ────────

class StripeEngine:
    """Реальный Stripe Connect.

    Требования:
      • STRIPE_SECRET_KEY — секретный API-ключ
      • Каждый продавец должен иметь подключённый Stripe-аккаунт
        (его ID хранится в profile.stripe_account_id или аналогичном
        поле; ниже используется placeholder).

    Архитектура:
      create_intent → stripe.PaymentIntent.create(transfer_data={destination: platform_account})
      confirm_intent → stripe.PaymentIntent.confirm()  (на сервере или на клиенте через client_secret)
      release_to_seller → stripe.Transfer.create(destination=seller_account, source_transaction=charge.id)
      refund_to_buyer → stripe.Refund.create(charge=charge.id, amount=...)

    На текущем этапе — скелет: вызовы оставлены `NotImplementedError`,
    чтобы при включении PAYMENT_ENGINE=stripe без подготовленного
    окружения мы не делали тихих неправильных вещей. Доработка — после
    регистрации Stripe-аккаунта и связки seller→stripe_account_id.
    """
    name = "stripe"

    def __init__(self):
        self.secret_key = os.getenv("STRIPE_SECRET_KEY", "")
        self.platform_account = os.getenv("STRIPE_PLATFORM_ACCOUNT", "")
        if not self.secret_key:
            raise RuntimeError("STRIPE_SECRET_KEY is required for stripe engine")
        try:
            import stripe
        except ImportError as e:
            raise RuntimeError("stripe package not installed: pip install stripe") from e
        stripe.api_key = self.secret_key
        self._stripe = stripe

    def create_intent(self, amount, *, order_id, payer, kind):
        # Уникальный idempotency-key чтобы повтор не создал дубликат
        idem = f"order_{order_id}_{kind}_{uuid.uuid4().hex[:8]}"
        intent = self._stripe.PaymentIntent.create(
            amount=int(Decimal(str(amount)) * 100),  # cents
            currency="usd",
            metadata={"order_id": str(order_id), "kind": kind, "payer_id": str(payer.id)},
            # transfer_data={"destination": self.platform_account},  # → enable for Connect
            idempotency_key=idem,
        )
        return {
            "id": intent.id,
            "amount": float(amount),
            "currency": "usd",
            "order_id": order_id,
            "payer_id": payer.id,
            "kind": kind,
            "status": intent.status,
            "engine": "stripe",
            "client_secret": intent.client_secret,  # для подтверждения на клиенте
        }

    def confirm_intent(self, intent, payer):
        # Confirm на сервере — обычно происходит на клиенте через Stripe Elements
        confirmed = self._stripe.PaymentIntent.confirm(intent["id"])
        intent["status"] = confirmed.status
        return intent

    def release_to_seller(self, *, order, seller, amount):
        # Требует, чтобы у seller был подключённый Stripe Connect аккаунт
        seller_account = getattr(seller, "stripe_account_id", "")
        if not seller_account:
            return {"ok": False, "reason": f"seller {seller.username} has no stripe_account_id"}
        transfer = self._stripe.Transfer.create(
            amount=int(Decimal(str(amount)) * 100),
            currency="usd",
            destination=seller_account,
            metadata={"order_id": str(order.id)},
        )
        return {"ok": True, "amount": float(amount), "to": seller.username,
                "transfer_id": transfer.id, "engine": "stripe"}

    def refund_to_buyer(self, *, order, buyer, amount):
        # Найти charge по заказу — в реальной системе хранится intent_id в Order
        # Здесь — placeholder; нужна вспомогательная таблица OrderPayment(stripe_charge_id, ...)
        return {"ok": False, "reason": "refund_to_buyer skeleton — needs OrderPayment table to find charge_id",
                "engine": "stripe"}


# ── Engine selector ─────────────────────────────────────────

_ENGINE_INSTANCE: PaymentEngine | None = None


def get_engine() -> PaymentEngine:
    """Возвращает активный engine (singleton).

    PAYMENT_ENGINE=stripe → StripeEngine, иначе → WalletEngine.
    Если выбран stripe, но он сломан (нет ключей / нет SDK) — fallback на wallet
    с предупреждением в лог.
    """
    global _ENGINE_INSTANCE
    if _ENGINE_INSTANCE is not None:
        return _ENGINE_INSTANCE
    name = (os.getenv("PAYMENT_ENGINE") or "wallet").lower()
    if name == "stripe":
        try:
            _ENGINE_INSTANCE = StripeEngine()
            logger.info("payments engine: stripe")
            return _ENGINE_INSTANCE
        except Exception:
            logger.exception("stripe engine init failed → falling back to wallet")
    _ENGINE_INSTANCE = WalletEngine()
    logger.info("payments engine: wallet (default)")
    return _ENGINE_INSTANCE


# ── Webhook signature verification ──────────────────────────

def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Проверка Stripe-Signature заголовка (без парсинга event'а).

    Возвращает True если HMAC-SHA256 валиден (или verification отключён).
    Если STRIPE_WEBHOOK_SECRET не установлен — проверка пропускается
    (демо-режим). Используем низкоуровневый WebhookSignature.verify_header,
    чтобы не запускать Stripe event-parser, который требует поля
    Stripe-API формата (object, id, ...) — нам важна только подпись.
    """
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        return True  # demo mode — webhook верят на слово
    try:
        from stripe import WebhookSignature
        # Stripe library wants str, not bytes (does its own .encode internally)
        payload_str = raw_body.decode("utf-8") if isinstance(raw_body, (bytes, bytearray)) else raw_body
        WebhookSignature.verify_header(payload_str, signature_header, secret)
        return True
    except Exception:
        logger.warning("stripe webhook signature verify failed")
        return False
