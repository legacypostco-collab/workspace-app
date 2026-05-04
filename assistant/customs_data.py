"""Reference data for customs / compliance flow.

Простые статичные таблицы:
- HS_CODES — ТН ВЭД ЕАЭС, упрощённый набор для тяжёлой техники / запчастей
- DUTY_RATES — таможенная пошлина по префиксу HS-кода (%)
- VAT_RATES — НДС по стране импорта
- COUNTRY_FEES — фиксированные сборы (broker fee, terminal handling)
- SANCTIONS — простой риск-скоринг по контрагенту/стране
- CERT_REQUIREMENTS — какие сертификаты нужны для группы товаров

Все значения демонстрационные; в проде нужны реальные обновляемые
таблицы (ФТС, EAC реестр, OFAC SDN list).
"""
from __future__ import annotations
from decimal import Decimal


# ── HS-коды ───────────────────────────────────────────────────
# Формат: 4–6 знаков ТН ВЭД ЕАЭС, ключевые слова для поиска
HS_CODES = [
    {"code": "8413.50", "title": "Насосы поршневые объёмные",
     "keywords": ["насос", "pump", "гидронасос", "hydraulic pump"]},
    {"code": "8413.60", "title": "Насосы шестерённые",
     "keywords": ["шестерённый насос", "gear pump"]},
    {"code": "8429.51", "title": "Погрузчики фронтальные",
     "keywords": ["погрузчик", "loader", "wheel loader"]},
    {"code": "8431.41", "title": "Ковши, отвалы, грейферы для строительной техники",
     "keywords": ["ковш", "bucket", "отвал", "blade"]},
    {"code": "8431.43", "title": "Части бурильных машин",
     "keywords": ["буровая", "drill", "коронка"]},
    {"code": "8431.49", "title": "Прочие части строительной техники",
     "keywords": ["запчасть", "part", "rs", "spare"]},
    {"code": "8483.40", "title": "Шестерни и зубчатые передачи",
     "keywords": ["шестерня", "gear", "зубчатая"]},
    {"code": "8482.10", "title": "Подшипники шариковые",
     "keywords": ["подшипник", "bearing"]},
    {"code": "8421.23", "title": "Фильтры масляные / топливные",
     "keywords": ["фильтр", "filter", "oil filter", "fuel filter"]},
    {"code": "4011.20", "title": "Шины пневматические для грузовых машин",
     "keywords": ["шина", "tire", "tyre"]},
    {"code": "8536.50", "title": "Выключатели электрические",
     "keywords": ["выключатель", "switch", "main switch"]},
    {"code": "8537.10", "title": "Пульты управления, щиты",
     "keywords": ["пульт", "панель", "control panel"]},
    {"code": "9026.20", "title": "Датчики давления",
     "keywords": ["датчик давления", "pressure sensor"]},
    {"code": "8412.21", "title": "Гидроцилиндры",
     "keywords": ["гидроцилиндр", "hydraulic cylinder"]},
    {"code": "8409.99", "title": "Прочие части ДВС",
     "keywords": ["двигатель", "engine part", "блок цилиндров"]},
]


# ── Пошлины (%) по префиксу HS-кода ──────────────────────────
# В РФ ставки на запчасти к строительной технике в среднем 0–7.5%.
DUTY_RATES = {
    "8413": Decimal("5.0"),
    "8429": Decimal("0.0"),
    "8431": Decimal("0.0"),  # части тех. машин — преференция
    "8483": Decimal("5.0"),
    "8482": Decimal("8.0"),
    "8421": Decimal("5.0"),
    "4011": Decimal("10.0"),
    "8536": Decimal("5.0"),
    "8537": Decimal("5.0"),
    "9026": Decimal("3.0"),
    "8412": Decimal("0.0"),
    "8409": Decimal("3.0"),
}
DUTY_DEFAULT = Decimal("5.0")


# ── НДС по странам импорта (для нашего демо — РФ) ────────────
VAT_RATES = {
    "RU": Decimal("20.0"),
    "BY": Decimal("20.0"),
    "KZ": Decimal("12.0"),
    "AM": Decimal("20.0"),
    "KG": Decimal("12.0"),
}
VAT_DEFAULT = Decimal("20.0")


# ── Фикс-сборы (broker, terminal) ────────────────────────────
COUNTRY_FEES = {
    "RU": {"broker": Decimal("250"), "terminal": Decimal("180")},
    "BY": {"broker": Decimal("180"), "terminal": Decimal("120")},
    "KZ": {"broker": Decimal("220"), "terminal": Decimal("150")},
}
FEES_DEFAULT = {"broker": Decimal("250"), "terminal": Decimal("180")}


# ── Санкции (упрощённый риск-скоринг) ────────────────────────
# В реальности — OFAC SDN, EU consolidated, UK HMT. Для демо — список
# флагов с уровнями риска (high/medium/low/none) и пометкой.
SANCTIONS = {
    # countries
    "country:KP": {"level": "high", "reason": "OFAC: КНДР · полные санкции"},
    "country:IR": {"level": "high", "reason": "OFAC: Иран · вторичные санкции"},
    "country:SY": {"level": "high", "reason": "OFAC: Сирия"},
    "country:CU": {"level": "medium", "reason": "OFAC: Куба · ограничения"},
    # entities (примеры известных дилеров под санкциями — синтетика)
    "entity:rostec": {"level": "high", "reason": "OFAC SDN: Ростех group"},
    "entity:wagner": {"level": "high", "reason": "OFAC SDN"},
    # dual-use markers
    "category:dual_use_chip": {"level": "medium", "reason": "EAR: dual-use чипы"},
}


# ── Сертификаты по категориям ────────────────────────────────
# Для импорта в РФ: EAC обязателен, ГОСТ для отдельных групп.
CERT_REQUIREMENTS = {
    "8413": ["EAC", "ТР ТС 010/2011"],   # насосы
    "8429": ["EAC", "ТР ТС 010/2011"],   # самоходные машины
    "8431": ["EAC"],                      # части машин
    "8482": ["EAC"],
    "4011": ["EAC", "ТР ТС 018/2011"],
    "8536": ["EAC", "ТР ТС 004/2011"],   # низковольтное оборудование
    "8537": ["EAC", "ТР ТС 004/2011"],
    "9026": ["EAC", "ТР ТС 020/2011"],
}


# ── Helpers ──────────────────────────────────────────────────

def find_hs_codes(query: str, limit: int = 5) -> list[dict]:
    """Простой word-level matcher по описанию + ключевым словам."""
    q = (query or "").strip().lower()
    if not q:
        return []
    words = [w for w in q.split() if len(w) >= 3]
    scored = []
    for hs in HS_CODES:
        haystack = " ".join([hs["title"], " ".join(hs["keywords"])]).lower()
        score = sum(1 for w in words if w in haystack)
        if score:
            scored.append((score, hs))
    scored.sort(key=lambda x: -x[0])
    return [h for _, h in scored[:limit]]


def duty_rate_for(hs_code: str) -> Decimal:
    """Возвращает ставку пошлины по 4-значному префиксу."""
    if not hs_code:
        return DUTY_DEFAULT
    prefix = hs_code.split(".")[0][:4]
    return DUTY_RATES.get(prefix, DUTY_DEFAULT)


def vat_rate_for(country: str) -> Decimal:
    return VAT_RATES.get((country or "RU").upper(), VAT_DEFAULT)


def fees_for(country: str) -> dict:
    return COUNTRY_FEES.get((country or "RU").upper(), FEES_DEFAULT)


def required_certs_for(hs_code: str) -> list[str]:
    if not hs_code:
        return ["EAC"]
    prefix = hs_code.split(".")[0][:4]
    return CERT_REQUIREMENTS.get(prefix, ["EAC"])


def sanctions_check(*, country: str = "", entity: str = "", category: str = "") -> dict:
    """Возвращает {level, reasons[]} — высший risk выигрывает."""
    levels_order = {"high": 3, "medium": 2, "low": 1, "none": 0}
    hits = []
    if country:
        h = SANCTIONS.get(f"country:{country.upper()}")
        if h: hits.append(h)
    if entity:
        h = SANCTIONS.get(f"entity:{entity.lower()}")
        if h: hits.append(h)
    if category:
        h = SANCTIONS.get(f"category:{category.lower()}")
        if h: hits.append(h)
    if not hits:
        return {"level": "none", "reasons": []}
    top = max(hits, key=lambda x: levels_order[x["level"]])
    return {"level": top["level"], "reasons": [h["reason"] for h in hits]}
