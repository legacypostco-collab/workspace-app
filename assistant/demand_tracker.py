"""Аналитика спроса без предложения.

Бизнес-модель Consolidator после PIVOT 2026-05-26:
  • Мы НЕ рассылаем запросы по поставщикам (это «как все», MANUAL отключён)
  • Работаем только через жёсткий каталожный матчинг (AUTO / SEMI)
  • Если позиции нет в каталоге → фиксируем спрос для отдела развития:
    «Какие OEM чаще всего запрашивают но нет ни у одного поставщика»
  • На основе этой аналитики → ищем и заводим новых поставщиков с прайсами

Это таблица «незакрытого спроса» — топливо для роста каталога.
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


def track_missing_demand(user, queries: Iterable[str], *, rfq_id: int | None = None) -> int:
    """Зафиксировать что юзер искал X позиций, которых нет в каталоге.

    Каждый query → строка в MissingDemandLog (или агрегат, если уже было).

    Возвращает количество записанных позиций.
    """
    from marketplace.models import MissingDemand  # ленивый импорт чтобы избежать circular
    n = 0
    for q in queries:
        q_clean = (q or "").strip()[:128]
        if not q_clean:
            continue
        try:
            # Идемпотентность: одна строка на (oem, day), counter++
            from django.utils import timezone
            today = timezone.now().date()
            obj, created = MissingDemand.objects.get_or_create(
                oem=q_clean.upper(),
                day=today,
                defaults={"buyer": user, "rfq_id": rfq_id, "count": 1},
            )
            if not created:
                obj.count = (obj.count or 0) + 1
                obj.last_rfq_id = rfq_id
                obj.save(update_fields=["count", "last_rfq_id"])
            n += 1
        except Exception:
            logger.exception("track_missing_demand failed for %s", q_clean)
    return n


def top_missing_demand(limit: int = 50, days: int = 30) -> list[dict]:
    """Топ запрашиваемых OEM которые нет в каталоге — за последние N дней.

    Возвращает [{oem, total, unique_buyers, last_seen}, ...]
    Используется в дашборде «развитие каталога» / отчётах.
    """
    from datetime import timedelta
    from django.db.models import Sum, Count, Max
    from django.utils import timezone
    from marketplace.models import MissingDemand
    cutoff = (timezone.now() - timedelta(days=days)).date()
    qs = (
        MissingDemand.objects.filter(day__gte=cutoff)
        .values("oem")
        .annotate(
            total=Sum("count"),
            unique_buyers=Count("buyer", distinct=True),
            last_seen=Max("day"),
        )
        .order_by("-total")[:limit]
    )
    return list(qs)
