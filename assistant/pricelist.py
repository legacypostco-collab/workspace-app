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
    # (key, label, required)
    ("oem_number", "Артикул (OEM)",       True),
    ("title",      "Название",             True),
    ("price",      "Цена",                 True),
    ("currency",   "Валюта",               False),
    ("brand",      "Бренд",                False),
    ("stock",      "Остаток",              False),
    ("moq",        "MOQ",                  False),
    ("incoterm",   "Базис (FOB/CIF/DDP)",  False),
    ("weight_kg",  "Вес, кг",              False),
]

REQUIRED_FIELDS = [k for k, _, req in STD_FIELDS if req]


# ── File reading ─────────────────────────────────────────────────

MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB


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
    """Iter csv/tsv с авто-определением разделителя."""
    text = blob.decode("utf-8", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        class _D(csv.excel):
            delimiter = ";"
        dialect = _D()
    reader = csv.reader(io.StringIO(text), dialect=dialect)
    n = 0
    for row in reader:
        yield tuple((v or "").strip() for v in row)
        n += 1
        if max_rows is not None and n >= max_rows:
            break


def _detect_format(filename: str, blob: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return "xlsx"
    if name.endswith(".csv") or name.endswith(".tsv") or name.endswith(".txt"):
        return "csv"
    # Magic-byte sniff
    if blob[:2] == b"PK":
        return "xlsx"
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

def _heuristic_mapping(headers: list[str]) -> dict[str, str]:
    """Правила по ключевым словам — fallback если AI недоступен."""
    rules = {
        "oem_number": ["артикул", "oem", "sku", "part", "code", "номер", "код"],
        "title":      ["название", "наименование", "title", "name", "описание", "description"],
        "price":      ["цена", "price", "стоимост", "cost"],
        "currency":   ["валюта", "currency", "ccy"],
        "brand":      ["бренд", "brand", "производит", "manufacturer", "make"],
        "stock":      ["остаток", "stock", "наличи", "qty", "количество"],
        "moq":        ["moq", "мин", "minimum", "min order"],
        "incoterm":   ["incoterm", "базис", "условия", "fob", "cif", "ddp"],
        "weight_kg":  ["вес", "weight", "kg", "масса"],
    }
    mapping: dict[str, str] = {}
    used = set()
    for std_key, keywords in rules.items():
        for h in headers:
            if h in used:
                continue
            hl = h.lower()
            if any(k in hl for k in keywords):
                mapping[std_key] = h
                used.add(h)
                break
    return mapping


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
        for k, label, req in STD_FIELDS
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
                 if k in {f for f, _, _ in STD_FIELDS} and v in headers}
    except Exception:
        logger.exception("AI mapping failed, falling back to heuristic")
        return _heuristic_mapping(headers)


# ── Deterministic parse + import ────────────────────────────────

def _coerce_decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip().replace(",", ".")
    # Удаляем валюту-маркеры и пробелы
    import re as _re
    s = _re.sub(r"[^\d\.\-]", "", s)
    if not s:
        return None
    try:
        return Decimal(s)
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
    # mapping: {std_field: column_header}. Нужны индексы по headers.
    col_idx = {fld: headers.index(col) for fld, col in mapping.items()
                if col in headers}

    # Проверка обязательных полей в маппинге
    missing_required = [f for f in REQUIRED_FIELDS if f not in col_idx]
    if missing_required:
        return 0, 0, [{"row": 0, "reason": f"missing required mapping: {missing_required}"}]

    cat = Category.objects.filter(slug="parts").first() or Category.objects.first()
    if not cat:
        cat = Category.objects.create(name="Запчасти", slug="parts")
    generic_brand = Brand.objects.filter(name__iexact="Generic").first() or \
                     Brand.objects.create(name="Generic", slug="generic")

    imported = 0
    failed = 0
    errors: list[dict] = []

    with transaction.atomic():
        for row_n, row in enumerate(_read_all(import_obj.filename, blob), start=2):
            if not any(c.strip() for c in row):
                continue  # пустая строка

            def get(field):
                idx = col_idx.get(field)
                if idx is None or idx >= len(row):
                    return ""
                return str(row[idx]).strip()

            oem = get("oem_number")
            title = get("title")
            price = _coerce_decimal(get("price"))
            if not oem or not title or price is None or price <= 0:
                failed += 1
                if len(errors) < 50:
                    reason = (
                        "no oem" if not oem else
                        "no title" if not title else
                        "bad price"
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
            moq = max(1, _coerce_int(get("moq")) or 1)
            currency = _normalize_currency(get("currency"))
            incoterm = _normalize_incoterm(get("incoterm"))
            weight = _coerce_decimal(get("weight_kg")) or Decimal("0.5")

            try:
                Part.objects.update_or_create(
                    seller=seller, oem_number__iexact=oem,
                    defaults={
                        "title": title[:255],
                        "oem_number": oem[:100],
                        "slug": slugify(f"{oem}-{seller.username}")[:280],
                        "price": price,
                        "currency": currency,
                        "stock_quantity": stock,
                        "moq": moq,
                        "incoterm": incoterm,
                        "gross_weight_kg": weight,
                        "category": cat,
                        "brand": brand,
                        "is_active": True,
                    },
                )
                imported += 1
            except Exception as e:
                failed += 1
                if len(errors) < 50:
                    errors.append({"row": row_n, "oem": oem[:60], "reason": str(e)[:100]})

    return imported, failed, errors


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
            return Response({"error": "file is required"}, status=400)
        if f.size > MAX_FILE_BYTES:
            return Response(
                {"error": f"file too large (>{MAX_FILE_BYTES // 1024 // 1024}MB)"},
                status=400,
            )
        blob = f.read()
        try:
            headers, sample = _read_preview(f.name, blob)
        except Exception as e:
            return Response({"error": f"cannot read file: {e}"}, status=400)
        if not headers:
            return Response({"error": "no headers found"}, status=400)

        # Предложенный маппинг: сначала прошлый сохранённый, иначе AI/heuristic
        prev = PricelistMapping.objects.filter(seller=request.user).first()
        suggested = (
            {k: v for k, v in (prev.mapping or {}).items() if v in headers}
            if prev else {}
        )
        if len(suggested) < len(REQUIRED_FIELDS):
            ai_map = _ai_mapping(headers, sample)
            for k, v in ai_map.items():
                suggested.setdefault(k, v)

        imp = PricelistImport.objects.create(
            seller=request.user,
            filename=f.name[:255],
            headers=headers,
            sample_rows=sample,
            suggested_mapping=suggested,
            status="preview",
        )
        # Сохраняем файл
        imp.file_obj.save(f.name, ContentFile(blob), save=True)

        return Response({
            "import_id": imp.id,
            "filename": f.name,
            "headers": headers,
            "sample_rows": sample,
            "suggested_mapping": suggested,
            "std_fields": [
                {"key": k, "label": label, "required": req}
                for k, label, req in STD_FIELDS
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
        # Маппинг должен ссылаться на реальные headers
        for fld, col in mapping.items():
            if col and col not in imp.headers:
                return Response(
                    {"error": f"unknown column '{col}' for {fld}"}, status=400,
                )

        # Читаем файл
        try:
            with imp.file_obj.open("rb") as fh:
                blob = fh.read()
        except Exception as e:
            return Response({"error": f"file unavailable: {e}"}, status=500)

        imported, failed, errors = _import_file(imp, mapping, blob)

        # Сохраняем итог
        imp.final_mapping = mapping
        imp.imported_rows = imported
        imp.failed_rows = failed
        imp.error_details = errors
        imp.status = "imported"
        imp.completed_at = timezone.now()
        imp.save(update_fields=[
            "final_mapping", "imported_rows", "failed_rows",
            "error_details", "status", "completed_at",
        ])

        # Запоминаем mapping для следующего раза
        PricelistMapping.objects.update_or_create(
            seller=request.user, defaults={"mapping": mapping},
        )

        return Response({
            "ok": True,
            "import_id": imp.id,
            "imported": imported,
            "failed": failed,
            "errors_preview": errors[:10],
        })


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
