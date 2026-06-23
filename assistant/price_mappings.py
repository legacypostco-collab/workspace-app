"""Словарь известных вариантов колонок прайс-листов поставщиков.

ТЗ: «умная автоматическая загрузка прайса». Логика:
  1. Читаем заголовки → нижний регистр + trim.
  2. Сверяем со словарём COLUMN_MAP + LearnedColumnSynonym (БД).
  3. Если все известны — парсим без AI.
  4. Только нераспознанные отправляем в AI одним запросом.
  5. AI-ответы добавляются в LearnedColumnSynonym → словарь растёт сам.

Канонические ключи COLUMN_MAP (8 базовых полей):
  part_number · description · price · currency ·
  weight · hs_code · lead_time · moq

CANONICAL_TO_STD — маппинг канонических ключей в STD_FIELDS платформы
(см. pricelist.py). Несовпадающие просто игнорируются при импорте.
"""
from __future__ import annotations

import re

# ── Словарь канонических полей и их синонимов (RU/EN/ZH/DE) ─────

COLUMN_MAP: dict[str, list[str]] = {
    # 14 канонических полей × 5 языков (EN/RU/ZH/DE/ES) + реальные
    # заголовки из прайсов Epiroc/Sandvik/Komatsu/Liebherr.
    "part_number": [
        # EN — убрал слишком общие «item», «code», «ref» (на Sandvik
        # «ITEM» означало «номер строки», не артикул).
        "part number", "partnumber", "part no", "part no.", "part#",
        "part-number", "item code", "item number", "item no",
        "sku", "article",
        # RU
        "артикул", "номер детали", "код товара",
        "каталожный номер", "каталожный №", "каталожный",
        # ZH — 编码 (код), 编号 (номер), 物料编码 (код материала)
        "件号", "零件号", "零件编号", "编码", "编号", "物料编码", "图号",
        # DE
        "artikelnummer", "artikel-nr", "teilenummer",
        # ES
        "número de pieza", "numero de pieza", "referencia",
        # Реальные заголовки: Epiroc → "Part Number";
        # Sandvik → "PART NO."; Liebherr → "Partnumber"
    ],
    "description": [
        "description", "desc", "name", "title",
        "наименование", "название", "описание",
        "名称", "描述",
        "bezeichnung", "beschreibung",
        "descripción", "descripcion", "nombre",
        # Sandvik → "NAME"
    ],
    "price": [
        "unitprice", "unit price", "price", "cost", "list price",
        "price exw", "exw price", "exworks", "ex works",
        "цена", "стоимость",
        "成本", "价格", "单价",
        "preis", "stückpreis",
        "precio", "coste",
        # Epiroc → "Unitprice"; Sandvik → "unit-price (CNY)"; Komatsu → "成本"
        # marketplace output → "Price_EXW"
        "unit-price", "unit-price (cny)", "unit-price (usd)", "unit-price (eur)",
    ],
    "currency": [
        "currency", "ccy",
        "валюта",
        "货币",
        "währung",
        "moneda", "divisa",
    ],
    "weight": [
        "weight", "unit-weight", "unit weight", "gross weight", "net weight",
        "вес", "масса", "масса кг", "вес кг",
        "重量",
        "gewicht",
        "peso", "peso unitario",
        # Sandvik → "unit-weight"; Liebherr → "Weight"
    ],
    "hs_code": [
        "hs code", "hs-code", "hscode", "harmonized code",
        "тн вэд", "код тн вэд",
        "海关编码",
        "zollnummer", "hs-tarifnummer",
        "código arancelario", "codigo hs",
        # Liebherr → "HS Code"
    ],
    "lead_time": [
        "delivery time", "lead time", "delivery", "lead-time",
        "срок поставки", "поставка",
        "交货期", "交付时间",
        "lieferzeit",
        "tiempo de entrega", "plazo de entrega",
        # Sandvik → "Delivery time"
    ],
    "moq": [
        "moq", "min qty", "minimum", "min order", "minimum order",
        "минимальная партия", "минимум", "мин. заказ",
        "最小订量", "最小起订量",
        "mindestbestellmenge", "mbm",
        "cantidad mínima", "pedido mínimo",
    ],
    "country_of_origin": [
        "country of origin", "origin", "country",
        "страна происхождения", "страна",
        "原产国", "产地",
        "ursprungsland", "herkunftsland",
        "país de origen", "origen",
    ],
    "status": [
        "status", "state", "availability status",
        "статус",
        "状态",
        "status", "verfügbarkeit",
        "estado", "disponibilidad",
    ],
    "replaced_by": [
        "replaced by", "replacement", "successor", "supersedes",
        "заменён на", "замена", "новый артикул",
        "替代件",
        "ersetzt durch", "ersatzteil",
        "reemplazado por", "sustituido por",
        # Liebherr → "Replaced by"
    ],
    "incoterms": [
        "incoterms", "incoterm", "delivery terms", "trade terms",
        "условия поставки", "базис поставки", "инкотермс",
        "贸易条款", "国际贸易术语",
        "lieferbedingungen", "incoterms",
        "incoterms", "términos de entrega",
    ],
    # Дополнительные часто встречающиеся:
    "brand": [
        "brand", "make", "vendor",
        "бренд",
        "品牌",
        "marke",
        "marca",
    ],
    "manufacturer": [
        "manufacturer", "factory", "plant",
        "производитель", "завод", "завод-производитель",
        "制造商", "厂家", "工厂",
        "hersteller", "werk",
        "fabricante", "fábrica",
    ],
    "availability": [
        "availability", "stock status", "in stock",
        "наличие", "доступность",
        "可用性", "库存状态",
        "verfügbarkeit",
        "disponibilidad",
    ],
    "stock": [
        "stock", "qty", "quantity", "in stock", "available",
        "остаток", "наличие", "количество", "кол-во", "кол во",
        "库存", "数量",
        "lager", "bestand",
        "existencias", "cantidad",
        # Sandvik → "Quantity"
    ],
    "condition": [
        "condition", "type", "grade",
        "состояние", "тип",
        "状态",
        "zustand",
        "condición", "estado",
    ],
    "warehouse": [
        "warehouse", "warehouseaddress", "warehouse address",
        "адрес склада", "склад", "адрес",
        "仓库", "仓库地址",
        "lager", "lageradresse",
    ],
    "price_fob_sea": [
        "price fob sea", "price_fob_sea", "fob sea",
        "цена fob море", "цена море",
        "fob 海运", "海运价格",
        "fob seefracht",
    ],
    "price_fob_air": [
        "price fob air", "price_fob_air", "fob air",
        "цена fob авиа", "цена авиа",
        "fob 空运", "空运价格",
        "fob luftfracht",
    ],
    "sea_port": [
        "seaport", "sea port", "port of loading sea",
        "морпорт", "порт",
        "海港", "装运港",
        "seehafen",
    ],
    "air_port": [
        "airport", "air port", "port of loading air",
        "аэропорт",
        "机场", "起飞机场",
        "flughafen",
    ],
    "length": [
        "length", "длина", "长度", "länge",
    ],
    "width": [
        "width", "ширина", "宽度", "breite",
    ],
    "height": [
        "height", "высота", "高度", "höhe",
    ],
    "cross_number": [
        "cross number", "crossnumber", "cross-number", "cross ref", "alternative",
        "кросс-номер", "кросс", "аналог",
        "каталожный номер 2", "编码2",
        "交叉号", "替代件号", "关联件号", "关联零件号",
        "kreuzreferenz",
        "número alternativo", "referencia cruzada",
    ],
}


# Маппинг канонических ключей → полей STD_FIELDS платформы.
# Если канонический ключ не указан здесь — данные игнорируются (но
# заголовок всё равно «распознан», AI его не атакует повторно).
CANONICAL_TO_STD: dict[str, str] = {
    "part_number":   "oem_number",
    "cross_number":  "cross_number",
    "description":   "title",
    "brand":         "brand",
    "manufacturer":  "manufacturer",
    "availability":  "availability",
    "price":         "price_exw",
    "currency":      "currency",
    "stock":         "stock",
    "moq":           "moq",
    "weight":        "weight_kg",
    "length":        "length_cm",
    "width":         "width_cm",
    "height":        "height_cm",
    "condition":     "condition",
    "warehouse":     "warehouse_address",
    "price_fob_sea": "price_fob_sea",
    "price_fob_air": "price_fob_air",
    "sea_port":      "sea_port",
    "air_port":      "air_port",
    # hs_code, lead_time — пока не в STD_FIELDS, но распознаются.
}


# ── Helpers ──────────────────────────────────────────────────────

_NORM_RE = re.compile(r"[\s_\-./()]+")

def normalize(header: str) -> str:
    """Нормализует заголовок: lower + trim + убирает разделители для
    устойчивого matching (например «Part-Number» = «part_number» = «part number»).
    Для словарного matching хранятся уже нормализованные ключи.
    """
    s = (header or "").strip().lower()
    s = _NORM_RE.sub(" ", s).strip()
    return s


# Предкомпилированный «обратный» индекс: normalized synonym → canonical.
def _build_lookup() -> dict[str, str]:
    out = {}
    for canonical, syns in COLUMN_MAP.items():
        for s in syns:
            out[normalize(s)] = canonical
    return out


_LOOKUP: dict[str, str] = _build_lookup()


# Заголовки-«мусор»: порядковый номер строки, примечания. Их НЕ нужно
# маппить ни в одно поле (иначе «№ п/п» 1,2,3… уходит в вес/наличие), но и
# в AI слать не нужно — возвращаем спец-канон "_ignore" (нет в
# CANONICAL_TO_STD → данные отбрасываются, но колонка считается «известной»).
_IGNORE_HEADERS: frozenset[str] = frozenset({
    normalize(x) for x in (
        "№", "№ п/п", "п/п", "пп", "no", "no.", "n", "n.", "#",
        "порядковый номер", "порядковый", "номер строки", "строка",
        "row", "row no", "sr no", "sl no", "s/n", "seq", "item no.",
        "序号", "行号", "примечание", "备注", "note", "notes", "коммент",
        "комментарий", "remark", "remarks",
    )
})


def match_header(header: str, learned: dict[str, str] | None = None) -> str | None:
    """Возвращает канонический ключ для заголовка или None.

    learned: dict нормализованных синонимов из БД (LearnedColumnSynonym).
             Применяется поверх статического словаря — пользователь может
             переопределить значение.
    """
    if not header:
        return None
    n = normalize(header)
    if not n:
        return None
    if learned and n in learned:
        return learned[n]
    if n in _IGNORE_HEADERS:
        return "_ignore"
    if n in _LOOKUP:
        return _LOOKUP[n]
    # «Бренд аналога» / «brand of analog» — это бренд АНАЛОГА (кросс-бренд),
    # а не основной бренд товара. Не матчим как brand, иначе в мультибрендовом
    # прайсе основная колонка «Бренд» проиграет «Бренд аналога».
    _is_analog = ("аналог" in n or "analog" in n or "кросс" in n or "cross" in n)
    # «Лёгкий» fuzzy: подстрочный match на длинных заголовках типа
    # «Komatsu Part Number Code (OEM)» — ищем нашу подстроку.
    for synonym, canonical in _LOOKUP.items():
        if len(synonym) >= 4 and synonym in n:
            if canonical == "brand" and _is_analog:
                continue  # это про аналог, не основной бренд
            return canonical
    return None


def match_headers(headers: list[str], learned: dict[str, str] | None = None
                   ) -> tuple[dict[str, str], list[str]]:
    """Принимает список заголовков, возвращает:
      mapped:  dict {canonical_key → original_header}
      unknown: list заголовков, которые НЕ удалось распознать.

    Если несколько заголовков попадают на один канонический ключ —
    берём первый и не перезаписываем. Остальные игнорируются.
    """
    mapped: dict[str, str] = {}
    unknown: list[str] = []
    for h in headers or []:
        if not h or not str(h).strip():
            continue
        canonical = match_header(h, learned=learned)
        if canonical and canonical not in mapped:
            mapped[canonical] = h
        elif canonical:
            # Уже занято этим каноническим — не unknown
            pass
        else:
            unknown.append(h)
    return mapped, unknown


def load_learned_lookup() -> dict[str, str]:
    """Подгружает LearnedColumnSynonym → {normalized_header → canonical}.

    Вынесено в БД (а не в файл) чтобы не было гонок при многопоточной
    записи и чтобы deploy не сбрасывал накопленный словарь.
    """
    try:
        from marketplace.models import LearnedColumnSynonym
        return {
            normalize(s.header): s.canonical
            for s in LearnedColumnSynonym.objects.all()
        }
    except Exception:
        return {}


def learn_synonym(canonical: str, header: str, source: str = "ai") -> None:
    """Сохраняет в БД новую пару canonical→header. Идемпотентно по
    нормализованному заголовку: повторный learn() не дублирует.
    """
    try:
        from marketplace.models import LearnedColumnSynonym
        n = normalize(header)
        if not n or not canonical:
            return
        # update_or_create — идемпотентность
        LearnedColumnSynonym.objects.update_or_create(
            header_normalized=n,
            defaults={"canonical": canonical, "raw_header": header[:200],
                       "source": source[:20]},
        )
    except Exception:
        # Не валим импорт прайса если что-то с БД
        pass
