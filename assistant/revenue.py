"""ТЗ §15: декомпозиция дохода группы на каждом закрытом заказе.

Структура:
  IT-Платформа: 6% FOB / 8% CIF / 12% DDP
  Экспортёр:    success_fee 5% от завода + логистическая маржа 3-7%
  РФ-агент:     2% от суммы счёта (если оплата в RUB) + $300 customs (опц.)

API:
  generate_revenue_lines(order, *, basis='DDP', payment_currency='USD',
                         we_clear_customs=False, supplier_payable=None)
    → список созданных PlatformRevenueLine

Hook: confirm_delivery (когда деньги уходят продавцу) → генерируем lines.
       Можно вызывать вручную из op_revenue_breakdown action для аудита.
"""
from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


# ТЗ §15.1: процент платформы по базису
BASIS_FEE_PCT = {
    "FOB": Decimal("6"),
    "CIF": Decimal("8"),
    "DDP": Decimal("12"),
    "EXW": Decimal("6"),   # ≈ FOB
    "CIP": Decimal("8"),   # ≈ CIF
}

# Success fee from supplier
SUCCESS_FEE_PCT = Decimal("5")

# РФ-агент 2% если оплата в RUB
RF_AGENT_PCT = Decimal("2")

# Customs если мы оформляем
CUSTOMS_FEE_USD = Decimal("300")


def generate_revenue_lines(
    order, *,
    basis: str = "DDP",
    payment_currency: str = "USD",
    we_clear_customs: bool = False,
    supplier_payable: Decimal | None = None,
):
    """Создаёт PlatformRevenueLine для заказа. Идемпотентно: удаляет
    существующие lines и создаёт заново (для пересчёта).

    Args:
        order: Order
        basis: 'FOB' | 'CIF' | 'DDP' | 'EXW' | 'CIP'
        payment_currency: 'USD' | 'RUB' (RF-agent fee)
        we_clear_customs: bool — мы ли оформляем таможню (+$300)
        supplier_payable: сумма выплачиваемая поставщику (для success_fee).
                          Если None — берётся 100% order.total_amount, что
                          даёт верхнюю оценку.
    """
    from marketplace.models import PlatformRevenueLine
    if not order:
        return []

    basis = (basis or "DDP").upper()
    total = Decimal(str(order.total_amount or 0))
    if total <= 0:
        return []

    # Drop existing
    PlatformRevenueLine.objects.filter(order=order).delete()

    lines = []

    # 1. IT-Платформа FOB/CIF/DDP
    basis_pct = BASIS_FEE_PCT.get(basis, Decimal("8"))
    basis_fee = (total * basis_pct / Decimal("100")).quantize(Decimal("0.01"))
    lines.append(PlatformRevenueLine.objects.create(
        order=order, kind="basis_fee",
        amount=basis_fee, pct=basis_pct, basis=basis,
        note=f"IT-Платформа {basis} {basis_pct}% от ${total:,.0f}",
    ))

    # 2. Логистическая маржа 3-7% (по правилам портов из ТЗ §16)
    # Упрощённо берём 5% (среднее) — реальная логика расчёта в pricing.py
    logistics_pct = Decimal("5")
    logistics_margin = (total * logistics_pct / Decimal("100")).quantize(Decimal("0.01"))
    lines.append(PlatformRevenueLine.objects.create(
        order=order, kind="logistics_margin",
        amount=logistics_margin, pct=logistics_pct,
        note=f"Логистическая маржа {logistics_pct}%",
    ))

    # 3. Success fee 5% от поставщика
    if supplier_payable is None:
        supplier_payable = total
    success_fee = (Decimal(str(supplier_payable)) * SUCCESS_FEE_PCT / Decimal("100")).quantize(Decimal("0.01"))
    lines.append(PlatformRevenueLine.objects.create(
        order=order, kind="success_fee",
        amount=success_fee, pct=SUCCESS_FEE_PCT,
        note=f"5% от ${supplier_payable:,.0f} payable to supplier",
    ))

    # 4. РФ-агент 2% если RUB
    if (payment_currency or "").upper() == "RUB":
        rf_fee = (total * RF_AGENT_PCT / Decimal("100")).quantize(Decimal("0.01"))
        lines.append(PlatformRevenueLine.objects.create(
            order=order, kind="rf_agent",
            amount=rf_fee, pct=RF_AGENT_PCT,
            note="РФ-агент 2% (оплата в RUB)",
        ))

    # 5. Customs $300 фиксированный
    if we_clear_customs:
        lines.append(PlatformRevenueLine.objects.create(
            order=order, kind="customs_fee",
            amount=CUSTOMS_FEE_USD, pct=Decimal("0"),
            note="Customs handling $300 (наша таможня)",
        ))

    # 6. Минусуем volume_discount (если применился) — buyer_volume_discount
    try:
        from .discounts import get_buyer_discount_pct
        if order.buyer:
            disc_pct = get_buyer_discount_pct(order.buyer)
            if disc_pct > 0:
                disc_amount = -(total * disc_pct / Decimal("100")).quantize(Decimal("0.01"))
                lines.append(PlatformRevenueLine.objects.create(
                    order=order, kind="volume_discount",
                    amount=disc_amount, pct=disc_pct,
                    note=f"Auto-discount {disc_pct}% (по обороту)",
                ))
    except Exception:
        logger.exception("volume_discount line failed")

    return lines


def revenue_summary(order):
    """Сумма по компонентам для конкретного заказа."""
    from marketplace.models import PlatformRevenueLine
    lines = list(PlatformRevenueLine.objects.filter(order=order))
    by_kind = {}
    for ln in lines:
        by_kind[ln.kind] = by_kind.get(ln.kind, Decimal("0")) + ln.amount
    total = sum(by_kind.values(), Decimal("0"))
    return {"by_kind": by_kind, "total": total, "lines": lines}
