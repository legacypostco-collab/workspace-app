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
from django.utils.translation import gettext as _


# Роли, у которых реферал = $100 с первой покупки приглашённого.
_FLAT_ROLES = {"seller", "operator", "operator_logist", "operator_customs",
               "operator_payment", "admin"}


def record_acceptance(referrer, referred, referrer_role: str, *, code=None):
    """Record the first accepted referral for a user without changing rewards."""
    from django.db import IntegrityError
    from marketplace.models import ReferralAcceptance

    if not (referrer and referred) or referrer.pk == referred.pk:
        return None
    existing = ReferralAcceptance.objects.filter(referred=referred).first()
    if existing:
        return existing
    if code is not None and code.user_id != referrer.pk:
        code = None
    try:
        with transaction.atomic():
            return ReferralAcceptance.objects.create(
                referrer=referrer,
                referred=referred,
                code=code,
                referrer_role=referrer_role or "",
            )
    except IntegrityError:
        return ReferralAcceptance.objects.filter(referred=referred).first()


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
    acceptance = record_acceptance(referrer, referred, role)
    if not acceptance or acceptance.referrer_id != referrer.pk:
        return None
    if role == "operator_manager":
        return None  # KAM — отдельная механика (CRM + резидуальные бонусы)

    if role == "buyer":
        kind = "buyer_discount"
        # buyer_discount — один на пригласившего; referred не входит в uniq-ключ.
        obj, _created = ReferralReward.objects.get_or_create(
            referrer=referrer, kind="buyer_discount",
            defaults={
                "referred": referred,
                "referrer_role": role,
                "amount": Decimal(ReferralReward.FLAT_AMOUNT_USD),
                "note": _("−$100 на первый заказ — зачёт при пополнении депозита"),
            },
        )
        return obj

    # Продавец / оператор / прочие (кроме KAM) → $100 с первой покупки приглашённого.
    # Анти-абуз:
    #  (1) self-referral исключён в accept_referral (user.id != ref_uid);
    #  (2) ПЕРВЫЙ-ТАЧ — один пригласивший на приглашённого: если он уже
    #      закреплён за кем-то другим, второй награды не получает (иначе один
    #      заказ приглашённого оплачивал бы $100 каждому, кто кинул ему ссылку);
    #  (3) только НОВЫЙ клиент: нельзя «привести» того, кто уже оплачивал заказы
    #      (награда — за приведённого нового покупателя, а не за чужого старого).
    from marketplace.models import Order
    _PAID = ("reserve_paid", "mid_paid", "paid", "final_paid")
    existing = (ReferralReward.objects
                .filter(referred=referred, kind="flat_first_order")
                .first())
    if existing:
        # уже закреплён: за этим же пригласившим → идемпотентно вернём;
        # за другим → отказ (без мульти-награды за одного приглашённого).
        return existing if existing.referrer_id == referrer.id else None
    if Order.objects.filter(buyer=referred, payment_status__in=_PAID).exists():
        return None
    obj, _created = ReferralReward.objects.get_or_create(
        referrer=referrer, referred=referred, kind="flat_first_order",
        defaults={
            "referrer_role": role,
            "amount": Decimal(ReferralReward.FLAT_AMOUNT_USD),
            "note": _("$100 с первой покупки приглашённого"),
        },
    )
    return obj


def _invitee_is_real(u) -> bool:
    """Приглашённый — настоящий клиент: прошёл KYB-верификацию ИЛИ реально
    оплачивал заказы. Отсекает self-grant через throwaway-аккаунты."""
    if not u:
        return False
    try:
        from marketplace.models import CompanyVerification
        if CompanyVerification.objects.filter(user=u, status="verified").exists():
            return True
    except Exception:
        pass
    from marketplace.models import Order
    return Order.objects.filter(
        buyer=u, payment_status__in=("reserve_paid", "mid_paid", "paid", "final_paid"),
    ).exists()


def on_order_reserve_paid(order) -> int:
    """Приглашённый оплатил резерв заказа → зачислить награды пригласившему.

    Срабатывает на ПЕРВОМ заказе приглашённого. Возвращает число зачисленных строк.
    """
    from marketplace.models import ReferralReward
    buyer = getattr(order, "buyer", None)
    if not buyer:
        return 0
    credited = 0
    with transaction.atomic():
        # flat_first_order ($100 продавцу/оператору). Первый-тач: на одного
        # приглашённого начисляем максимум ОДНУ награду (самую раннюю) — защита
        # от мульти-выплаты даже на легаси-данных с несколькими pending-строками.
        rows = (ReferralReward.objects
                .select_for_update()
                .filter(referred=buyer, kind="flat_first_order", status="pending")
                .order_by("created_at")[:1])
        for rw in rows:
            _credit_wallet(
                rw.referrer, rw.amount,
                description=_("Реферальный бонус: первый заказ приглашённого (#%(id)s)") % {"id": order.id},
                order_id=order.id,
            )
            rw.status = "credited"
            rw.credited_at = timezone.now()
            rw.trigger_order = order
            rw.save(update_fields=["status", "credited_at", "trigger_order"])
            credited += 1
        # buyer_discount (−$100 пригласившему-покупателю): приглашённый только что
        # оформил РЕАЛЬНЫЙ заказ → стал настоящим клиентом → можно зачесть скидку
        # (даже если пригласивший ещё не пополнял депозит). Без реальной покупки
        # приглашённого скидка не выдаётся — это убивает self-grant через throwaway.
        bd = (ReferralReward.objects
              .select_for_update()
              .filter(referred=buyer, kind="buyer_discount", status="pending"))
        for rw in bd:
            _credit_wallet(
                rw.referrer, rw.amount,
                description=_("Реферальная скидка −$100 (приглашённый оформил первый заказ #%(id)s)") % {"id": order.id},
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

    Зачисляем ТОЛЬКО если приглашённый — настоящий клиент (KYB-верифицирован или
    реально покупал). Иначе скидка остаётся pending и зачтётся позже (когда
    приглашённый оформит заказ — см. on_order_reserve_paid). Это закрывает
    self-grant: «пригласить» throwaway и просто пополнить депозит уже не сработает.
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
            if not _invitee_is_real(rw.referred):
                continue  # приглашённый ещё не настоящий клиент — скидка ждёт
            _credit_wallet(
                rw.referrer, rw.amount,
                description=_("Реферальная скидка −$100 на первый заказ (зачёт при пополнении)"),
            )
            rw.status = "credited"
            rw.credited_at = timezone.now()
            rw.save(update_fields=["status", "credited_at"])
            credited += 1
    return credited


def summary_for(user) -> dict:
    """Сводка реферальных наград пользователя (для экрана my_referrals)."""
    from marketplace.models import Order, ReferralAcceptance, ReferralCode, ReferralReward

    rows = list(ReferralReward.objects.filter(referrer=user))
    accepted_ids = set(
        ReferralAcceptance.objects.filter(referrer=user)
        .values_list("referred_id", flat=True)
    )
    accepted_ids.update(
        row.referred_id for row in rows if row.referred_id is not None
    )
    paid_statuses = ("reserve_paid", "mid_paid", "paid", "final_paid")
    converted = (
        Order.objects.filter(
            buyer_id__in=accepted_ids,
            payment_status__in=paid_statuses,
        )
        .values("buyer_id")
        .distinct()
        .count()
        if accepted_ids
        else 0
    )
    code = ReferralCode.objects.filter(user=user).only("clicks").first()
    pending = sum(float(r.amount) for r in rows if r.status == "pending")
    credited = sum(float(r.amount) for r in rows if r.status == "credited")
    return {
        "rows": rows,
        "pending": pending,
        "credited": credited,
        "count": len(accepted_ids),
        "converted": converted,
        "clicks": code.clicks if code else 0,
    }
