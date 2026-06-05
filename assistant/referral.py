"""Бизнес-логика реферальных наград (для всех ролей, КРОМЕ KAM).

Связка с моделью marketplace.ReferralReward:
  • record_referral()        — фиксирует pending-награду в момент перехода по
                               реф-ссылке (accept_referral), по роли пригласившего.
  • on_order_reserve_paid()  — триггер «приглашённый оплатил первый резерв» →
                               зачисляет $100 пригласившему (flat_first_order).
  • on_deposit_funded()      — триггер «пригласивший-покупатель пополнил депозит»
                               → зачисляет −$100 на первый заказ (buyer_discount).

Все начисления идемпотентны: статус строки переводится pending → credited под
транзакцией с select_for_update; повторные триггеры — no-op.

KAM (operator_manager) сюда НЕ попадает: его награда — CRM-привязка клиента и
резидуальные начисления (см. assistant/seller_actions.py / operator_bonus.py).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone


# Роли, у которых реферал = $100 с первой покупки приглашённого.
_FLAT_ROLES = {"seller", "operator", "operator_logist", "operator_customs",
               "operator_payments", "admin"}


def _credit_wallet(user, amount: Decimal, *, description: str, order_id=None) -> None:
    """Зачислить сумму на Wallet пользователя + записать WalletTx (под row-lock)."""
    from assistant.models import Wallet, WalletTx
    wallet = Wallet.objects.select_for_update().get(pk=Wallet.for_user(user).pk)
    wallet.balance = (wallet.balance or Decimal("0")) + amount
    wallet.save(update_fields=["balance", "updated_at"])
    WalletTx.objects.create(
        wallet=wallet, kind="topup", amount=amount,
        description=description, order_id=order_id, balance_after=wallet.balance,
    )


def record_referral(referrer, referred, referrer_role: str):
    """Зафиксировать pending-награду пригласившего по его роли.

    Возвращает ReferralReward | None (None — если роль KAM или некорректные данные).
    Идемпотентно: при повторном вызове вернёт уже существующую строку.
    """
    from marketplace.models import ReferralReward
    if not (referrer and referred):
        return None
    role = referrer_role or ""
    if role == "operator_manager":
        return None  # KAM — отдельная механика (CRM + резидуальные бонусы)

    if role == "buyer":
        kind = "buyer_discount"
        # buyer_discount — один на пригласившего; referred не входит в uniq-ключ.
        obj, _ = ReferralReward.objects.get_or_create(
            referrer=referrer, kind="buyer_discount",
            defaults={
                "referred": referred,
                "referrer_role": role,
                "amount": Decimal(ReferralReward.FLAT_AMOUNT_USD),
                "note": "−$100 на первый заказ — зачёт при пополнении депозита",
            },
        )
        return obj

    # Продавец / оператор / прочие (кроме KAM) → $100 с первой покупки приглашённого
    obj, _ = ReferralReward.objects.get_or_create(
        referrer=referrer, referred=referred, kind="flat_first_order",
        defaults={
            "referrer_role": role,
            "amount": Decimal(ReferralReward.FLAT_AMOUNT_USD),
            "note": "$100 с первой покупки приглашённого",
        },
    )
    return obj


def on_order_reserve_paid(order) -> int:
    """Приглашённый оплатил резерв заказа → зачислить flat_first_order пригласившему.

    Срабатывает на ПЕРВОМ заказе приглашённого (по любому заказу — но строка
    одна на пару referrer→referred, и она credited лишь раз). Возвращает число
    зачисленных строк (0/1).
    """
    from marketplace.models import ReferralReward
    buyer = getattr(order, "buyer", None)
    if not buyer:
        return 0
    credited = 0
    with transaction.atomic():
        rows = (ReferralReward.objects
                .select_for_update()
                .filter(referred=buyer, kind="flat_first_order", status="pending"))
        for rw in rows:
            _credit_wallet(
                rw.referrer, rw.amount,
                description=f"Реферальный бонус: первый заказ приглашённого (#{order.id})",
                order_id=order.id,
            )
            rw.status = "credited"
            rw.credited_at = timezone.now()
            rw.trigger_order = order
            rw.save(update_fields=["status", "credited_at", "trigger_order"])
            credited += 1
    return credited


def on_deposit_funded(user) -> int:
    """Пригласивший-покупатель пополнил депозит → зачислить его buyer_discount −$100.

    Реализуем «скидку на первый заказ» как зачисление $100 на кошелёк при
    ближайшем пополнении (в депозитной модели функционально эквивалентно скидке).
    Возвращает число зачисленных строк (0/1).
    """
    from marketplace.models import ReferralReward
    if not user:
        return 0
    credited = 0
    with transaction.atomic():
        rows = (ReferralReward.objects
                .select_for_update()
                .filter(referrer=user, kind="buyer_discount", status="pending"))
        for rw in rows:
            _credit_wallet(
                rw.referrer, rw.amount,
                description="Реферальная скидка −$100 на первый заказ (зачёт при пополнении)",
            )
            rw.status = "credited"
            rw.credited_at = timezone.now()
            rw.save(update_fields=["status", "credited_at"])
            credited += 1
    return credited


def summary_for(user) -> dict:
    """Сводка реферальных наград пользователя (для экрана my_referrals)."""
    from marketplace.models import ReferralReward
    rows = list(ReferralReward.objects.filter(referrer=user))
    pending = sum(float(r.amount) for r in rows if r.status == "pending")
    credited = sum(float(r.amount) for r in rows if r.status == "credited")
    return {
        "rows": rows,
        "pending": pending,
        "credited": credited,
        "count": len(rows),
    }
