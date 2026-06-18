"""Скорость ответа продавца на RFQ — РЕАЛЬНАЯ метрика (без выдуманных наград).

Считаем из существующих меток: время от создания RFQ до ПЕРВОЙ котировки продавца
(Quote round_number=1, direction='seller_to_buyer'). Показываем продавцу его медиану
и честный перцентиль относительно платформы — «насколько быстро ты отвечаешь».

Сознательно НЕ обещаем авто-подтверждение/бонус к рейтингу — таких механик в системе
нет, котировки выбираются по цене. Честный посыл: не ответишь вовремя — в выбор не
попадёшь вовсе. Всё fail-soft: ошибка учёта НЕ ломает форму котировки.
"""
import logging

from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

_BENCH_WINDOW_DAYS = 120   # окно выборки для бенчмарка платформы
_BENCH_CAP = 5000          # потолок строк (защита от тяжёлого запроса)
_MIN_SAMPLE = 3            # меньше котировок — «мало данных», перцентиль не считаем
_CACHE_KEY = "seller_speed_benchmark_v1"
_CACHE_TTL = 3600          # 1 час

# Пороги для влияния скорости на РЕЙТИНГ (а рейтинг — на ранжирование в _rank_offers,
# т.е. на продажи). Быстрый ответ → +1 (rfq_response), поздний → −2 (rfq_response_late),
# между — нейтрально. Тюнится здесь.
QUICK_MINUTES = 60         # ≤ 1 ч от создания RFQ — быстрый ответ
LATE_MINUTES = 1440        # > 24 ч — поздний ответ


def response_event_for(rfq, quote):
    """Классифицировать скорость первого ответа → (event_type | None, response_minutes).

      • ≤ QUICK_MINUTES  → 'rfq_response'      (+1 к рейтингу)
      • > LATE_MINUTES   → 'rfq_response_late' (−2 к рейтингу)
      • между            → (None, m)  — рейтинг не трогаем (нейтрально)

    Fail-soft: ошибка → (None, None). Базис — RFQ.created_at (тот же, что в метрике).
    """
    try:
        secs = max(0.0, (quote.created_at - rfq.created_at).total_seconds())
        m = int(round(secs / 60))
        if m <= QUICK_MINUTES:
            return "rfq_response", m
        if m > LATE_MINUTES:
            return "rfq_response_late", m
        return None, m
    except Exception:
        return None, None


def _resp_seconds(q):
    """Время ответа на одну котировку (сек) = Quote.created_at − RFQ.created_at."""
    try:
        return max(0.0, (q.created_at - q.rfq.created_at).total_seconds())
    except Exception:
        return None


def _fmt_duration(minutes):
    """Минуты → человекочитаемо: «18 мин» / «2 ч 5 мин» / «3 дн»."""
    if minutes is None:
        return ""
    m = int(round(minutes))
    if m < 60:
        return _("%(m)s мин") % {"m": m}
    if m < 1440:
        h, mm = divmod(m, 60)
        return _("%(h)s ч %(mm)s мин") % {"h": h, "mm": mm} if mm else _("%(h)s ч") % {"h": h}
    d = m // 1440
    return _("%(d)s дн") % {"d": d}


def rfq_age_label(rfq):
    """«открыт 25 мин назад» / «3 ч назад» / «2 дн назад». Fail-soft → ''."""
    try:
        from django.utils import timezone
        secs = max(0.0, (timezone.now() - rfq.created_at).total_seconds())
        return _("открыт %(dur)s назад") % {"dur": _fmt_duration(secs / 60)}
    except Exception:
        return ""


def seller_response_median_seconds(seller_user, limit=50):
    """Медиана времени ответа продавца (сек) по его последним first-round котировкам.
    → (median_or_None, sample_count). Мало данных (< _MIN_SAMPLE) → (None, n)."""
    from statistics import median
    try:
        from marketplace.models import Quote
        qs = (Quote.objects
              .filter(seller=seller_user, direction="seller_to_buyer", round_number=1)
              .select_related("rfq")
              .order_by("-created_at")[:limit])
        vals = [s for s in (_resp_seconds(q) for q in qs) if s is not None]
        if len(vals) < _MIN_SAMPLE:
            return None, len(vals)
        return median(vals), len(vals)
    except Exception:
        logger.exception("seller_response_median failed")
        return None, 0


def _platform_benchmark():
    """Кэш (1ч): отсортированный список медиан времени ответа по продавцам платформы.
    Нужен для перцентиля «быстрее N%». Любая ошибка → [] (перцентиль просто не покажем)."""
    from django.core.cache import cache
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached
    meds = []
    try:
        from collections import defaultdict
        from datetime import timedelta
        from statistics import median
        from django.utils import timezone
        from marketplace.models import Quote
        since = timezone.now() - timedelta(days=_BENCH_WINDOW_DAYS)
        rows = (Quote.objects
                .filter(direction="seller_to_buyer", round_number=1, created_at__gte=since)
                .select_related("rfq")
                .order_by("-created_at")[:_BENCH_CAP])
        by_seller = defaultdict(list)
        for q in rows:
            s = _resp_seconds(q)
            if s is not None and q.seller_id:
                by_seller[q.seller_id].append(s)
        meds = sorted(median(v) for v in by_seller.values() if len(v) >= _MIN_SAMPLE)
    except Exception:
        logger.exception("platform speed benchmark failed")
        meds = []
    cache.set(_CACHE_KEY, meds, _CACHE_TTL)
    return meds


def seller_speed_standing(seller_user):
    """Честная сводка скорости для UI.

    → {median_min, median_label, count, faster_than_pct, label}.
      • мало данных → median_min=None, label=_('мало данных').
      • faster_than_pct=None, если бенчмарка ещё нет (один продавец и т.п.).
    """
    med, n = seller_response_median_seconds(seller_user)
    if med is None:
        return {"median_min": None, "median_label": "", "count": n,
                "faster_than_pct": None, "label": _("мало данных")}
    median_min = int(round(med / 60))
    dur = _fmt_duration(median_min)
    faster_pct = None
    bench = _platform_benchmark()
    if bench and len(bench) >= 3:
        slower = sum(1 for m in bench if m > med)   # сколько продавцов медленнее нас
        faster_pct = int(round(100 * slower / len(bench)))
    label = (_("быстрее %(pct)s%% продавцов") % {"pct": faster_pct}) if faster_pct is not None else dur
    return {"median_min": median_min, "median_label": dur, "count": n,
            "faster_than_pct": faster_pct, "label": label}
