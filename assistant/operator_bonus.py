"""Бизнес-логика бонусов оператора.

Единая комиссия за закрытую сделку (final release):
  FOB 0.4% · CIP 0.5% · DDP 0.7% от стоимости товара
  min $50 / max $5,000 на сделку
  −50% при подтверждённой вине оператора по рекламации

Жизненный цикл:
  • При release (status=delivered + payment=paid) → создаётся OperatorBonusLine
    со статусом `pending` и release_at = created_at + 14 дней
  • При accept_bonuses_due (cron / ручной trigger) → строки с release_at <= now
    и без открытых рекламаций → released (зачисление в Wallet оператора)
  • При подтверждённой вине оператора по рекламации → withheld или reduced
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone


def compute_bonus_amount(base_amount: Decimal | float, basis: str) -> tuple[Decimal, Decimal]:
    """Вернуть (rate_pct, amount) по правилам Incoterm.

    base_amount — стоимость товара (без логистики/таможни), USD
    basis       — 'FOB' | 'CIP' | 'DDP' (DDP-style)
    """
    from marketplace.models import OperatorBonusLine as _OBL
    basis_up = (basis or "").upper()
    # Маппим CIF→CIP, EXW→FOB для совместимости с PlatformRevenueLine
    if basis_up == "CIF":
        basis_up = "CIP"
    if basis_up == "EXW":
        basis_up = "FOB"
    rate = _OBL.RATE_BY_BASIS.get(basis_up, 0.4)  # дефолт FOB
    base = Decimal(str(base_amount or 0))
    amt = base * Decimal(str(rate)) / Decimal("100")
    # Применяем min/max
    if amt < Decimal(_OBL.MIN_BONUS_USD):
        amt = Decimal(_OBL.MIN_BONUS_USD)
    if amt > Decimal(_OBL.MAX_BONUS_USD):
        amt = Decimal(_OBL.MAX_BONUS_USD)
    return Decimal(str(rate)), amt.quantize(Decimal("0.01"))


def create_bonus_on_release(order) -> "OperatorBonusLine | None":
    """Создать pending-бонус оператору при закрытии сделки.

    Идемпотентно: если бонус уже есть — возвращает существующий.
    Не создаёт если нет assigned_operator или базис не определён.
    """
    from marketplace.models import OperatorBonusLine
    # Уже есть?
    existing = OperatorBonusLine.objects.filter(order=order).first()
    if existing:
        return existing
    op = getattr(order, "assigned_operator", None)
    if not op:
        return None
    basis = getattr(order, "incoterm", None) or "FOB"
    base_amount = float(order.total_amount or 0)
    if base_amount <= 0:
        return None
    rate, amount = compute_bonus_amount(base_amount, basis)
    return OperatorBonusLine.objects.create(
        operator=op,
        order=order,
        basis=(basis if basis in ("FOB", "CIP", "DDP") else "FOB"),
        base_amount=Decimal(str(base_amount)),
        rate_pct=rate,
        amount=amount,
        status="pending",
        release_at=timezone.now() + timedelta(days=14),
        note=f"Авто-начисление при release заказа #{order.id}",
    )


def release_due_bonuses() -> int:
    """Перевести pending → released все строки где release_at <= now и
    нет открытых рекламаций. Зачисляет в Wallet оператора.

    Возвращает количество выпущенных строк.
    """
    from marketplace.models import OperatorBonusLine, OrderClaim
    from assistant.models import Wallet, WalletTx
    now = timezone.now()
    qs = OperatorBonusLine.objects.filter(status="pending", release_at__lte=now)
    released_count = 0
    for line in qs:
        # Проверка: нет открытых рекламаций по заказу
        has_open_claim = OrderClaim.objects.filter(
            order=line.order, closed_at__isnull=True,
        ).exists()
        if has_open_claim:
            continue
        # Зачисляем в Wallet оператора
        wallet = Wallet.for_user(line.operator)
        wallet.balance = (wallet.balance or Decimal("0")) + line.amount
        wallet.save(update_fields=["balance"])
        WalletTx.objects.create(
            wallet=wallet,
            amount=line.amount,
            kind="escrow_release",
            description=f"Бонус оператора по заказу #{line.order_id} ({line.basis} {line.rate_pct}%)",
            order_id=line.order_id,
            balance_after=wallet.balance,
        )
        line.status = "released"
        line.released_at = now
        line.save(update_fields=["status", "released_at"])
        released_count += 1
    return released_count


def withhold_bonus(order, *, full: bool = True, reason: str = "") -> bool:
    """Удержать (или уполовинить) бонус при подтверждённой вине оператора.

    full=True  → withheld (полное удержание)
    full=False → reduced  (−50%)
    """
    from marketplace.models import OperatorBonusLine
    line = OperatorBonusLine.objects.filter(order=order, status="pending").first()
    if not line:
        return False
    if full:
        line.status = "withheld"
        line.note = f"Удержан полностью. {reason}".strip()[:200]
    else:
        line.status = "reduced"
        line.amount = (line.amount / Decimal("2")).quantize(Decimal("0.01"))
        line.note = f"Уполовинено за вину оператора. {reason}".strip()[:200]
    line.save(update_fields=["status", "amount", "note"])
    return True
