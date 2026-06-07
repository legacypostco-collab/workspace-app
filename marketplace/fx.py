"""Конвертация валют → USD (финансовый, ДЕТЕРМИНИРОВАННЫЙ модуль).

Правило платформы:
  • Продавец задаёт цену в своей валюте (USD/EUR/RUB/CNY).
  • Оператор/продавец видят ИСХОДНУЮ валюту.
  • Покупатель ВСЕГДА видит цену в USD по биржевому курсу — для всех товаров.

Курсы кэшируются (12ч) и обновляются из бесплатного API (open.er-api.com,
без ключа). Если API недоступен (напр. фильтрация/сеть) — мягкий фолбэк на
константы, так что конвертация работает всегда. Это НЕ AI — чистый код.
"""
from decimal import Decimal, ROUND_HALF_UP
import logging

logger = logging.getLogger(__name__)

# USD за 1 единицу валюты — фолбэк, если внешний курс недоступен.
# Обновляются рантаймом из API; здесь — разумные средние, чтобы система
# никогда не падала и не показывала бессмыслицу.
_FALLBACK_USD_PER = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.08"),
    "RUB": Decimal("0.0108"),
    "CNY": Decimal("0.139"),
}
_CACHE_KEY = "fx_usd_per_v1"
_CACHE_TTL = 60 * 60 * 12  # 12 часов
_CENT = Decimal("0.01")


def _normalize(cur) -> str:
    return (cur or "USD").strip().upper()[:3]


def _fetch_rates():
    """Тянем актуальные курсы из бесплатного API. None при неудаче (→ фолбэк)."""
    try:
        import json
        import urllib.request
        # rates[X] = сколько X за 1 USD → инвертируем в "USD за 1 X".
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "consolidator-fx/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        per_usd = data.get("rates") or {}
        out = {"USD": Decimal("1")}
        for cur in ("EUR", "RUB", "CNY"):
            v = per_usd.get(cur)
            try:
                if v and float(v) > 0:
                    out[cur] = (Decimal("1") / Decimal(str(v))).quantize(Decimal("0.000001"))
            except Exception:
                pass
        for k, v in _FALLBACK_USD_PER.items():
            out.setdefault(k, v)
        if len(out) >= 2:
            logger.info("fx: rates refreshed from API")
            return out
    except Exception:
        logger.warning("fx: rate fetch failed — using fallback constants")
    return None


def get_rates() -> dict:
    """{currency: USD_per_unit} — из кэша/API, фолбэк — константы."""
    try:
        from django.core.cache import cache
        rates = cache.get(_CACHE_KEY)
        if rates:
            return rates
        rates = _fetch_rates() or dict(_FALLBACK_USD_PER)
        cache.set(_CACHE_KEY, rates, _CACHE_TTL)
        return rates
    except Exception:
        return dict(_FALLBACK_USD_PER)


def rate_to_usd(currency) -> Decimal:
    """Сколько USD стоит 1 единица `currency`."""
    cur = _normalize(currency)
    return get_rates().get(cur, _FALLBACK_USD_PER.get(cur, Decimal("1")))


def to_usd(amount, currency):
    """`amount` в валюте `currency` → Decimal USD (2 знака). None → None."""
    if amount is None or amount == "":
        return None
    cur = _normalize(currency)
    try:
        dec = Decimal(str(amount))
    except Exception:
        return None
    if cur == "USD":
        return dec.quantize(_CENT, ROUND_HALF_UP)
    return (dec * rate_to_usd(cur)).quantize(_CENT, ROUND_HALF_UP)


def to_usd_float(amount, currency):
    """То же, но float (для JSON-карточек). None → None."""
    v = to_usd(amount, currency)
    return float(v) if v is not None else None
