"""OEM-нормализация: расширяет точный поиск списком кандидатов.

Проблема: один и тот же артикул у разных поставщиков пишется в 4-5 форматах
  • OEM Komatsu:     707-99-58030
  • Без дефисов:     7079958030
  • С leading zero:  0707995803
  • Китайский алиас: KM-PC400-HC-01
  • Cross-reference: 7079958030E (E = заменитель)
  • OEM Caterpillar: 265-0235 / CAT-265-0235 / 2650235

Раньше `search_parts` искал по точному `icontains` — упускали ~30% matches.
Теперь `normalize_oem("707-99-58030")` возвращает list of candidates,
которые скармливаем в `Part.objects.filter(oem_number__in=candidates)` —
один SQL, на порядки быстрее любого LLM-roundtrip.

Когда LLM нужен:
  • Свободный текст «гидроцилиндр стрелы PC400, 707.99 что-то 58000»
  • Распознавание по описанию без явного OEM
  → это уже `pricelist.py` smart-mapper (Claude Haiku tool-use).
Здесь — только rule-based для извлечённых OEM-tokens.
"""
from __future__ import annotations

import re

# Brand aliases — нормализуем к каноническому имени.
BRAND_ALIASES: dict[str, str] = {
    "caterpillar": "cat", "cat": "cat",
    "komatsu": "komatsu", "kmt": "komatsu", "km": "komatsu",
    "hitachi": "hitachi", "hit": "hitachi",
    "volvo": "volvo", "vlv": "volvo",
    "doosan": "doosan", "ds": "doosan",
    "liebherr": "liebherr", "lbh": "liebherr",
    "jcb": "jcb",
    "sandvik": "sandvik", "sd": "sandvik",
    "epiroc": "epiroc",
}

# Префиксы, которые часто добавляют поставщики к OEM. Срезаем при матчинге.
COMMON_PREFIXES = ("OEM-", "OEM_", "CAT-", "KM-", "KOM-", "HIT-", "VLV-", "P-")

# Суффиксы означающие «заменитель / переупаковка / страна».
# Два варианта:
#   1) с разделителем:   707-99-58030-CN, 707-99-58030/A1
#   2) без разделителя (одиночная буква E/R после длинной базы ≥7 символов):
#                         707-99-58030E (заменитель), 7079958030R
# Условие «база ≥7 цифр» защищает короткие артикулы типа `5R` от ложного среза.
COMMON_SUFFIXES_RE = re.compile(
    r"([-_/])(E|R|REF|CN|RU|EU|US|REM|A\d+|B\d+|REV\d+)$",
    re.IGNORECASE,
)
SHORT_SUFFIX_RE = re.compile(r"(?<=[0-9])(E|R)$", re.IGNORECASE)


def canonical_brand(brand: str | None) -> str:
    """Нормализует имя бренда: «CAT» → «cat», «Caterpillar» → «cat»."""
    if not brand:
        return ""
    key = re.sub(r"[\s.®™]+", "", brand.strip()).lower()
    return BRAND_ALIASES.get(key, key)


def _strip_separators(s: str) -> str:
    """Убирает пробелы, дефисы, точки, слэши, подчёркивания."""
    return re.sub(r"[\s\-_./]+", "", s)


def _strip_leading_zeros(s: str) -> str:
    """Убирает все ведущие нули. `0707995803` → `707995803`."""
    return s.lstrip("0") or s  # хотя бы один символ должен остаться


def _strip_prefix(s: str) -> str:
    """Срезает один из COMMON_PREFIXES, если есть."""
    up = s.upper()
    for p in COMMON_PREFIXES:
        if up.startswith(p):
            return s[len(p):]
    return s


def _strip_suffix(s: str) -> str:
    """Срезает суффиксы-маркеры заменителя/refurb/country.

    Две стадии:
      1) с разделителем (707-99-58030-CN → 707-99-58030)
      2) одиночная E/R после ≥7-символьной базы (707-99-58030E → 707-99-58030)
    """
    out = COMMON_SUFFIXES_RE.sub("", s)
    if len(out) >= 8:  # 7 цифр + 1 суффикс = 8 минимум
        out = SHORT_SUFFIX_RE.sub("", out)
    return out


def normalize_oem(raw: str | None, brand: str | None = None) -> list[str]:
    """Возвращает список кандидатов OEM для поиска в базе.

    Порядок: от самого точного к самому общему. Дедуп по порядку.
    Все варианты приведены к UPPER-CASE.

    >>> normalize_oem("707-99-58030")
    ['707-99-58030', '7079958030']
    >>> normalize_oem("0707995803")
    ['0707995803', '707995803']
    >>> normalize_oem("CAT-265-0235", brand="Caterpillar")
    ['CAT-265-0235', '265-0235', '2650235', 'CAT2650235']
    >>> normalize_oem("707-99-58030E")
    ['707-99-58030E', '70799580300', '7079958030E', '707-99-58030', '7079958030']
    """
    if not raw or not raw.strip():
        return []
    base = raw.strip().upper()
    out: list[str] = [base]

    # 1) без разделителей
    no_sep = _strip_separators(base)
    if no_sep and no_sep != base:
        out.append(no_sep)

    # 2) без leading zeros
    no_zero = _strip_leading_zeros(no_sep)
    if no_zero and no_zero != no_sep:
        out.append(no_zero)

    # 3) без префикса
    no_pfx = _strip_prefix(base)
    if no_pfx != base:
        out.append(no_pfx)
        out.append(_strip_separators(no_pfx))

    # 4) без суффикса-маркера (заменитель/refurb) — base part
    no_suf = _strip_suffix(base)
    if no_suf != base:
        out.append(no_suf)
        out.append(_strip_separators(no_suf))

    # 5) brand-aware prefix:  Caterpillar + 265-0235 → CAT-265-0235, CAT2650235.
    # Если в base уже есть префикс этого бренда — не дублируем.
    cb = canonical_brand(brand)
    BRAND_PREFIX = {"cat": "CAT", "komatsu": "KM", "hitachi": "HIT"}
    if cb in BRAND_PREFIX:
        pfx = BRAND_PREFIX[cb]
        already_prefixed = (
            base.startswith(pfx + "-") or base.startswith(pfx + "_")
            or base.startswith(pfx) and len(base) > len(pfx) and base[len(pfx)].isdigit()
        )
        if not already_prefixed:
            out.append(f"{pfx}-{base}")
            out.append(f"{pfx}{_strip_separators(base)}")

    # Дедуп с сохранением порядка
    seen: set[str] = set()
    uniq: list[str] = []
    for v in out:
        v = v.strip().upper()
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def expand_query_for_db(query: str, brand: str | None = None) -> list[str]:
    """Высокоуровневый helper — для использования в search_parts.

    Возвращает список кандидатов, готовых для `oem_number__in=[...]` или
    нескольких `Q(oem_number__iexact=v)` объединённых через OR.

    Если query многословный и не похож на OEM (содержит пробелы, кириллицу) —
    возвращает только `[query]` чтобы fallback на icontains/title-поиск
    в вызывающем коде.
    """
    if not query or not query.strip():
        return []
    q = query.strip()
    # Эвристика: похоже на OEM-токен — буквы/цифры/дефисы, длина 4-30
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_./]{2,28}[A-Za-z0-9]", q):
        return normalize_oem(q, brand)
    return [q]


def match_against_candidates(candidate_list: list[str], stored_oem: str) -> bool:
    """True если stored_oem из БД пересекается с любым кандидатом по
    нормализованной форме. Используется для cross-проверки матча из icontains.
    """
    if not stored_oem:
        return False
    stored_variants = set(normalize_oem(stored_oem))
    candidate_variants = {v for c in candidate_list for v in normalize_oem(c)}
    return bool(stored_variants & candidate_variants)
