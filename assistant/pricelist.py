"""ТЗ: загрузка прайс-листа через чат с AI-маппингом колонок.

Сценарий:
  1. POST /api/assistant/upload-pricelist/ (multipart, file)
     • Читаем заголовки + первые 3 строки.
     • AI предлагает маппинг колонок на стандартные поля платформы.
     • Возвращаем PricelistImport(status='preview') + карточку с формой.

  2. POST /api/assistant/upload-pricelist/<import_id>/commit/
     body: {"mapping": {std_field: source_column}}
     • Детерминированный парсер обходит весь файл по mapping.
     • Upsert по (seller, oem_number) — повторная загрузка не плодит дубли.
     • Сохраняем mapping в PricelistMapping для следующего раза.
     • Возвращаем итог: imported / failed + errors.

  3. POST /api/assistant/upload-pricelist/<import_id>/cancel/
     Отменить превью без импорта.

Стандартные поля платформы:
  oem_number  (обязательное)
  title       (обязательное)
  price       (обязательное)
  currency    (опц., default USD)
  brand       (опц.)
  stock       (опц., default 0)
  moq         (опц., default 1)
  incoterm    (опц., FOB/CIF/DDP, default FOB)
  weight_kg   (опц.)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .actions import ActionResult, _notify, register

logger = logging.getLogger(__name__)


# ── Standard fields ──────────────────────────────────────────────

STD_FIELDS = [
    # (key, label, required, enum_values_or_None)
    # ВСЕ поля required — по требованию пользователя «лучше пусть будут точные,
    # чтобы не было путаницы с номерами». Heuristic подставит только title и
    # weight — остальное seller выбирает руками.
    ("oem_number",        "Артикул (PartNumber)",   True,  None),
    ("cross_number",      "Кросс-номер (CrossNumber)", True, None),
    ("brand",             "Бренд",                   True,  None),
    ("title",             "Название",                True,  None),
    ("stock",             "Остаток (Quantity)",      True,  None),
    ("condition",         "Состояние",               True,  ["ORIGINAL", "OEM", "AFTERMARKET", "REMAN"]),
    ("price_exw",         "Цена EXW",                True,  None),
    ("warehouse_address", "Адрес склада",            True,  None),
    ("price_fob_sea",     "Цена FOB SEA",            True,  None),
    ("price_fob_air",     "Цена FOB AIR",            True,  None),
    ("sea_port",          "Морпорт отправления",     True,  None),
    ("air_port",          "Аэропорт отправления",    True,  None),
    ("weight_kg",         "Вес, кг",                 True,  None),
    ("length_cm",         "Длина, см",               True,  None),
    ("width_cm",          "Ширина, см",              True,  None),
    ("height_cm",         "Высота, см",              True,  None),
    # Опционально: валюта (фикс или из колонки)
    ("currency",          "Валюта",                  False, ["USD", "EUR", "RUB", "CNY"]),
]

REQUIRED_FIELDS = [k for k, _, req, _ in STD_FIELDS if req]


# ── File reading ─────────────────────────────────────────────────

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 МБ (ТЗ)
MIN_COLUMNS = 2          # ТЗ: минимум 2 колонки
MAX_COLUMNS = 100        # ТЗ: максимум 100 колонок
MAX_AI_HEADERS = 20      # ТЗ: AI получает максимум 20 заголовков за раз
MAX_AI_CALLS_PER_DAY = 3  # ТЗ: 3 AI-вызова в день на seller


def _read_xlsx_rows(blob: bytes, max_rows: int | None = None):
    """Iter всех строк xlsx как кортежи. Максимум первый лист."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    sheet = wb.worksheets[0]
    n = 0
    for row in sheet.iter_rows(values_only=True):
        # Прозрачно конвертируем None → ""
        yield tuple("" if v is None else str(v).strip() for v in row)
        n += 1
        if max_rows is not None and n >= max_rows:
            break


def _read_csv_rows(blob: bytes, max_rows: int | None = None):
    """Iter csv/tsv с авто-определением разделителя.

    Терпит mixed line endings (\\r, \\r\\n, \\n) — csv.reader падает с
    «new-line character seen in unquoted field» когда в данных смешаны
    разные newlines. Нормализуем все newlines к \\n перед парсингом.
    Также отрезаем UTF-8 BOM если есть.
    """
    # UTF-8 BOM можно встретить в файлах из Excel
    if blob[:3] == b"\xef\xbb\xbf":
        blob = blob[3:]
    text = blob.decode("utf-8", errors="replace")
    # Нормализация newlines: \\r\\n или \\r → \\n
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        class _D(csv.excel):
            delimiter = ";"
        dialect = _D()
    reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect)
    n = 0
    for row in reader:
        yield tuple((v or "").strip() for v in row)
        n += 1
        if max_rows is not None and n >= max_rows:
            break


def _detect_format(filename: str, blob: bytes) -> str:
    """Определяет формат файла. Magic-байты приоритетнее расширения —
    бывает что seller сохраняет xlsx с расширением .csv (или наоборот).
    """
    # Magic: zip-архив (xlsx) — приоритет над расширением
    if blob[:4] == b"PK\x03\x04":
        return "xlsx"
    # Magic: BIFF-Excel (старый .xls) — у нас не поддерживается, но
    # пометим как xlsx чтобы openpyxl выдал понятную ошибку.
    if blob[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xlsx"
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm") or name.endswith(".xls"):
        return "xlsx"
    if name.endswith(".csv") or name.endswith(".tsv") or name.endswith(".txt"):
        return "csv"
    return "csv"


def _read_preview(filename: str, blob: bytes) -> tuple[list[str], list[list[str]]]:
    """Возвращает (headers, sample_rows[3])."""
    fmt = _detect_format(filename, blob)
    rows = list(
        _read_xlsx_rows(blob, max_rows=4) if fmt == "xlsx"
        else _read_csv_rows(blob, max_rows=4)
    )
    if not rows:
        raise ValueError("File is empty")
    headers = list(rows[0])
    sample = [list(r) for r in rows[1:4]]
    return headers, sample


def _read_all(filename: str, blob: bytes):
    """Iter всех строк (без первой строки = headers)."""
    fmt = _detect_format(filename, blob)
    rows_iter = _read_xlsx_rows(blob) if fmt == "xlsx" else _read_csv_rows(blob)
    first = True
    for row in rows_iter:
        if first:
            first = False
            continue
        yield row


# ── AI mapping ───────────────────────────────────────────────────

def _ai_calls_used_today(seller) -> int:
    """Сколько раз seller вызывал AI за последние 24 часа.

    Считаем по PricelistImport.ai_called=True.
    """
    from datetime import timedelta
    from marketplace.models import PricelistImport
    since = timezone.now() - timedelta(hours=24)
    return PricelistImport.objects.filter(
        seller=seller, ai_called=True, created_at__gte=since,
    ).count()


def _smart_mapping(headers: list[str], sample_rows: list[list[str]],
                    seller=None
                    ) -> tuple[dict[str, str], list[str], bool, str]:
    """ТЗ: умная автоматическая загрузка прайса.

      1. Словарь COLUMN_MAP + LearnedColumnSynonym (БД)
      2. Для нераспознанных — один AI-запрос (max 20 заголовков)
      3. Лимит 3 AI-вызова в день на seller
      4. AI-ответы → learn_synonym() в БД
      5. Возвращает (mapping_std, unknown, ai_called, status):
            mapping_std — {std_field: header} для сохранения в commit
            unknown     — заголовки которые AI тоже не распознал
            ai_called   — True если AI вызывался (для аналитики)
            status      — 'ok' / 'quota_exceeded' / 'ai_unavailable'
    """
    from .price_mappings import (
        COLUMN_MAP, CANONICAL_TO_STD,
        match_headers, load_learned_lookup, learn_synonym, normalize,
    )

    learned = load_learned_lookup()
    canonical_map, unknown_headers = match_headers(headers, learned=learned)

    ai_called = False
    status = "ok"
    if unknown_headers:
        # ТЗ: AI получает максимум 20 заголовков за раз
        if len(unknown_headers) > MAX_AI_HEADERS:
            unknown_headers = unknown_headers[:MAX_AI_HEADERS]
        # ТЗ: лимит 3 AI-вызова в день на seller
        if seller is not None:
            used = _ai_calls_used_today(seller)
            if used >= MAX_AI_CALLS_PER_DAY:
                status = "quota_exceeded"
                # AI не вызываем, unknowns остаются как есть
                _build_std_mapping = lambda cmap: {  # noqa: E731
                    CANONICAL_TO_STD[c]: h for c, h in cmap.items()
                    if c in CANONICAL_TO_STD
                }
                return _build_std_mapping(canonical_map), unknown_headers, False, status

        ai_canonical = _ai_resolve_unknowns(unknown_headers, sample_rows,
                                              list(COLUMN_MAP.keys()))
        ai_called = bool(ai_canonical)
        for header, canonical in (ai_canonical or {}).items():
            if canonical not in canonical_map and canonical in COLUMN_MAP:
                canonical_map[canonical] = header
                learn_synonym(canonical, header, source="ai")
                if header in unknown_headers:
                    unknown_headers.remove(header)

    # canonical_map → std_field → header (для STD_FIELDS)
    mapping_std: dict[str, str] = {}
    for canonical, header in canonical_map.items():
        std = CANONICAL_TO_STD.get(canonical)
        if std and std not in mapping_std:
            mapping_std[std] = header

    return mapping_std, unknown_headers, ai_called, status


def _ai_resolve_unknowns(unknown_headers: list[str], sample_rows: list[list[str]],
                          allowed_canonicals: list[str]) -> dict[str, str] | None:
    """Шлёт AI ОДИН запрос только с нераспознанными заголовками.

    Возвращает {original_header: canonical_key} или {}.
    """
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key or not unknown_headers:
        return {}
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
    except Exception:
        return {}

    sample_text = "\n".join(
        " | ".join(str(c)[:30] for c in row) for row in (sample_rows or [])[:3]
    )
    prompt = (
        "Определи назначение колонок прайс-листа поставщика. Допустимые ключи:\n"
        + ", ".join(allowed_canonicals) + "\n\n"
        "Не угадывай — если колонка не подходит ни под один ключ, не включай её "
        "в ответ. Верни ТОЛЬКО JSON формата {\"original_header\": \"canonical_key\"}, "
        "без объяснений.\n\n"
        f"Нераспознанные заголовки: {', '.join(repr(h) for h in unknown_headers)}\n\n"
        f"Примеры строк (для контекста):\n{sample_text}\n\nJSON:"
    )
    try:
        msg = client.messages.create(
            model=getattr(settings, "ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if "```" in text:
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.split("```", 1)[0]
        result = json.loads(text.strip())
        # Фильтр: только реальные unknown headers + только allowed canonicals
        return {h: c for h, c in result.items()
                 if h in unknown_headers and c in allowed_canonicals}
    except Exception:
        logger.exception("AI resolve unknowns failed")
        return {}


def _build_mapped_preview(headers: list[str], sample_rows: list[list[str]],
                            mapping: dict[str, str]) -> dict:
    """Строит «как ляжет в базу» превью первых N строк.

    Возвращает {std_columns: […], rows: [[v, …], …]} — для UI рендеринга
    как table_preview. Применяет mapping, но без коэрсии (Decimal/int) —
    показываем как есть из ячеек.
    """
    # std_field → header → idx
    col_idx: dict[str, int] = {}
    for std, src in (mapping or {}).items():
        if not src or src.startswith("fix:") or src not in headers:
            continue
        col_idx[std] = headers.index(src)
    # Берём std-поля в фикс-порядке STD_FIELDS чтобы UI колонки
    # были в осмысленном порядке.
    std_keys = [k for k, _, _, _ in STD_FIELDS if k in col_idx]
    rows = []
    for row in (sample_rows or [])[:5]:
        rows.append([
            (str(row[col_idx[k]]) if col_idx[k] < len(row) else "")
            for k in std_keys
        ])
    labels = {k: lbl for k, lbl, _, _ in STD_FIELDS}
    return {
        "headers": [labels.get(k, k) for k in std_keys],
        "std_keys": std_keys,
        "rows": rows,
    }


# legacy-обёртка, оставлена ради совместимости с тестами
def _heuristic_mapping(headers: list[str]) -> dict[str, str]:
    """Минимальный legacy-маппер. В новом коде используется _smart_mapping."""
    mapping_std, _, _, _ = _smart_mapping(headers, sample_rows=[])
    # _smart_mapping возвращает только что нашёл по словарю — это может
    # включать любые поля, не только title+weight. По legacy-контракту
    # тут возвращали именно minimal-set. Оставлю как есть — словарь
    # достаточно консервативен.
    return mapping_std


def _ai_mapping(headers: list[str], sample_rows: list[list[str]]) -> dict[str, str]:
    """Спрашивает Claude про маппинг колонок. Если API недоступен — heuristic."""
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return _heuristic_mapping(headers)

    try:
        import anthropic  # noqa: F401
    except ImportError:
        return _heuristic_mapping(headers)

    fields_doc = "\n".join(
        f"  - {k} ({label}){' [REQUIRED]' if req else ''}"
        for k, label, req, enum_v in STD_FIELDS
    )
    sample_text = "\n".join(
        " | ".join(str(c)[:40] for c in row) for row in sample_rows
    )
    prompt = (
        f"Определи маппинг колонок прайс-листа поставщика на стандартные поля "
        f"платформы B2B-маркетплейса запчастей. Верни ТОЛЬКО JSON, без "
        f"объяснений, в формате {{\"std_field\": \"source_column_header\"}}.\n\n"
        f"Стандартные поля:\n{fields_doc}\n\n"
        f"Заголовки прайса:\n{', '.join(repr(h) for h in headers)}\n\n"
        f"Примеры строк:\n{sample_text}\n\n"
        f"JSON:"
    )
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=getattr(settings, "ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Достаём JSON из ответа
        if "```" in text:
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.split("```", 1)[0]
        text = text.strip()
        result = json.loads(text)
        # Проверяем что все значения — реальные заголовки
        return {k: v for k, v in result.items()
                 if k in {f for f, _, _, _ in STD_FIELDS} and v in headers}
    except Exception:
        logger.exception("AI mapping failed, falling back to heuristic")
        return _heuristic_mapping(headers)


# ── Deterministic parse + import ────────────────────────────────

def _coerce_decimal(raw: Any) -> Decimal | None:
    """Извлекает первое число из текста.

    Терпим к: «94,0841350448234 €», «$1,234.56», «1 234,56 RUB»,
    нескольким числам в одной ячейке («94,08 €  9,65 €» → берёт 94.08).
    """
    if raw is None or raw == "":
        return None
    import re as _re
    s = str(raw).strip()
    # Первый числовой кусок (с разделителями тысяч и десятичными)
    m = _re.search(r"-?[\d.,'\s ]+", s)
    if not m:
        return None
    raw_num = m.group(0).strip()
    # Удаляем апострофы (швейц. 1'234) и любые пробелы (включая NBSP)
    raw_num = _re.sub(r"[\s' ]", "", raw_num)
    if not raw_num or raw_num in ("-",):
        return None
    # Эвристика разделителей "," vs "."
    if "," in raw_num and "." in raw_num:
        # Оба знака: последний из них = десятичный, другой = тысячи
        if raw_num.rfind(",") > raw_num.rfind("."):
            cleaned = raw_num.replace(".", "").replace(",", ".")
        else:
            cleaned = raw_num.replace(",", "")
    elif "," in raw_num:
        # Одна запятая, после неё ровно 3 цифры, до неё ≤3 цифр → тысячи
        last = raw_num.rfind(",")
        tail = raw_num[last + 1:]
        head = raw_num[:last].replace(",", "")
        if (raw_num.count(",") == 1 and len(tail) == 3 and tail.isdigit()
                and head.isdigit() and len(head) <= 3):
            cleaned = raw_num.replace(",", "")
        else:
            cleaned = raw_num.replace(",", ".")
    else:
        # Только точка(и) — последняя десятичная, остальные тысячи
        if raw_num.count(".") > 1:
            parts = raw_num.split(".")
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        else:
            cleaned = raw_num
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _coerce_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    import re as _re
    s = _re.sub(r"[^\d]", "", s)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _normalize_currency(raw: str) -> str:
    s = (raw or "").strip().upper().replace("$", "USD").replace("€", "EUR").replace("₽", "RUB")
    if s in {"USD", "EUR", "RUB", "CNY"}:
        return s
    return "USD"


def _normalize_incoterm(raw: str) -> str:
    s = (raw or "").strip().upper()
    if s in {"FOB", "CIF", "DDP"}:
        return s
    return "FOB"


def _import_file(import_obj, mapping: dict[str, str], blob: bytes):
    """Полный детерминированный импорт по подтверждённому маппингу.

    Возвращает (imported, failed, error_details).
    """
    from marketplace.models import Brand, Category, Part

    seller = import_obj.seller
    headers = import_obj.headers
    # mapping: {std_field: source}. Source может быть:
    #   header-name      — берём из колонки
    #   "fix:VALUE"      — фикс. значение (применяется ко всем строкам)
    col_idx: dict[str, int] = {}
    fixed_vals: dict[str, str] = {}
    for fld, val in mapping.items():
        if not val:
            continue
        if isinstance(val, str) and val.startswith("fix:"):
            fixed_vals[fld] = val[4:]
        elif val in headers:
            col_idx[fld] = headers.index(val)

    # Проверка обязательных полей: либо колонка, либо фикс
    missing_required = [
        f for f in REQUIRED_FIELDS
        if f not in col_idx and f not in fixed_vals
    ]
    if missing_required:
        return 0, 0, [{"row": 0, "reason": f"missing required mapping: {missing_required}"}]

    cat = Category.objects.filter(slug="parts").first() or Category.objects.first()
    if not cat:
        cat = Category.objects.create(name="Запчасти", slug="parts")
    generic_brand = Brand.objects.filter(name__iexact="Generic").first() or \
                     Brand.objects.create(name="Generic", slug="generic")

    imported = 0
    created = 0
    updated = 0
    failed = 0
    errors: list[dict] = []

    with transaction.atomic():
        for row_n, row in enumerate(_read_all(import_obj.filename, blob), start=2):
            if not any(c.strip() for c in row):
                continue  # пустая строка

            def get(field):
                # Сначала фикс-значение (применяется ко всем строкам),
                # затем колонка из файла.
                if field in fixed_vals:
                    return fixed_vals[field]
                idx = col_idx.get(field)
                if idx is None or idx >= len(row):
                    return ""
                return str(row[idx]).strip()

            oem = get("oem_number")
            title = get("title")
            price_exw = _coerce_decimal(get("price_exw"))
            if not oem or not title or price_exw is None or price_exw <= 0:
                failed += 1
                if len(errors) < 50:
                    reason = (
                        "no oem" if not oem else
                        "no title" if not title else
                        "bad price_exw"
                    )
                    errors.append({"row": row_n, "oem": oem[:60], "reason": reason})
                continue

            # Brand: ищем по имени, или используем generic
            brand_name = get("brand")
            if brand_name:
                brand = Brand.objects.filter(name__iexact=brand_name).first()
                if not brand:
                    brand = Brand.objects.create(
                        name=brand_name[:200],
                        slug=slugify(brand_name)[:200] or generic_brand.slug,
                    )
            else:
                brand = generic_brand

            stock = _coerce_int(get("stock")) or 0
            currency = _normalize_currency(get("currency"))
            cond_raw = (get("condition") or "").strip().lower()
            condition = ("oem" if "oem" in cond_raw else
                          "reman" if "reman" in cond_raw else
                          "aftermarket" if "after" in cond_raw else
                          "oem")  # ORIGINAL → oem
            cross_number = (get("cross_number") or "")[:500]
            price_fob_sea = _coerce_decimal(get("price_fob_sea")) or Decimal("0")
            price_fob_air = _coerce_decimal(get("price_fob_air")) or Decimal("0")
            warehouse = (get("warehouse_address") or "")[:255]
            sea_port = (get("sea_port") or "")[:120]
            air_port = (get("air_port") or "")[:120]
            weight = _coerce_decimal(get("weight_kg")) or Decimal("0.5")
            length = _coerce_decimal(get("length_cm")) or Decimal("1.0")
            width = _coerce_decimal(get("width_cm")) or Decimal("1.0")
            height = _coerce_decimal(get("height_cm")) or Decimal("1.0")

            try:
                _obj, was_created = Part.objects.update_or_create(
                    seller=seller, oem_number__iexact=oem,
                    defaults={
                        "title": title[:255],
                        "oem_number": oem[:100],
                        "slug": slugify(f"{oem}-{seller.username}")[:280],
                        "price": price_exw,
                        "currency": currency,
                        "stock_quantity": stock,
                        "condition": condition,
                        "cross_numbers": cross_number,
                        "price_fob_sea": price_fob_sea,
                        "price_fob_air": price_fob_air,
                        "warehouse_address": warehouse,
                        "sea_port": sea_port,
                        "air_port": air_port,
                        "gross_weight_kg": weight,
                        "length_cm": length,
                        "width_cm": width,
                        "height_cm": height,
                        "category": cat,
                        "brand": brand,
                        "is_active": True,
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1
                imported += 1
            except Exception as e:
                failed += 1
                if len(errors) < 50:
                    errors.append({"row": row_n, "oem": oem[:60], "reason": str(e)[:100]})

    return imported, created, updated, failed, errors


# ── HTTP views ───────────────────────────────────────────────────

class PricelistUploadView(APIView):
    """POST /api/assistant/upload-pricelist/  (multipart, field 'file')

    1) читаем headers + 3 строки
    2) AI/heuristic предлагает mapping
    3) сохраняем PricelistImport(status='preview') + ChatMessage с формой
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        from marketplace.models import PricelistImport, PricelistMapping

        f = request.FILES.get("file")
        if not f:
            return Response({"error": "Файл не передан."}, status=400)
        # ТЗ: валидация ДО любой обработки — AI не вызываем впустую
        if f.size > MAX_FILE_BYTES:
            mb = MAX_FILE_BYTES // 1024 // 1024
            return Response(
                {"error": f"Файл слишком большой ({f.size // 1024 // 1024} МБ). Максимум {mb} МБ."},
                status=400,
            )
        blob = f.read()
        try:
            headers, sample = _read_preview(f.name, blob)
        except Exception as e:
            return Response({"error": f"Не удалось прочитать файл: {e}"}, status=400)
        # ТЗ: первая строка не пустая
        if not headers or not any(str(h).strip() for h in headers):
            return Response({"error": "Первая строка пустая — нет заголовков."}, status=400)
        # ТЗ: ≥2 колонки
        non_empty = [h for h in headers if str(h).strip()]
        if len(non_empty) < MIN_COLUMNS:
            return Response({"error": (
                f"В файле слишком мало колонок ({len(non_empty)}). "
                f"Минимум {MIN_COLUMNS}."
            )}, status=400)
        # ТЗ: ≤100 колонок
        if len(headers) > MAX_COLUMNS:
            return Response({"error": (
                f"В файле слишком много колонок ({len(headers)}). "
                f"Максимум {MAX_COLUMNS}."
            )}, status=400)

        # ТЗ: умный маппинг.
        # 1. Если у seller'а есть сохранённый PricelistMapping — берём
        #    его целиком и AI вообще не трогаем (повторная загрузка).
        # 2. Иначе словарь COLUMN_MAP + LearnedColumnSynonym (БД).
        #    Для нераспознанных — один AI-запрос, ответы пишутся в
        #    LearnedColumnSynonym → словарь растёт сам.
        ai_called = False
        unknown: list = []
        smart_status = "ok"
        prev = PricelistMapping.objects.filter(seller=request.user).first()
        if prev and prev.mapping:
            suggested = {k: v for k, v in prev.mapping.items()
                          if v and (v.startswith("fix:") or v in headers)}
        else:
            suggested, unknown, ai_called, smart_status = _smart_mapping(
                headers, sample, seller=request.user,
            )
        # ТЗ: если AI-лимит исчерпан — error для seller'а
        if smart_status == "quota_exceeded":
            return Response({
                "error": (
                    "Лимит распознавания исчерпан. "
                    "Попробуйте завтра или скачайте шаблон."
                ),
            }, status=429)

        # Превью «как ляжет в базу»: первые 5 строк уже отрендеренных по
        # маппингу — seller видит что именно сохранится.
        mapped_preview = _build_mapped_preview(headers, sample, suggested)

        imp = PricelistImport.objects.create(
            seller=request.user,
            filename=f.name[:255],
            headers=headers,
            sample_rows=sample,
            suggested_mapping=suggested,
            ai_called=ai_called,
            status="preview",
        )
        # Сохраняем файл
        imp.file_obj.save(f.name, ContentFile(blob), save=True)

        return Response({
            "import_id": imp.id,
            "filename": f.name,
            "headers": headers,
            "sample_rows": sample,
            "mapped_preview": mapped_preview,  # ТЗ: 5 строк «как ляжет»
            "suggested_mapping": suggested,
            "ai_called": ai_called,            # для аналитики
            "unknown_headers": unknown,        # колонки без маппинга
            "from_saved_mapping": bool(prev and prev.mapping),
            "std_fields": [
                {"key": k, "label": label, "required": req,
                 "enum_values": enum_v or []}
                for k, label, req, enum_v in STD_FIELDS
            ],
        })


class PricelistCommitView(APIView):
    """POST /api/assistant/upload-pricelist/<id>/commit/

    body: {"mapping": {std_field: source_column}}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, import_id):
        from marketplace.models import PricelistImport, PricelistMapping
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)
        if imp.status != "preview":
            return Response({"error": f"already in status {imp.status}"}, status=400)

        mapping = request.data.get("mapping") or {}
        if not isinstance(mapping, dict):
            return Response({"error": "mapping must be object"}, status=400)
        # Валидация обязательных
        for f in REQUIRED_FIELDS:
            if not mapping.get(f):
                return Response(
                    {"error": f"required field '{f}' not mapped"}, status=400,
                )
        # Маппинг ссылается либо на реальный header, либо на «fix:VALUE»
        for fld, val in mapping.items():
            if not val:
                continue
            if isinstance(val, str) and val.startswith("fix:"):
                continue  # фикс. значение
            if val not in imp.headers:
                return Response(
                    {"error": f"unknown column '{val}' for {fld}"}, status=400,
                )

        # Читаем файл
        try:
            with imp.file_obj.open("rb") as fh:
                blob = fh.read()
        except Exception as e:
            return Response({"error": f"file unavailable: {e}"}, status=500)

        imported, created, updated, failed, errors = _import_file(imp, mapping, blob)

        # Сохраняем итог
        imp.final_mapping = mapping
        imp.imported_rows = imported
        imp.created_rows = created
        imp.updated_rows = updated
        imp.failed_rows = failed
        imp.error_details = errors
        imp.status = "imported"
        imp.completed_at = timezone.now()
        imp.save(update_fields=[
            "final_mapping", "imported_rows", "created_rows", "updated_rows",
            "failed_rows", "error_details", "status", "completed_at",
        ])

        # Запоминаем mapping для следующего раза (повторная загрузка
        # пропускает AI и сразу применяет этот маппинг).
        PricelistMapping.objects.update_or_create(
            seller=request.user, defaults={"mapping": mapping},
        )

        return Response({
            "ok": True,
            "import_id": imp.id,
            "imported": imported,
            "created": created,
            "updated": updated,
            "failed": failed,
            "errors_preview": errors[:10],
        })


class PricelistTemplateView(APIView):
    """GET /api/assistant/pricelist-template.csv — шаблон 16 колонок."""
    from rest_framework.permissions import AllowAny
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        from django.http import HttpResponse
        rows = [
            ["PartNumber", "CrossNumber", "Brand", "Name", "Quantity",
             "Condition", "Price_EXW", "WarehouseAddress",
             "Price_FOB_SEA", "Price_FOB_AIR", "SeaPort", "AirPort",
             "Weight", "Length", "Width", "Height"],
            ["561-50-82311", "5615082311", "Komatsu", "BUSHING", "8",
             "ORIGINAL", "100.00", "Shanghai CN",
             "120.00", "150.00", "Yangshan Port", "Pudong Airport",
             "0.5", "10", "5", "5"],
            ["585-33-21240", "5853321240", "Komatsu", "DISC", "14",
             "OEM", "120.00", "Shanghai CN",
             "140.00", "170.00", "Yangshan Port", "Pudong Airport",
             "2.0", "15", "10", "3"],
        ]
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerows(rows)
        resp = HttpResponse(out.getvalue().encode("utf-8-sig"),  # BOM для Excel
                              content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = (
            'attachment; filename="consolidator_pricelist_template.csv"'
        )
        return resp


class PricelistCancelView(APIView):
    """POST /api/assistant/upload-pricelist/<id>/cancel/  — отменить превью."""
    permission_classes = [IsAuthenticated]

    def post(self, request, import_id):
        from marketplace.models import PricelistImport
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)
        if imp.status == "preview":
            imp.status = "cancelled"
            imp.completed_at = timezone.now()
            imp.save(update_fields=["status", "completed_at"])
        return Response({"ok": True, "status": imp.status})


# ── Action для отображения карточки маппинга в чате ───────────────

@register("pricelist_show_errors")
def pricelist_show_errors(params, user, role):
    """Показать список ошибок последнего импорта."""
    from marketplace.models import PricelistImport
    try:
        imp = PricelistImport.objects.get(
            id=int(params.get("import_id") or 0), seller=user,
        )
    except (PricelistImport.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Импорт не найден.")
    if not imp.error_details:
        return ActionResult(text="Ошибок нет 🎉")
    rows = [{
        "label": f"Стр. {e.get('row', '?')} · {e.get('oem', '?')[:30]}",
        "value": e.get("reason", "?"),
    } for e in imp.error_details]
    return ActionResult(
        text=f"❌ Ошибки импорта #{imp.id}: {imp.failed_rows} строк",
        cards=[{"type": "draft", "data": {
            "title": f"Ошибки импорта (показаны первые {len(rows)})",
            "rows": rows,
        }}],
    )


@register("pricelist_history")
def pricelist_history(params, user, role):
    """История загрузок прайса этого seller'а."""
    from marketplace.models import PricelistImport
    items = PricelistImport.objects.filter(seller=user).order_by("-created_at")[:10]
    if not items:
        return ActionResult(
            text="Прайс ещё не загружали.",
            actions=[{"action": "upload_pricelist", "label": "📤 Загрузить",
                      "params": {}}],
        )
    rows = [{
        "label": f"#{i.id} · {i.filename[:40]} · {i.created_at.strftime('%d.%m %H:%M')}",
        "value": (
            f"{i.imported_rows}↑ / {i.failed_rows}✗"
            if i.status == "imported" else i.get_status_display()
        ),
    } for i in items]
    return ActionResult(
        text=f"📋 История загрузок ({len(items)})",
        cards=[{"type": "draft", "data": {
            "title": "История импортов прайса", "rows": rows,
        }}],
        actions=[{"action": "upload_pricelist", "label": "📤 Новая загрузка",
                  "params": {}}],
    )
