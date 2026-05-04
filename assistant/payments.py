"""Stripe-compatible escrow + payment abstraction layer.

Цель: подменить «деньги в никуда» (текущий pay_reserve просто debit
buyer'а) на правильную эскроу-механику:

  • buyer pays  → деньги идут на платформу-эскроу (а не пропадают)
  • delivery confirmed → платформа переводит сумму продавцу
  • dispute refund → платформа возвращает покупателю
  • dispute release → платформа выплачивает продавцу

API повторяет Stripe Connect destination-charge модель, чтобы при
переходе на реальный Stripe заменить только реализацию helpers,
а не call-site'ы.

Implementation notes:
  • Эскроу — отдельный sentinel-юзер `__platform_escrow__` с Wallet'ом,
    чтобы не плодить новые модели и миграции.
  • Все движения атомарные через transaction.atomic().
  • WalletTx.kind теперь поддерживает escrow_hold/escrow_release/escrow_refund
    (см. модели — пришлось добавить choices).
  • Per-order escrow_balance() выводится из WalletTx-лога платформы.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import Wallet, WalletTx

logger = logging.getLogger(__name__)


ESCROW_USERNAME = "__platform_escrow__"


class InsufficientFunds(Exception):
    pass


class InsufficientEscrow(Exception):
    pass


# ── Платформа-эскроу ──────────────────────────────────────────

def get_platform_user():
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username=ESCROW_USERNAME,
        defaults={
            "first_name": "Platform",
            "last_name": "Escrow",
            "is_active": False,
            "email": "escrow@consolidator.local",
        },
    )
    return user


def get_platform_wallet() -> Wallet:
    """Sentinel-кошелёк платформы. Стартует с 0."""
    return Wallet.for_user(get_platform_user(), demo_seed_amount=0)


# ── Stripe-like Payment Intent ────────────────────────────────

def create_payment_intent(amount, *, order_id: int, payer, kind: str = "reserve") -> dict:
    """Создаёт payment intent через активный engine.

    Wallet engine — чисто in-memory placeholder.
    Stripe engine — реальный stripe.PaymentIntent.create() через API.
    """
    from .payments_engines import get_engine
    intent = get_engine().create_intent(Decimal(str(amount)), order_id=order_id, payer=payer, kind=kind)
    intent.setdefault("created_at", timezone.now().isoformat())
    return intent


def confirm_payment_intent(intent: dict, payer) -> dict:
    """Подтверждение intent → реальное движение денег через активный engine."""
    from .payments_engines import get_engine
    return get_engine().confirm_intent(intent, payer)


@transaction.atomic
def _wallet_confirm_intent(intent: dict, payer) -> dict:
    """Атомарно: списание у покупателя → зачисление в эскроу платформы.

    Соответствует stripe.PaymentIntent.confirm() в Stripe Connect режиме
    `destination_charge` где transfer_data.destination = platform.
    """
    amount = Decimal(str(intent["amount"]))
    order_id = intent["order_id"]
    kind_label = intent.get("kind", "payment")

    payer_wallet = Wallet.for_user(payer)
    if payer_wallet.balance < amount:
        raise InsufficientFunds(
            f"need ${amount} have ${payer_wallet.balance}"
        )

    plat = get_platform_wallet()
    payer_wallet.balance -= amount
    payer_wallet.save(update_fields=["balance", "updated_at"])
    plat.balance += amount
    plat.save(update_fields=["balance", "updated_at"])

    WalletTx.objects.create(
        wallet=payer_wallet, kind="escrow_hold", amount=amount,
        description=f"Эскроу-холд {kind_label} #{order_id} (intent {intent['id']})",
        order_id=order_id, balance_after=payer_wallet.balance,
    )
    WalletTx.objects.create(
        wallet=plat, kind="escrow_hold", amount=amount,
        description=f"Эскроу-приём {kind_label} #{order_id} (intent {intent['id']})",
        order_id=order_id, balance_after=plat.balance,
    )

    intent["status"] = "succeeded"
    intent["confirmed_at"] = timezone.now().isoformat()
    return intent


def split_by_seller(order) -> list[dict]:
    """Разбивка эскроу-суммы заказа по продавцам пропорционально их позициям.

    Возвращает [{"seller": user, "amount": Decimal, "items": [item_id,...]}].
    Сумма всех amount = escrow_balance_for_order(order.id).

    Для multi-supplier заказов (RFQ-консолидация). Если в заказе один
    продавец — вернёт список из одного элемента. Если у части позиций
    нет продавца (старые seed-данные) — они исключаются из распределения.
    """
    from marketplace.models import OrderItem
    items = list(OrderItem.objects.filter(order=order).select_related("part__seller"))
    by_seller: dict[int, dict] = {}
    seller_obj_by_id: dict[int, object] = {}

    base_total = Decimal("0")
    for it in items:
        seller = it.part.seller if it.part else None
        if not seller:
            continue
        line_total = Decimal(str(it.unit_price or 0)) * Decimal(it.quantity or 0)
        base_total += line_total
        bucket = by_seller.setdefault(seller.id, {"line_total": Decimal("0"), "items": []})
        bucket["line_total"] += line_total
        bucket["items"].append(it.id)
        seller_obj_by_id[seller.id] = seller

    escrow = escrow_balance_for_order(order.id)
    if base_total <= 0 or escrow <= 0:
        return []

    out = []
    accumulated = Decimal("0")
    seller_ids = list(by_seller.keys())
    for i, sid in enumerate(seller_ids):
        bucket = by_seller[sid]
        share = (bucket["line_total"] / base_total)
        # Последнему продавцу доплачиваем разницу, чтобы не потерять копейки на округлении
        if i == len(seller_ids) - 1:
            amount = (escrow - accumulated).quantize(Decimal("0.01"))
        else:
            amount = (escrow * share).quantize(Decimal("0.01"))
            accumulated += amount
        out.append({
            "seller": seller_obj_by_id[sid],
            "amount": amount,
            "items": bucket["items"],
            "share": float(share),
        })
    return out


def release_to_seller(*, order, seller, amount=None) -> dict:
    """Перевод эскроу → seller. Без amount — высвобождает весь баланс по заказу.

    Делегирует активному engine. Для Stripe режима amount обязателен.
    """
    if amount is None:
        amount = escrow_balance_for_order(order.id)
    amount = Decimal(str(amount))
    if amount <= 0:
        return {"ok": False, "reason": "ничего не удержано", "amount": 0}
    from .payments_engines import get_engine
    return get_engine().release_to_seller(order=order, seller=seller, amount=amount)


@transaction.atomic
def _wallet_release_to_seller(*, order, seller, amount: Decimal) -> dict:
    """Wallet-engine реализация: эскроу → seller wallet."""
    plat = get_platform_wallet()
    if amount is None:
        amount = escrow_balance_for_order(order.id)
    amount = Decimal(str(amount))
    if amount <= 0:
        return {"ok": False, "reason": "ничего не удержано", "amount": 0}
    if plat.balance < amount:
        raise InsufficientEscrow(f"escrow has ${plat.balance}, need ${amount}")

    seller_wallet = Wallet.for_user(seller)
    plat.balance -= amount
    plat.save(update_fields=["balance", "updated_at"])
    seller_wallet.balance += amount
    seller_wallet.save(update_fields=["balance", "updated_at"])

    WalletTx.objects.create(
        wallet=plat, kind="escrow_release", amount=amount,
        description=f"Перевод продавцу по заказу #{order.id}",
        order_id=order.id, balance_after=plat.balance,
    )
    WalletTx.objects.create(
        wallet=seller_wallet, kind="escrow_release", amount=amount,
        description=f"Поступление по заказу #{order.id}",
        order_id=order.id, balance_after=seller_wallet.balance,
    )
    return {"ok": True, "amount": float(amount), "to": seller.username}


def refund_to_buyer(*, order, buyer, amount=None) -> dict:
    """Возврат эскроу → buyer. Делегирует активному engine."""
    if amount is None:
        amount = escrow_balance_for_order(order.id)
    amount = Decimal(str(amount))
    if amount <= 0:
        return {"ok": False, "reason": "ничего не удержано", "amount": 0}
    from .payments_engines import get_engine
    return get_engine().refund_to_buyer(order=order, buyer=buyer, amount=amount)


@transaction.atomic
def _wallet_refund_to_buyer(*, order, buyer, amount: Decimal) -> dict:
    plat = get_platform_wallet()
    if plat.balance < amount:
        raise InsufficientEscrow(f"escrow has ${plat.balance}, need ${amount}")

    buyer_wallet = Wallet.for_user(buyer)
    plat.balance -= amount
    plat.save(update_fields=["balance", "updated_at"])
    buyer_wallet.balance += amount
    buyer_wallet.save(update_fields=["balance", "updated_at"])

    WalletTx.objects.create(
        wallet=plat, kind="escrow_refund", amount=amount,
        description=f"Возврат покупателю по заказу #{order.id}",
        order_id=order.id, balance_after=plat.balance,
    )
    WalletTx.objects.create(
        wallet=buyer_wallet, kind="escrow_refund", amount=amount,
        description=f"Возврат по заказу #{order.id}",
        order_id=order.id, balance_after=buyer_wallet.balance,
    )
    return {"ok": True, "amount": float(amount), "to": buyer.username}


# ── Аналитика эскроу ─────────────────────────────────────────

def escrow_balance_for_order(order_id: int) -> Decimal:
    """Hold − release − refund на платформе для конкретного заказа."""
    plat = get_platform_wallet()
    txs = WalletTx.objects.filter(wallet=plat, order_id=order_id)
    held = sum((tx.amount for tx in txs if tx.kind == "escrow_hold"), Decimal("0"))
    out = sum(
        (tx.amount for tx in txs if tx.kind in ("escrow_release", "escrow_refund")),
        Decimal("0"),
    )
    return held - out


def escrow_summary() -> dict:
    """Платформенный обзор: кому сколько должны, сколько уже выпустили."""
    plat = get_platform_wallet()
    txs = WalletTx.objects.filter(wallet=plat)
    held = sum((tx.amount for tx in txs if tx.kind == "escrow_hold"), Decimal("0"))
    released = sum((tx.amount for tx in txs if tx.kind == "escrow_release"), Decimal("0"))
    refunded = sum((tx.amount for tx in txs if tx.kind == "escrow_refund"), Decimal("0"))
    # Разбивка по заказам, у которых есть незакрытый hold
    holds = {}
    for tx in txs:
        prev = holds.get(tx.order_id) or Decimal("0")
        if tx.kind == "escrow_hold":
            holds[tx.order_id] = prev + tx.amount
        elif tx.kind in ("escrow_release", "escrow_refund"):
            holds[tx.order_id] = prev - tx.amount
    open_holds = {oid: amt for oid, amt in holds.items() if amt > 0 and oid}
    return {
        "platform_balance": float(plat.balance),
        "total_held_ever": float(held),
        "total_released_ever": float(released),
        "total_refunded_ever": float(refunded),
        "outstanding_balance": float(held - released - refunded),
        "open_holds": {oid: float(amt) for oid, amt in open_holds.items()},
    }


# ── Webhook router (Stripe-style) ────────────────────────────

WEBHOOK_HANDLERS = {}


def register_webhook(event_type: str):
    def decorator(fn):
        WEBHOOK_HANDLERS[event_type] = fn
        return fn
    return decorator


def dispatch_webhook(event: dict) -> dict:
    """Receive а Stripe-like event dict, route to handler.

    Используется как для админ-инспекции, так и для будущего Stripe-моста.
    """
    et = event.get("type") or ""
    handler = WEBHOOK_HANDLERS.get(et)
    if not handler:
        return {"received": True, "handled": False, "reason": f"unknown event {et!r}"}
    try:
        result = handler(event.get("data") or {})
        return {"received": True, "handled": True, "result": result}
    except Exception as e:
        logger.exception("webhook handler %s failed", et)
        return {"received": True, "handled": False, "error": str(e)}


@register_webhook("payment_intent.succeeded")
def _wh_intent_succeeded(data):
    """Демо-хук: можно использовать для re-emit notification."""
    return {"intent_id": data.get("id"), "status": data.get("status")}


@register_webhook("escrow.released")
def _wh_escrow_released(data):
    return {"order_id": data.get("order_id"), "amount": data.get("amount")}


@register_webhook("escrow.refunded")
def _wh_escrow_refunded(data):
    return {"order_id": data.get("order_id"), "amount": data.get("amount")}
