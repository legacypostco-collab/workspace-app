"""Калькулятор международной логистики.

Формула:
    volumetric_kg = (L × W × H см) / divisor
        divisor = 5000 для sea, 6000 для air (IATA / FIATA стандарт)
    chargeable_kg = max(actual_kg, volumetric_kg)
    cost = max(chargeable_kg × rate_per_kg, min_charge)

Источник тарифов — модель `LogisticsTariff`. В будущем подключаем API
провайдеров (DHL/FedEx/Maersk) через `LogisticsProvider`-интерфейс.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

DIVISORS = {
    "sea":  Decimal("5000"),
    "air":  Decimal("6000"),
    "auto": Decimal("4000"),
}

# Реальный breakdown базисов поставки (Incoterms 2020):
#
# FOB (Free On Board):
#   покупатель сам забирает товар в порту отгрузки поставщика.
#   Доплат поверх EXW-цены НЕТ — клиент сам организует вывоз.
#
# CIP (Carriage & Insurance Paid To):
#   продавец/маркетплейс довозит до выбранного покупателем порта прибытия.
#   Стоимость = фрахт (port-to-port) + страховка груза (1.5% от cargo,
#   правило Institute Cargo Clauses A, минимум 110% cargo value).
#   Таможню и пошлины платит покупатель.
#
# DDP (Delivered Duty Paid):
#   до двери, all-in. Поверх CIP добавляются:
#     • импортная пошлина (~5% от cargo — реалистичный дефолт для запчастей
#       спецтехники по ЕАЭС; диапазон 0-15% по HS-коду. HS-кодов в каталоге нет,
#       поэтому используем единый дефолт, а не плоские 10% — те завышали DDP);
#     • НДС 22% от (cargo + duty + freight + insurance + last_mile) — закон РФ (с 2026);
#     • last-mile авто внутри страны (~5% от cargo);
#     • таможенное оформление: брокер + терминальная обработка (THC) — ФИКС-сбор
#       на одну декларацию (НЕ % и НЕ на каждую позицию), ставки COUNTRY_FEES.
#       Только DDP — в FOB/CIP покупатель растамаживает сам. В базу НДС не входит
#       (это услуги внутри РФ, к таможенной стоимости импорта не относятся).
INCOTERM_RULES = {
    "FOB": dict(include_freight=False, insurance_pct=Decimal("0"),
                duty_pct=Decimal("0"),  vat_pct=Decimal("0"),
                last_mile_pct=Decimal("0"), clearance=False),
    "CIP": dict(include_freight=True,  insurance_pct=Decimal("0.015"),
                duty_pct=Decimal("0"),  vat_pct=Decimal("0"),
                last_mile_pct=Decimal("0"), clearance=False),
    "DDP": dict(include_freight=True,  insurance_pct=Decimal("0.015"),
                duty_pct=Decimal("0.05"), vat_pct=Decimal("0.22"),
                last_mile_pct=Decimal("0.05"), clearance=True),
}


def calc_incoterm_breakdown(freight: Decimal, cargo_value: Decimal,
                              incoterm: str) -> dict:
    """Разложение надбавки за доставку по базису.

    FOB → 0 (покупатель забирает в порту отгрузки).
    CIP → freight + страховка.
    DDP → CIP + пошлина + НДС + last-mile.
    """
    rules = INCOTERM_RULES.get(incoterm, INCOTERM_RULES["FOB"])
    freight = Decimal(freight or 0)
    cargo = Decimal(cargo_value or 0)
    freight_used = freight if rules["include_freight"] else Decimal("0")
    insurance = (cargo * rules["insurance_pct"]).quantize(Decimal("0.01"))
    duty = (cargo * rules["duty_pct"]).quantize(Decimal("0.01"))
    last_mile = (cargo * rules["last_mile_pct"]).quantize(Decimal("0.01"))
    vat_base = cargo + duty + freight_used + insurance + last_mile
    vat = (vat_base * rules["vat_pct"]).quantize(Decimal("0.01"))
    total = freight_used + insurance + duty + vat + last_mile
    return {
        "freight": freight_used, "insurance": insurance,
        "carriage_ext": Decimal("0"), "duty": duty,
        "vat": vat, "last_mile": last_mile,
        # clearance — ПЕР-ОТПРАВКА фикс-сбор, его прибавляет вызывающий код
        # один раз на декларацию (см. clearance_fee). Здесь всегда 0, чтобы
        # пер-позиционное суммирование не множило сбор на число позиций.
        "clearance": Decimal("0"),
        "total": total.quantize(Decimal("0.01")),
        "incoterm": incoterm,
    }


def clearance_fee(dest_country: str = "RU", incoterm: str = "DDP") -> Decimal:
    """Фикс-сбор за таможенное оформление: брокер + терминальная обработка (THC).

    Берётся ОДИН РАЗ на отправку (одну декларацию), не на каждую позицию, и
    только для DDP — в FOB/CIP покупатель растамаживает сам. Ставки — COUNTRY_FEES
    (customs_data). В базу импортного НДС не входит.
    """
    rules = INCOTERM_RULES.get(incoterm, {})
    if not rules.get("clearance"):
        return Decimal("0")
    from .customs_data import fees_for
    f = fees_for(dest_country)
    return (Decimal(f["broker"]) + Decimal(f["terminal"])).quantize(Decimal("0.01"))


# Legacy alias — старый коэффициент для совместимости
INCOTERM_MARKUP = {"FOB": Decimal("1.00"), "CIP": Decimal("1.07"), "DDP": Decimal("1.18")}


def _country_from_port(port_string: str) -> str:
    """Извлекает ISO-код страны из портовой строки.

    Пример: 'CNNGB · Ningbo-Zhoushan · 宁波 · 🇨🇳 Китай' → 'CN'
    """
    if not port_string:
        return ""
    head = port_string.strip().split()[0]
    if len(head) >= 2 and head[:2].isalpha():
        return head[:2].upper()
    return ""


# ─── Fallback origin для позиций без порта отправления ──────────────────────
# 82% сид-каталога (Komatsu/Epiroc bulk-импорт) загружены БЕЗ sea_port/air_port,
# поэтому фрахт (а с ним CIP/DDP) не считался — покупатель видел «нет тарифа».
# Если порт не указан, выводим страну отправления-источник в порядке:
#   country_of_origin (если реальная страна) → бренд → дефолт CN (китайский хаб).
# Это ОЦЕНКА для матрицы/витрины; продавец подтверждает реальный порт в заказе.
DEFAULT_ORIGIN = "CN"

# Свободный текст country_of_origin (EN/RU) → ISO-2. Только реальные страны;
# 'Unknown'/'' пропускаем (упадём в бренд-фолбэк).
COUNTRY_NAME_ISO = {
    "china": "CN", "p.r. china": "CN", "prc": "CN", "китай": "CN",
    "japan": "JP", "япония": "JP",
    "germany": "DE", "deutschland": "DE", "германия": "DE",
    "usa": "US", "u.s.a.": "US", "united states": "US", "сша": "US",
    "korea": "KR", "south korea": "KR", "republic of korea": "KR", "корея": "KR",
    "italy": "IT", "италия": "IT",
    "spain": "ES", "испания": "ES",
    "india": "IN", "индия": "IN",
    "turkey": "TR", "türkiye": "TR", "турция": "TR",
    "uae": "AE", "united arab emirates": "AE", "оаэ": "AE",
    "netherlands": "NL", "holland": "NL", "нидерланды": "NL",
    "pakistan": "PK", "пакистан": "PK",
    "russia": "RU", "russian federation": "RU", "россия": "RU", "рф": "RU",
}

# Бренд (подстрока, lower) → страна отправления-источник (ISO-2 с тарифом до RU).
# Уверенные сопоставления; остальные бренды (Epiroc/Atlas Copco/Sandvik и пр.)
# падают в DEFAULT_ORIGIN=CN (китайские производственные хабы + тарифы есть).
BRAND_ORIGIN = {
    "komatsu": "JP", "hitachi": "JP", "kobelco": "JP", "kubota": "JP",
    "yanmar": "JP", "isuzu": "JP", "nachi": "JP", "kayaba": "JP", "kyb": "JP",
    "liebherr": "DE", "bomag": "DE", "deutz": "DE", "bosch": "DE",
    "rexroth": "DE", "wirtgen": "DE", "hamm": "DE", "putzmeister": "DE",
    "mahle": "DE", "wabco": "DE",
    "caterpillar": "US", "cummins": "US", "john deere": "US", "terex": "US",
    "doosan": "KR", "hyundai": "KR",
    "carraro": "IT", "berco": "IT", "new holland": "IT", "fpt": "IT",
    "xcmg": "CN", "sany": "CN", "zoomlion": "CN", "shantui": "CN",
    "liugong": "CN", "sdlg": "CN", "weichai": "CN", "shacman": "CN",
    "sinotruk": "CN", "howo": "CN", "lonking": "CN", "lovol": "CN",
}


def _brand_origin(brand: str) -> str:
    b = (brand or "").strip().lower()
    if not b:
        return ""
    for key, iso in BRAND_ORIGIN.items():
        if key in b:
            return iso
    return ""


def fallback_origin_country(part) -> str:
    """Страна отправления-источник для позиции без порта (ISO-2).

    Порядок: country_of_origin (реальная страна) → бренд → DEFAULT_ORIGIN.
    Используется в calc_logistics и в матрице доставки, когда у Part пуст
    и sea_port, и air_port.
    """
    coo = (getattr(part, "country_of_origin", "") or "").strip().lower()
    if coo and coo not in ("unknown", "n/a", "na", "-", "—", "не указано"):
        iso = COUNTRY_NAME_ISO.get(coo)
        if iso:
            return iso
    iso = _brand_origin(getattr(getattr(part, "brand", None), "name", "") or "")
    if iso:
        return iso
    return DEFAULT_ORIGIN


def resolve_origin_code(part, mode: str) -> str:
    """Эффективный origin-код позиции для режима mode: порт, иначе fallback-страна."""
    port_field = "sea_port" if mode == "sea" else "air_port" if mode == "air" else "sea_port"
    origin = (getattr(part, port_field, "") or "").strip()
    code = origin.split()[0] if origin else ""
    return code or fallback_origin_country(part)


def _volumetric_kg(length_cm, width_cm, height_cm, mode: str) -> Decimal:
    """Объёмный вес в кг по габаритам в см."""
    try:
        l = Decimal(length_cm or 0)
        w = Decimal(width_cm or 0)
        h = Decimal(height_cm or 0)
    except Exception:
        return Decimal("0")
    if l <= 0 or w <= 0 or h <= 0:
        return Decimal("0")
    divisor = DIVISORS.get(mode, DIVISORS["sea"])
    return (l * w * h) / divisor


def calc_with_incoterm(part, dest_country: str, mode: str, incoterm: str) -> dict:
    """Расчёт доставки с учётом базиса. Поверх базового тарифа применяет
    markup для CIP/CIF/DDP (страховка, фрахт, таможня).
    """
    r = calc_logistics(part, dest_country, mode)
    if r.get("cost") is None:
        return r
    markup = INCOTERM_MARKUP.get(incoterm, Decimal("1.00"))
    r = dict(r)
    r["cost"] = (r["cost"] * markup).quantize(Decimal("0.01"))
    r["incoterm"] = incoterm
    return r


def calc_logistics(part, dest_country: str, mode: str = "sea") -> dict:
    """Считает стоимость доставки одной позиции.

    Args:
        part: marketplace.Part instance — нужны gross_weight_kg + LxWxH + sea_port/air_port
        dest_country: ISO-код страны назначения (RU, KZ, ...)
        mode: 'sea' или 'air'

    Returns:
        dict с ключами:
            cost (Decimal | None)            — итоговая стоимость USD
            chargeable_kg (Decimal)          — billable вес
            actual_kg (Decimal)              — фактический вес
            volumetric_kg (Decimal)          — объёмный вес
            transit_days (int | None)        — срок доставки
            tariff_id (int | None)           — id применённого тарифа
            origin_port (str)                — порт отправления
            mode (str)                       — sea / air
            source (str)                     — internal / api_dhl / ...
            error (str | None)               — что не получилось
    """
    from marketplace.models import LogisticsTariff

    mode = mode if mode in ("sea", "air") else "sea"
    port_field = "sea_port" if mode == "sea" else "air_port"
    origin = (getattr(part, port_field, "") or "").strip()
    origin_code = (origin.split()[0] if origin else "")
    # Порт не указан → fallback на страну-источник (country_of_origin/бренд/CN),
    # чтобы посчитать фрахт по тарифу страна→страна, а не падать в no_origin_port.
    if not origin_code:
        origin_code = fallback_origin_country(part)
    dest_country = (dest_country or "").upper()[:2]

    actual = Decimal(part.gross_weight_kg or 0)
    vol = _volumetric_kg(part.length_cm, part.width_cm, part.height_cm, mode)
    chargeable = max(actual, vol)

    base = {
        "cost": None,
        "actual_kg": actual,
        "volumetric_kg": vol,
        "chargeable_kg": chargeable,
        "transit_days": None,
        "tariff_id": None,
        "origin_port": origin_code,
        "mode": mode,
        "source": None,
        "error": None,
    }

    if not origin_code:
        base["error"] = "no_origin_port"
        return base
    if not dest_country:
        base["error"] = "no_dest_country"
        return base
    if chargeable <= 0:
        base["error"] = "no_weight_or_dims"
        return base

    # Lookup тариф: точное совпадение по porto-code, иначе fallback на страну-страну.
    origin_country = _country_from_port(origin)
    # Сначала ищем точное совпадение по порту (TRMER), потом fallback на
    # страну отправления (TR). Специфичный тариф порта > общий по стране.
    tariff = LogisticsTariff.objects.filter(
        origin_port__iexact=origin_code,
        dest_country=dest_country, mode=mode, is_active=True,
    ).first()
    if not tariff and origin_country and origin_country != origin_code:
        tariff = LogisticsTariff.objects.filter(
            origin_port__iexact=origin_country,
            dest_country=dest_country, mode=mode, is_active=True,
        ).first()
    if not tariff:
        base["error"] = "no_tariff"
        return base

    cost = chargeable * tariff.rate_per_kg
    if tariff.min_charge and cost < tariff.min_charge:
        cost = Decimal(tariff.min_charge)

    base.update({
        "cost": cost.quantize(Decimal("0.01")),
        "transit_days": tariff.transit_days,
        "tariff_id": tariff.id,
        "source": tariff.source,
    })
    return base


# ── Pluggable provider interface (для будущих API) ──────────────────

class LogisticsProvider(Protocol):
    """Интерфейс провайдера расчёта логистики.

    Дефолтный провайдер использует LogisticsTariff (внутренние тарифы).
    В будущем — DHL/FedEx/Maersk API через тот же интерфейс.
    """
    def quote(self, part, dest_country: str, mode: str) -> dict: ...


class InternalTariffProvider:
    """Дефолтный провайдер на основе модели LogisticsTariff."""
    def quote(self, part, dest_country: str, mode: str = "sea") -> dict:
        return calc_logistics(part, dest_country, mode)
