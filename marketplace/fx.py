"""Конвертация валют → USD (финансовый, ДЕТЕРМИНИРОВАННЫЙ модуль).

Правило платформы:
  • Продавец задаёт цену в своей валюте (USD/EUR/RUB/CNY).
  • Оператор/продавец видят ИСХОДНУЮ валюту.
  • Покупатель ВСЕГДА видит цену в USD по биржевому курсу — для всех товаров.

Курсы кэшируются (12ч) и обновляются из бесплатного API (open.er-api.com,
без ключа). Если API недоступен (напр. фильтрация/сеть) — мягкий фолбэк на
константы, так что конвертация работает всегда. Это НЕ AI — чистый код.
"""
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

# USD за 1 единицу валюты — фолбэк, если внешний курс недоступен.
# Обновляются рантаймом из API; здесь — разумные средние, чтобы система
# никогда не падала и не показывала бессмыслицу.
_FALLBACK_USD_PER = {
    "USD": Decimal("1"),
    "AED": Decimal("0.272294"),
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
    """Тянем актуальные курсы из разрешённого внешнего источника."""
    try:
        import json
        import urllib.request

        from assistant.security import safe_outbound_url, urlopen_no_redirect
        # rates[X] = сколько X за 1 USD → инвертируем в "USD за 1 X".
        url = "https://open.er-api.com/v6/latest/USD"
        ok_url, reason = safe_outbound_url(
            url,
            allowed_hosts_setting="FX_ALLOWED_HOSTS",
            allow_private_setting="FX_ALLOW_PRIVATE_IPS",
            allow_insecure_setting="FX_ALLOW_INSECURE_HTTP",
        )
        if not ok_url:
            logger.warning("fx: rate endpoint blocked: %s", reason)
            return None
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "consolidator-fx/1.0"})
        # URL is fixed and checked against FX_ALLOWED_HOSTS immediately above.
        with urlopen_no_redirect(
            req,
            timeout=5,
            allow_private=bool(getattr(settings, "FX_ALLOW_PRIVATE_IPS", False)),
        ) as r:
            raw = r.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            logger.warning("fx: oversized rate response rejected")
            return None
        data = json.loads(raw.decode())
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
        logger.warning("fx: rate fetch failed")
    return None


def get_rates() -> dict:
    """Вернуть курсы; тестовые константы разрешены только в DEBUG."""
    from django.conf import settings

    try:
        from django.core.cache import cache
        rates = cache.get(_CACHE_KEY)
        if rates:
            return rates
        rates = _fetch_rates()
        if not rates:
            if not settings.DEBUG:
                raise RuntimeError("FX rate provider is unavailable")
            rates = dict(_FALLBACK_USD_PER)
        cache.set(_CACHE_KEY, rates, _CACHE_TTL)
        return rates
    except RuntimeError:
        raise
    except Exception as exc:
        if settings.DEBUG:
            return dict(_FALLBACK_USD_PER)
        logger.exception("fx: unable to load rates")
        raise RuntimeError("FX rate provider is unavailable") from exc


def rate_to_usd(currency) -> Decimal:
    """Сколько USD стоит 1 единица `currency`."""
    cur = _normalize(currency)
    # Дирхам ОАЭ привязан к доллару по официальному паритету 3.6725 AED/USD.
    # Фиксированный курс исключает изменение уже выпущенных счетов из-за API.
    if cur == "AED":
        return (Decimal("1") / Decimal("3.6725")).quantize(Decimal("0.000001"))
    rates = get_rates()
    if cur not in rates:
        raise ValueError(f"Unsupported currency: {cur}")
    return rates[cur]


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


def units_per_usd(currency) -> Decimal:
    """Сколько единиц целевой валюты приходится на 1 USD."""
    cur = _normalize(currency)
    if cur == "USD":
        return Decimal("1.0000")
    rate = rate_to_usd(cur)
    if rate <= 0:
        raise ValueError(f"Invalid exchange rate for currency: {cur}")
    return (Decimal("1") / rate).quantize(Decimal("0.0001"), ROUND_HALF_UP)


def from_usd(amount, currency):
    """`amount` в USD -> Decimal в целевой валюте (2 знака)."""
    if amount is None or amount == "":
        return None
    cur = _normalize(currency)
    try:
        dec = Decimal(str(amount))
    except Exception:
        return None
    if cur == "USD":
        return dec.quantize(_CENT, ROUND_HALF_UP)
    return (dec * units_per_usd(cur)).quantize(_CENT, ROUND_HALF_UP)
