"""Supplier rating engine — события подбора/исполнения → пересчёт behavioral.

ТЗ §1, §8: рейтинг = 60% внешняя оценка + 40% поведенческая.

Поведенческая высчитывается из SupplierRatingEvent в скользящем окне (90 дней
по умолчанию). Каждое событие приходит с impact_score (+/-), который
суммируется поверх baseline=60. Итог clamp'ится в [0, 100].

Автоматическая смена статуса срабатывает при пересечении порогов 60 / 80
(уже реализовано в UserProfile.recalculate_supplier_rating через save()).

Стандартные impacts (можно переопределить через env / settings):
  rfq_response_quick     +1   ответил на запрос быстро
  rfq_response_late      −2   ответил после нормативного срока
  no_response            −5   не ответил вовсе
  terms_worsened         −1   ухудшил условия на переторжке (поднял цену/срок)
  data_mismatch          −5   подтвердил наличие, по факту нет
  delivery_on_time       +2   успел в срок
  delivery_delay         −5   просрочил отгрузку
  order_cancellation     −10  отменил подтверждённый заказ
  claim_confirmed        −7   рекламация подтверждена
  return                 −5   возврат по вине поставщика

API:
  record_rating_event(seller, event_type, impact_score=None, meta=None) → event
  recalc_behavioral_score(seller, *, window_days=90) → new_score
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)


# Стандартные impact'ы (баллы, +/-) для известных типов событий.
# Можно переопределить per-call через record_rating_event(impact_score=…).
_DEFAULT_IMPACTS = {
    "rfq_response":         Decimal("1"),    # quick response (positive default)
    "rfq_response_late":    Decimal("-2"),
    "no_response":          Decimal("-5"),
    "terms_worsened":       Decimal("-1"),   # поднял цену/срок на переторжке (мягко)
    "data_mismatch":        Decimal("-5"),
    "delivery_on_time":     Decimal("2"),
    "delivery_delay":       Decimal("-5"),
    "order_cancellation":   Decimal("-10"),
    "claim_confirmed":      Decimal("-7"),
    "return":               Decimal("-5"),
    "sandbox_selected":     Decimal("0"),     # informational only
    "risky_selected":       Decimal("0"),     # informational only
    "manual_oem_escalation": Decimal("0"),    # informational only
}


def record_rating_event(seller, *, event_type: str, impact_score=None, meta: dict | None = None):
    """Записать событие → пересчитать behavioral_score → сменить статус если нужно.

    Best-effort: ошибки логируются, не пробрасываются (рейтинг — фоновый процесс).
    """
    if not seller or not seller.is_authenticated:
        return None
    try:
        from marketplace.models import SupplierRatingEvent
        if impact_score is None:
            impact_score = _DEFAULT_IMPACTS.get(event_type, Decimal("0"))
        ev = SupplierRatingEvent.objects.create(
            supplier=seller,
            event_type=event_type,
            impact_score=Decimal(str(impact_score)),
            meta=meta or {},
        )
        recalc_behavioral_score(seller)
        return ev
    except Exception:
        logger.exception("record_rating_event failed for %s/%s", seller, event_type)
        return None


def recalc_behavioral_score(seller, *, window_days: int = 90, baseline: Decimal = Decimal("60")):
    """Пересчитать behavioral_score: baseline + sum(impact в окне). Clamp [0,100].

    Записывает в UserProfile.behavioral_score и вызывает save() — это
    автоматически триггерит recalculate_supplier_rating() (60/40 + status).
    """
    if not seller or not seller.is_authenticated:
        return None
    try:
        from django.db.models import Sum

        from marketplace.models import SupplierRatingEvent, UserProfile

        cutoff = timezone.now() - timedelta(days=window_days)
        agg = SupplierRatingEvent.objects.filter(
            supplier=seller, created_at__gte=cutoff,
        ).aggregate(total=Sum("impact_score"))
        total = agg["total"] or Decimal("0")

        new_score = baseline + Decimal(str(total))
        if new_score < 0:
            new_score = Decimal("0")
        elif new_score > 100:
            new_score = Decimal("100")

        profile = UserProfile.objects.filter(user=seller).first()
        if not profile:
            return None
        profile.behavioral_score = new_score
        profile.save()  # triggers UserProfile.recalculate_supplier_rating()
        return new_score
    except Exception:
        logger.exception("recalc_behavioral_score failed for %s", seller)
        return None


def status_history(seller, limit: int = 20):
    """Возвращает последние N rating-events для аудита."""
    try:
        from marketplace.models import SupplierRatingEvent
        return list(
            SupplierRatingEvent.objects.filter(supplier=seller)
            .order_by("-created_at")[:limit]
        )
    except Exception:
        return []
