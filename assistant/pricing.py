"""Конфигуратор цены платформы Consolidator.

Реализует детерминированный расчёт по правилам из ТЗ § «Финансовая модель и
расчёт цены». Никакого AI — только Decimal и формулы.

Структура доходов:
• IT-платформа: 6% FOB / 8% CIF / 12% DDP
• Логистическая маржа: 3–7% по правилам (одна страна/один порт = 3%, ...)
• Доплата RUB-оплаты: +2%
• Доплата за наше таможенное оформление: +$300

Уровни автоматической скидки клиента (по годовому обороту):
• Уровень 1 (>$100k): −2%
• Уровень 2 (>$500k): −4%
• Уровень 3 (>$1M):   −6%
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

D = Decimal


def _q(x) -> Decimal:
    return D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


# ── Базовая комиссия платформы по basis ─────────────────────
BASIS_FEE_PCT = {
    "FOB": D("6"),
    "CIF": D("8"),
    "DDP": D("12"),
    "EXW": D("4"),
    "CIP": D("8"),
}

# ── Уровни скидок по обороту ──────────────────────────────
DISCOUNT_TIERS = [
    (D("1000000"), D("6")),  # > $1M  → 6%
    (D("500000"),  D("4")),
    (D("100000"),  D("2")),
]


@dataclass
class QuoteLine:
    label: str
    amount: Decimal  # может быть отрицательной (скидка)


@dataclass
class Quote:
    base_supplier: Decimal               # цена поставщика (FOB-завод)
    basis: str                            # FOB/CIF/DDP/...
    currency: str                         # USD/RUB
    lines: list[QuoteLine] = field(default_factory=list)
    total: Decimal = D("0")

    def to_dict(self):
        return {
            "base_supplier": float(self.base_supplier),
            "basis": self.basis,
            "currency": self.currency,
            "lines": [{"label": l.label, "amount": float(l.amount)} for l in self.lines],
            "total": float(self.total),
        }


def logistics_margin_pct(supplier_countries: Iterable[str], ports: Iterable[str]) -> Decimal:
    """Логистическая маржа по правилам ТЗ.

    • одна страна, один порт            → 3%
    • одна страна, разные порты         → 4%
    • разные страны, один порт          → 5%
    • разные страны, разные порты, или >2 стран → 7%
    """
    countries = sorted(set(c for c in supplier_countries if c))
    ports_set = sorted(set(p for p in ports if p))
    n_countries = len(countries)
    n_ports = len(ports_set)

    if n_countries >= 3:
        return D("7")
    if n_countries == 1 and n_ports == 1:
        return D("3")
    if n_countries == 1 and n_ports > 1:
        return D("4")
    if n_countries > 1 and n_ports == 1:
        return D("5")
    return D("7")


def annual_discount_pct(annual_turnover_usd: Decimal) -> Decimal:
    """Авто-скидка по годовому обороту."""
    for threshold, pct in DISCOUNT_TIERS:
        if annual_turnover_usd >= threshold:
            return pct
    return D("0")


def calculate_quote(
    base_supplier_price: Decimal,
    *,
    basis: str = "FOB",
    payment_currency: str = "USD",
    we_handle_customs: bool = False,
    supplier_countries: Iterable[str] = ("CN",),
    ports: Iterable[str] = ("Qingdao",),
    annual_turnover_usd: Decimal = D("0"),
    custom_logistics_pct: Decimal | None = None,
) -> Quote:
    """Главная функция конфигуратора. Возвращает Quote с разбивкой и итогом.

    Все числа — Decimal. Округление 2 знака.
    """
    base = D(str(base_supplier_price))
    basis = (basis or "FOB").upper()
    cur = payment_currency.upper()

    q = Quote(base_supplier=base, basis=basis, currency=cur)

    # 1. Цена поставщика
    q.lines.append(QuoteLine("Цена поставщика", base))
    running = base

    # 2. Базовая комиссия платформы по basis
    base_fee_pct = BASIS_FEE_PCT.get(basis, D("8"))
    base_fee = _q(running * base_fee_pct / D("100"))
    q.lines.append(QuoteLine(f"Базис {basis} +{base_fee_pct}%", base_fee))
    running += base_fee

    # 3. Логистическая маржа
    log_pct = D(str(custom_logistics_pct)) if custom_logistics_pct is not None \
              else logistics_margin_pct(supplier_countries, ports)
    log_amount = _q(running * log_pct / D("100"))
    q.lines.append(QuoteLine(f"Логистика +{log_pct}%", log_amount))
    running += log_amount

    # 4. Таможня
    if we_handle_customs:
        q.lines.append(QuoteLine("Таможенное оформление", D("300.00")))
        running += D("300.00")

    # 5. Платёж в RUB
    if cur == "RUB":
        rub_fee = _q(running * D("2") / D("100"))
        q.lines.append(QuoteLine("RUB-оплата +2%", rub_fee))
        running += rub_fee

    # 6. Авто-скидка по обороту
    disc_pct = annual_discount_pct(D(str(annual_turnover_usd)))
    if disc_pct > 0:
        disc_amount = _q(running * disc_pct / D("100")) * D("-1")
        q.lines.append(QuoteLine(f"Скидка по обороту −{disc_pct}%", disc_amount))
        running += disc_amount

    q.total = _q(running)
    return q
