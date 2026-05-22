"""ТЗ §4.1: автоматическая система скидок по годовому обороту клиента.

Уровни (из ТЗ — пример, можно настраивать):
  100M ₽    → уровень 1 → 1%
  500M ₽    → уровень 2 → 1.5%
  1B ₽      → уровень 3 → 3%

Рублёвый порог конвертируется в USD по курсу ~90 ₽/$ (упрощённо для демо).
В реальном проде — брать курс ЦБ из настроек.

API:
  recalc_buyer_volume(user, year=None) → BuyerVolumeYearly
  get_buyer_discount_pct(user) → Decimal (0/1/1.5/3)
  get_buyer_level(user) → int (0..3)

Hooks:
  • pay_final → invoke recalc_buyer_volume на seller'ах
  • pay_reserve → не нужно (резерв ещё не закрыл сделку)
  • Cron: daily recalc для всех buyer'ов (оптимизация)
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


# Пороги в USD (RUB / 90 ≈ USD условно)
LEVEL_THRESHOLDS = [
    (3, Decimal("11_000_000"), Decimal("3.00")),   # 1B ₽ → ~$11M → 3%
    (2, Decimal("5_500_000"),  Decimal("1.50")),   # 500M ₽ → ~$5.5M → 1.5%
    (1, Decimal("1_100_000"),  Decimal("1.00")),   # 100M ₽ → ~$1.1M → 1%
    (0, Decimal("0"),          Decimal("0.00")),   # default
]


def _level_for_volume(volume_usd: Decimal) -> tuple[int, Decimal]:
    for level, threshold, discount in LEVEL_THRESHOLDS:
        if volume_usd >= threshold:
            return level, discount
    return 0, Decimal("0.00")


def recalc_buyer_volume(user, year: int | None = None):
    """Пересчитать годовой объём buyer'а и уровень скидки.

    Берёт sum(total_amount) по Order'ам где buyer=user, payment_status in
    ('paid', 'refunded') (refunded считается т.к. уже закрытая сделка),
    created_at в текущем году.
    """
    if not user or not user.is_authenticated:
        return None
    try:
        from marketplace.models import BuyerVolumeYearly, Order
        if year is None:
            year = timezone.now().year

        agg = Order.objects.filter(
            buyer=user, payment_status__in=("paid", "refunded"),
            created_at__year=year,
        ).aggregate(total=Sum("total_amount"))
        volume_usd = Decimal(str(agg["total"] or 0))

        level, discount_pct = _level_for_volume(volume_usd)

        bvy, _ = BuyerVolumeYearly.objects.update_or_create(
            user=user, year=year,
            defaults={
                "volume_usd": volume_usd,
                "level": level,
                "discount_pct": discount_pct,
                "last_recalculated_at": timezone.now(),
            },
        )
        return bvy
    except Exception:
        logger.exception("recalc_buyer_volume failed for %s/%s", user, year)
        return None


def get_buyer_discount_pct(user) -> Decimal:
    """Текущий discount % для buyer'а в этом году.

    Auto-recalc на лету если данных нет.
    """
    if not user or not user.is_authenticated:
        return Decimal("0.00")
    try:
        from marketplace.models import BuyerVolumeYearly
        year = timezone.now().year
        bvy = BuyerVolumeYearly.objects.filter(user=user, year=year).first()
        if not bvy:
            bvy = recalc_buyer_volume(user, year=year)
        return bvy.discount_pct if bvy else Decimal("0.00")
    except Exception:
        return Decimal("0.00")


def get_buyer_level(user) -> int:
    if not user or not user.is_authenticated:
        return 0
    try:
        from marketplace.models import BuyerVolumeYearly
        year = timezone.now().year
        bvy = BuyerVolumeYearly.objects.filter(user=user, year=year).first()
        if not bvy:
            bvy = recalc_buyer_volume(user, year=year)
        return bvy.level if bvy else 0
    except Exception:
        return 0


def apply_volume_discount(amount: Decimal, user) -> dict:
    """Применить volume-discount к сумме.

    Returns:
        {
            'subtotal': Decimal — изначально,
            'discount_pct': Decimal,
            'discount_amount': Decimal,
            'level': int,
            'total': Decimal — после скидки,
        }
    """
    pct = get_buyer_discount_pct(user)
    level = get_buyer_level(user)
    subtotal = Decimal(str(amount))
    discount_amount = (subtotal * pct / Decimal("100")).quantize(Decimal("0.01"))
    return {
        "subtotal": subtotal,
        "discount_pct": pct,
        "discount_amount": discount_amount,
        "level": level,
        "total": subtotal - discount_amount,
    }
