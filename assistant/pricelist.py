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
    # (key, label, required, enum_values_or_None, default_value_or_None)
    # required=True — поле ОБЯЗАТЕЛЬНО должно быть в файле или указано явно.
    # Остальные поля заполняются дефолтами автоматически если не замаплены.
    ("oem_number",        "Артикул (PartNumber)",   True,  None, None),
    ("cross_number",      "Кросс-номер (CrossNumber)", False, None, ""),
    ("brand",             "Бренд",                   False, [
        "Caterpillar", "Komatsu", "Hitachi", "Liebherr", "TEREX",
        "New Holland", "Wirtgen", "Iveco", "HBM-Nobas", "John Deere",
        "Volvo", "JCB", "Bobcat", "BOMAG",
        "Cummins", "Deutz", "Bosch",
        "Atlas Copco", "Epiroc", "Sandvik", "Ingersoll Rand",
        "Hyundai", "Doosan", "Kobelco", "Kubota",
        "XCMG", "FAW", "LiuGong", "Shantui", "Shacman", "SDLG",
        "Weichai", "Sinotruk", "HOWO", "Zoomlion", "Sany",
        "Bosch Rexroth", "Perkins", "Dana", "Carraro", "Denso",
        "Lincoln", "Berco", "ITR", "ETP",
        "Generic",
    ], "Generic"),
    ("title",             "Название",                True,  None, None),
    ("stock",             "Остаток (Quantity)",      False, None, "0"),
    ("condition",         "Состояние",               False, ["ORIGINAL", "OEM", "AFTERMARKET", "REMAN"], "OEM"),
    ("price_exw",         "Цена EXW",                True,  None, None),
    ("warehouse_address", "Адрес склада",            False, None, ""),
    ("price_fob_sea",     "Цена FOB SEA",            False, None, "0"),
    ("price_fob_air",     "Цена FOB AIR",            False, None, "0"),
    ("sea_port",          "Морпорт отправления",     False, None, ""),
    ("air_port",          "Аэропорт отправления",    False, None, ""),
    ("weight_kg",         "Вес, кг",                 False, None, "0.5"),
    ("length_cm",         "Длина, см",               False, None, "1"),
    ("width_cm",          "Ширина, см",              False, None, "1"),
    ("height_cm",         "Высота, см",              False, None, "1"),
    ("currency",          "Валюта",                  False, ["USD", "EUR", "RUB", "CNY"], "USD"),
]

REQUIRED_FIELDS = [k for k, _, req, _, _ in STD_FIELDS if req]

# Дефолты для незамапленных полей
FIELD_DEFAULTS = {k: d for k, _, _, _, d in STD_FIELDS if d is not None}


# ── Formula whitelist engine ────────────────────────────────────

import ast
import operator as _op

_SAFE_OPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.FloorDiv: _op.floordiv,
    ast.Mod: _op.mod,
    ast.USub: _op.neg,
}

def _safe_round(*args):
    if len(args) == 1:
        return Decimal(str(round(float(args[0]))))
    return Decimal(str(round(float(args[0]), int(args[1]))))

_SAFE_FUNCS = {
    "round": _safe_round,
    "min": lambda *a: min(*a),
    "max": lambda *a: max(*a),
    "abs": lambda x: abs(x),
    "int": lambda x: Decimal(int(x)),
    "float": lambda x: Decimal(str(float(x))),
}


def _eval_node(node, variables: dict[str, Any]) -> Any:
    """Рекурсивный evaluator AST-узлов — только арифметика + whitelist функций."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, Decimal)):
            return Decimal(str(node.value))
        return node.value
    if isinstance(node, ast.Name):
        if node.id in variables:
            return variables[node.id]
        raise ValueError(f"Unknown variable: {node.id}")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        op_fn = _SAFE_OPS.get(type(node.op))
        if not op_fn:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(Decimal(str(left)), Decimal(str(right)))
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, variables)
        op_fn = _SAFE_OPS.get(type(node.op))
        if not op_fn:
            raise ValueError(f"Unsupported unary: {type(node.op).__name__}")
        return op_fn(Decimal(str(operand)))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple function calls allowed")
        fn = _SAFE_FUNCS.get(node.func.id)
        if not fn:
            raise ValueError(f"Function not allowed: {node.func.id}")
        args = [_eval_node(a, variables) for a in node.args]
        return fn(*args)
    if isinstance(node, ast.IfExp):
        test = _eval_node(node.test, variables)
        return _eval_node(node.body if test else node.orelse, variables)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, variables)
        for cmp_op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, variables)
            if isinstance(cmp_op, ast.Gt):
                if not (left > right):
                    return False
            elif isinstance(cmp_op, ast.Lt):
                if not (left < right):
                    return False
            elif isinstance(cmp_op, ast.GtE):
                if not (left >= right):
                    return False
            elif isinstance(cmp_op, ast.LtE):
                if not (left <= right):
                    return False
            elif isinstance(cmp_op, ast.Eq):
                if not (left == right):
                    return False
            else:
                raise ValueError(f"Unsupported comparison: {type(cmp_op).__name__}")
            left = right
        return True
    raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def eval_formula(expression: str, variables: dict[str, Any]) -> Any:
    """Безопасное вычисление формулы. Только арифметика + whitelist функций.

    Примеры:
      eval_formula("price * 1.15", {"price": Decimal("100")})  → 115.00
      eval_formula("round(weight * 2.2, 1)", {"weight": Decimal("5")})  → 11.0
      eval_formula("price if price > 0 else 1", {"price": Decimal("0")})  → 1
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid formula syntax: {e}")
    return _eval_node(tree, variables)


def validate_formula(expression: str, available_vars: list[str]) -> tuple[bool, str]:
    """Проверяет формулу на синтаксис и использование доступных переменных."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as e:
        return False, f"Syntax error: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in available_vars and node.id not in _SAFE_FUNCS:
            return False, f"Unknown variable: {node.id}"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id not in _SAFE_FUNCS:
                return False, f"Function not allowed: {node.func.id}"
    return True, ""


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
    """Возвращает (headers, sample_rows[3]).

    Excel часто читает «висячие» колонки с пустыми заголовками после
    реальных данных. Срезаем trailing пустые headers + sample-данные.
    """
    fmt = _detect_format(filename, blob)
    rows = list(
        _read_xlsx_rows(blob, max_rows=4) if fmt == "xlsx"
        else _read_csv_rows(blob, max_rows=4)
    )
    if not rows:
        raise ValueError("File is empty")
    headers = list(rows[0])
    # Срезаем trailing пустые headers (Excel оставляет до десятка
    # пустых ячеек после реальных колонок).
    while headers and not str(headers[-1]).strip():
        headers.pop()
    n_cols = len(headers)
    # Truncate sample-rows до n_cols чтобы dropdown'ы не показывали
    # лишние «Колонка:» без имени.
    sample = [list(r)[:n_cols] for r in rows[1:4]]
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
    """Claude tool use для структурированного маппинга колонок.

    Вместо парсинга свободного текста JSON — Claude возвращает
    структурированный ответ через tool_use (propose_mapping).
    Fallback на текстовый JSON если tool use не сработал.
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

    propose_mapping_tool = {
        "name": "propose_mapping",
        "description": (
            "Предложить маппинг колонок прайс-листа поставщика "
            "на канонические поля платформы."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mappings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_header": {
                                "type": "string",
                                "description": "Оригинальный заголовок колонки из файла",
                            },
                            "canonical_key": {
                                "type": "string",
                                "enum": allowed_canonicals,
                                "description": "Каноническое поле платформы",
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "Уверенность в маппинге (0-1)",
                            },
                        },
                        "required": ["source_header", "canonical_key", "confidence"],
                    },
                },
                "unmapped": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Заголовки, которые не подходят ни под один ключ",
                },
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "default": {"type": "string"},
                        },
                        "required": ["field", "question"],
                    },
                    "description": "Вопросы оператору для уточнения",
                },
            },
            "required": ["mappings"],
        },
    }

    prompt = (
        "Определи назначение колонок прайс-листа поставщика запчастей "
        "для спецтехники (Caterpillar, Komatsu, etc.). "
        "Используй tool propose_mapping для ответа.\n\n"
        "Правила:\n"
        "- Маппь только те колонки, в которых уверен (confidence ≥ 0.7)\n"
        "- Колонки, которые не подходят ни под один ключ — в unmapped\n"
        "- Если колонка неоднозначна — задай вопрос в questions\n\n"
        f"Нераспознанные заголовки: {', '.join(repr(h) for h in unknown_headers)}\n\n"
        f"Примеры строк:\n{sample_text}"
    )

    try:
        msg = client.messages.create(
            model=getattr(settings, "ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
            tools=[propose_mapping_tool],
            tool_choice={"type": "tool", "name": "propose_mapping"},
        )
        for block in msg.content:
            if block.type == "tool_use" and block.name == "propose_mapping":
                result = {}
                for m in block.input.get("mappings", []):
                    h = m.get("source_header", "")
                    c = m.get("canonical_key", "")
                    conf = m.get("confidence", 0)
                    if (h in unknown_headers and c in allowed_canonicals
                            and conf >= 0.7):
                        result[h] = c
                return result
        return {}
    except Exception:
        logger.exception("AI tool-use mapping failed")
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
    std_keys = [k for k, _, _, _, _ in STD_FIELDS if k in col_idx]
    rows = []
    for row in (sample_rows or [])[:5]:
        rows.append([
            (str(row[col_idx[k]]) if col_idx[k] < len(row) else "")
            for k in std_keys
        ])
    labels = {k: lbl for k, lbl, _, _, _ in STD_FIELDS}
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
        for k, label, req, enum_v, _ in STD_FIELDS
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
            model=getattr(settings, "ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=400,
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


def _apply_transform(field: str, raw_value: str, transform_rules: dict,
                      row_vars: dict[str, Any]) -> str:
    """Применяет правило трансформации к значению поля если есть."""
    rule = transform_rules.get(field)
    if not rule:
        return raw_value
    rule_type = rule.get("type", "")
    if rule_type == "formula":
        formula = rule.get("formula", "")
        if not formula:
            return raw_value
        try:
            result = eval_formula(formula, row_vars)
            return str(result)
        except Exception:
            return raw_value
    if rule_type == "map":
        value_map = rule.get("map", {})
        return value_map.get(raw_value.strip(), raw_value)
    if rule_type == "concat":
        parts = rule.get("parts", [])
        return " ".join(str(row_vars.get(p, p)) for p in parts).strip()
    return raw_value


def _lookup_reference_data(oems: list[str], brand: str | None = None) -> dict[str, dict]:
    """Поиск точных данных по OEM-номеру в нашей эталонной базе
    (таможня, дилеры, OEM-каталоги).

    Используется как enrichment layer ПЕРЕД AI оценкой:
    точные данные из реестров > AI guess > defaults.

    Source priority: customs > dealer > oem > manual > ai.

    Returns: {oem_number: {weight_kg, length_cm, ..., source, confidence}}
    """
    from marketplace.models import PartReference
    if not oems:
        return {}
    qs = PartReference.objects.filter(oem_number__in=oems).order_by(
        "oem_number", "-confidence", "updated_at",
    )
    if brand:
        # Сначала с точным брендом, потом без — fallback
        pass
    SOURCE_PRIORITY = {"customs": 5, "dealer": 4, "oem": 3, "manual": 2, "ai": 1}
    best: dict[str, dict] = {}
    for ref in qs:
        cur = best.get(ref.oem_number)
        score = (
            SOURCE_PRIORITY.get(ref.source, 0),
            ref.confidence,
        )
        if cur is None or score > cur["_score"]:
            best[ref.oem_number] = {
                "weight_kg": float(ref.weight_kg) if ref.weight_kg else None,
                "length_cm": float(ref.length_cm) if ref.length_cm else None,
                "width_cm":  float(ref.width_cm)  if ref.width_cm  else None,
                "height_cm": float(ref.height_cm) if ref.height_cm else None,
                "hs_code":   ref.hs_code or None,
                "country_of_origin": ref.country_of_origin or None,
                "source":    ref.source,
                "confidence": ref.confidence,
                "_score":    score,
            }
    for v in best.values():
        v.pop("_score", None)
    return best


def _import_file(import_obj, mapping: dict[str, str], blob: bytes,
                 transform_rules: dict | None = None,
                 constants: dict | None = None):
    """Полный детерминированный импорт по подтверждённому маппингу.

    transform_rules: {std_field: {type: formula|map|concat, ...}}
    constants: {std_field: fixed_value} — дополнительные константы

    Enrichment chain (для отсутствующих в файле полей):
      1. PartReference (customs/dealer/OEM) — точные данные
      2. ai_estimates (если seller вызвал AI) — оценки
      3. fix-defaults / NULL
    """
    from marketplace.models import Brand, Category, Part

    seller = import_obj.seller
    headers = import_obj.headers
    transform_rules = transform_rules or {}
    constants = constants or {}
    ai_estimates = getattr(import_obj, "ai_estimates", None) or {}

    col_idx: dict[str, int] = {}
    fixed_vals: dict[str, str] = {}
    # Сначала дефолты, потом mapping (mapping перезаписывает)
    for fld, dflt in FIELD_DEFAULTS.items():
        fixed_vals[fld] = str(dflt)
    for fld, val in mapping.items():
        if not val:
            continue
        if isinstance(val, str) and val.startswith("fix:"):
            fixed_vals[fld] = val[4:]
        elif val in headers:
            col_idx[fld] = headers.index(val)
            fixed_vals.pop(fld, None)
    for fld, val in constants.items():
        if fld not in col_idx and fld not in fixed_vals:
            fixed_vals[fld] = str(val)

    missing_required = [
        f for f in REQUIRED_FIELDS
        if f not in col_idx and f not in fixed_vals
    ]
    if missing_required:
        return 0, 0, 0, 0, [{"row": 0, "reason": f"missing required mapping: {missing_required}"}]

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

    # Кэш брендов: на каждый прайс одни и те же названия → не лезть
    # в БД n раз. seed'им существующими + Generic.
    brand_cache: dict[str, "Brand"] = {
        b.name.lower(): b for b in Brand.objects.all()
    }
    brand_cache.setdefault("generic", generic_brand)

    # Прогресс импорта для polling из UI
    from django.core.cache import cache
    progress_key = f"import_progress_{import_obj.id}"
    cache.set(progress_key, {"current": 0, "total": 0, "running": True}, 600)

    # ───────── Pass 1: парсим все строки в payload'ы ─────────
    payloads: list[dict] = []
    for row_n, row in enumerate(_read_all(import_obj.filename, blob), start=2):
            if not any(c.strip() for c in row):
                continue

            def get_raw(field):
                if field in fixed_vals:
                    return fixed_vals[field]
                idx = col_idx.get(field)
                if idx is None or idx >= len(row):
                    return ""
                return str(row[idx]).strip()

            row_vars: dict[str, Any] = {}
            for fld in col_idx:
                idx = col_idx[fld]
                raw = str(row[idx]).strip() if idx < len(row) else ""
                dec = _coerce_decimal(raw)
                row_vars[fld] = dec if dec is not None else raw

            def get(field):
                raw = get_raw(field)
                return _apply_transform(field, raw, transform_rules, row_vars)

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

            brand_name = (get("brand") or "").strip()
            if brand_name:
                key = brand_name.lower()
                brand = brand_cache.get(key)
                if not brand:
                    brand = Brand.objects.create(
                        name=brand_name[:200],
                        slug=slugify(brand_name)[:200] or generic_brand.slug,
                    )
                    brand_cache[key] = brand
            else:
                brand = generic_brand

            stock = _coerce_int(get("stock")) or 0
            currency = _normalize_currency(get("currency"))
            cond_raw = (get("condition") or "").strip().lower()
            condition = ("oem" if "oem" in cond_raw else
                          "reman" if "reman" in cond_raw else
                          "aftermarket" if "after" in cond_raw else
                          "oem")
            cross_number = (get("cross_number") or "")[:500]
            price_fob_sea = _coerce_decimal(get("price_fob_sea")) or Decimal("0")
            price_fob_air = _coerce_decimal(get("price_fob_air")) or Decimal("0")
            warehouse = (get("warehouse_address") or "")[:255]
            sea_port = (get("sea_port") or "")[:120]
            air_port = (get("air_port") or "")[:120]
            # AI-оценки per-part используются ТОЛЬКО когда колонка не
            # замаплена на файл (т.е. сейчас стоит дефолтный fix). Если
            # значение пришло из файла — оно приоритет.
            ai_est = ai_estimates.get(oem) if ai_estimates else None

            def _dim_with_ai(field, default_val, ai_key):
                # Если колонка замаплена на файл — берём из файла
                if field in col_idx:
                    raw = _coerce_decimal(get(field))
                    if raw and raw > 0:
                        return raw
                # Иначе — AI-оценка приоритет над fix-дефолтом
                if ai_est and ai_est.get(ai_key):
                    try:
                        v = Decimal(str(ai_est[ai_key]))
                        if v > 0:
                            return v
                    except Exception:
                        pass
                # Fallback на fix-значение (из constants/FIELD_DEFAULTS)
                raw = _coerce_decimal(get(field))
                return raw if raw and raw > 0 else default_val

            weight = _dim_with_ai("weight_kg", Decimal("0.5"), "weight_kg")
            length = _dim_with_ai("length_cm", Decimal("1.0"), "length_cm")
            width = _dim_with_ai("width_cm", Decimal("1.0"), "width_cm")
            height = _dim_with_ai("height_cm", Decimal("1.0"), "height_cm")

            payloads.append({
                "row_n": row_n,
                "oem": oem[:100],
                "fields": {
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
            })

    # ───────── Pass 2a: bulk SELECT существующих Parts по oem ─────────
    oems = [p["oem"] for p in payloads]
    existing_by_oem: dict[str, "Part"] = {}
    if oems:
        for p in Part.objects.filter(seller=seller, oem_number__in=oems):
            existing_by_oem[p.oem_number.lower()] = p

    # ───────── Pass 2b: enrichment из PartReference (customs/dealer/OEM) ─
    # Для каждой позиции пробуем достать точные данные из эталонной базы.
    # Применяется ТОЛЬКО если значение не пришло из файла (col_idx)
    # и не задано юзером явно (constants).
    reference_data = _lookup_reference_data(oems)
    reference_hits = 0
    for pl in payloads:
        ref = reference_data.get(pl["oem"])
        if not ref:
            continue
        fields = pl["fields"]
        applied = False
        for key in ("weight_kg", "length_cm", "width_cm", "height_cm"):
            # Применяем reference только если текущее значение — дефолт
            # (т.е. не из файла), и в reference есть значение.
            cur = fields.get({"weight_kg": "gross_weight_kg",
                               "length_cm": "length_cm",
                               "width_cm":  "width_cm",
                               "height_cm": "height_cm"}[key])
            if key in col_idx:
                continue  # из файла — приоритет
            if ref.get(key) is not None:
                target = {"weight_kg": "gross_weight_kg",
                          "length_cm": "length_cm",
                          "width_cm":  "width_cm",
                          "height_cm": "height_cm"}[key]
                fields[target] = Decimal(str(ref[key]))
                applied = True
        if applied:
            reference_hits += 1
    if reference_hits:
        logger.info("PartReference enrichment: %d positions matched", reference_hits)
    # Сохраним статистику в import_obj для отображения в UI
    import_obj._enrichment_stats = {"reference_hits": reference_hits}

    # ───────── Pass 3: разделяем на create vs update ─────────
    to_create: list = []
    to_update: list = []
    update_fields = [
        "title", "price", "currency", "stock_quantity", "condition",
        "cross_numbers", "price_fob_sea", "price_fob_air",
        "warehouse_address", "sea_port", "air_port",
        "gross_weight_kg", "length_cm", "width_cm", "height_cm",
        "category", "brand", "is_active", "slug",
    ]
    for pl in payloads:
        existing = existing_by_oem.get(pl["oem"].lower())
        if existing:
            for k, v in pl["fields"].items():
                setattr(existing, k, v)
            to_update.append(existing)
        else:
            to_create.append(Part(seller=seller, **pl["fields"]))

    # ───────── Pass 4: bulk_create + bulk_update ─────────
    BATCH = 200
    with transaction.atomic():
        for i in range(0, len(to_create), BATCH):
            chunk = to_create[i:i + BATCH]
            try:
                Part.objects.bulk_create(chunk, batch_size=BATCH)
                created += len(chunk)
            except Exception as e:
                failed += len(chunk)
                if len(errors) < 50:
                    errors.append({"row": 0, "reason": f"bulk_create: {str(e)[:120]}"})
            cache.set(progress_key, {
                "current": created + updated, "total": len(payloads), "running": True,
            }, 600)

        for i in range(0, len(to_update), BATCH):
            chunk = to_update[i:i + BATCH]
            try:
                Part.objects.bulk_update(chunk, fields=update_fields, batch_size=BATCH)
                updated += len(chunk)
            except Exception as e:
                failed += len(chunk)
                if len(errors) < 50:
                    errors.append({"row": 0, "reason": f"bulk_update: {str(e)[:120]}"})
            cache.set(progress_key, {
                "current": created + updated, "total": len(payloads), "running": True,
            }, 600)

    imported = created + updated
    cache.set(progress_key, {
        "current": imported, "total": imported + failed, "running": False,
    }, 600)
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
        from marketplace.models import (
            PricelistImport, PricelistMapping, SupplierImportProfile,
        )

        f = request.FILES.get("file")
        if not f:
            return Response({"error": "Файл не передан."}, status=400)
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
        if not headers or not any(str(h).strip() for h in headers):
            return Response({"error": "Первая строка пустая — нет заголовков."}, status=400)
        non_empty = [h for h in headers if str(h).strip()]
        if len(non_empty) < MIN_COLUMNS:
            return Response({"error": (
                f"В файле слишком мало колонок ({len(non_empty)}). "
                f"Минимум {MIN_COLUMNS}."
            )}, status=400)
        if len(headers) > MAX_COLUMNS:
            return Response({"error": (
                f"В файле слишком много колонок ({len(headers)}). "
                f"Максимум {MAX_COLUMNS}."
            )}, status=400)

        # 1. Ищем сохранённый профиль по fingerprint заголовков
        fingerprint = SupplierImportProfile.compute_fingerprint(headers)
        profile = SupplierImportProfile.objects.filter(
            seller=request.user, headers_fingerprint=fingerprint, is_active=True,
        ).first()

        ai_called = False
        unknown: list = []
        smart_status = "ok"
        transform_rules: dict = {}
        constants: dict = {}
        from_profile = False

        if profile:
            suggested = {k: v for k, v in profile.column_mapping.items()
                          if v and (v.startswith("fix:") or v in headers)}
            transform_rules = profile.transform_rules or {}
            constants = profile.constants or {}
            from_profile = True
        else:
            prev = PricelistMapping.objects.filter(seller=request.user).first()
            if prev and prev.mapping:
                suggested = {k: v for k, v in prev.mapping.items()
                              if v and (v.startswith("fix:") or v in headers)}
            else:
                suggested, unknown, ai_called, smart_status = _smart_mapping(
                    headers, sample, seller=request.user,
                )
        if smart_status == "quota_exceeded":
            return Response({
                "error": (
                    "Лимит распознавания исчерпан. "
                    "Попробуйте завтра или скачайте шаблон."
                ),
            }, status=429)

        mapped_preview = _build_mapped_preview(headers, sample, suggested)

        # Незамапленные опциональные поля → автозаполнение дефолтами
        auto_defaults = {}
        for key, label, req, enum_v, dflt in STD_FIELDS:
            if key not in suggested and key not in constants and dflt is not None:
                auto_defaults[key] = dflt
                suggested[key] = f"fix:{dflt}"

        # Вопросы оператору — только required без маппинга (oem, title, price)
        questions = []
        for key, label, req, enum_v, dflt in STD_FIELDS:
            if req and key not in suggested and key not in constants:
                q: dict[str, Any] = {
                    "field": key,
                    "question": f"Укажите значение для «{label}»",
                    "type": "select" if enum_v else "text",
                }
                if enum_v:
                    q["options"] = enum_v
                    q["default"] = enum_v[0]
                questions.append(q)

        imp = PricelistImport.objects.create(
            seller=request.user,
            filename=f.name[:255],
            headers=headers,
            sample_rows=sample,
            suggested_mapping=suggested,
            ai_called=ai_called,
            status="preview",
        )
        imp.file_obj.save(f.name, ContentFile(blob), save=True)

        # Подсчитаем общее количество строк (быстро, потоковый счёт)
        try:
            total_rows = sum(1 for _ in _read_all(f.name, blob))
        except Exception:
            total_rows = None

        # AI questionnaire НЕ генерируется здесь синхронно
        # (это ~4 секунды задержки). Юзер запросит его отдельно
        # через /smart-questions/ — фронт сразу показывает форму.

        return Response({
            "import_id": imp.id,
            "filename": f.name,
            "headers": headers,
            "sample_rows": sample,
            "total_rows": total_rows,
            "mapped_preview": mapped_preview,
            "suggested_mapping": suggested,
            "ai_called": ai_called,
            "ai_intro": "",         # подтянется async
            "smart_questions": [],  # подтянется async
            "smart_questions_pending": True,
            "unknown_headers": unknown,
            "from_saved_mapping": bool(not ai_called and not from_profile),
            "from_profile": from_profile,
            "profile_name": profile.name if profile else None,
            "transform_rules": transform_rules,
            "constants": constants,
            "questions": questions,
            "std_fields": [
                {"key": k, "label": label, "required": req,
                 "enum_values": enum_v or [], "default": dflt or ""}
                for k, label, req, enum_v, dflt in STD_FIELDS
            ],
        })


class PricelistCommitView(APIView):
    """POST /api/assistant/upload-pricelist/<id>/commit/

    body: {"mapping": {std_field: source_column}}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, import_id):
        from marketplace.models import (
            PricelistImport, PricelistMapping, SupplierImportProfile,
        )
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)
        if imp.status != "preview":
            return Response({"error": f"already in status {imp.status}"}, status=400)

        mapping = request.data.get("mapping") or {}
        transform_rules = request.data.get("transform_rules") or {}
        constants = request.data.get("constants") or {}
        save_profile = request.data.get("save_profile", True)
        # Юзерские правки AI-оценок: {oem: {weight_kg, length_cm, ...}}
        # Применяются поверх ai_estimates как human-in-the-loop корректировка
        ai_overrides = request.data.get("ai_estimates_override") or {}
        if isinstance(ai_overrides, dict) and ai_overrides:
            current = imp.ai_estimates or {}
            for oem, fields in ai_overrides.items():
                if not isinstance(fields, dict):
                    continue
                cur = current.get(oem, {})
                for k in ("weight_kg", "length_cm", "width_cm", "height_cm"):
                    if k in fields:
                        try:
                            cur[k] = float(fields[k])
                        except (TypeError, ValueError):
                            pass
                cur["confidence"] = 1.0  # юзер подтвердил → 100%
                current[oem] = cur
            imp.ai_estimates = current
            imp.save(update_fields=["ai_estimates"])

        if not isinstance(mapping, dict):
            return Response({"error": "mapping must be object"}, status=400)

        # Применяем ответы на вопросы: constants заполняют fix-значения
        for fld, val in constants.items():
            if fld not in mapping:
                mapping[fld] = f"fix:{val}"

        for f_key in REQUIRED_FIELDS:
            if not mapping.get(f_key):
                return Response(
                    {"error": f"required field '{f_key}' not mapped"}, status=400,
                )
        for fld, val in mapping.items():
            if not val:
                continue
            if isinstance(val, str) and val.startswith("fix:"):
                continue
            if val not in imp.headers:
                return Response(
                    {"error": f"unknown column '{val}' for {fld}"}, status=400,
                )

        # Валидация формул
        available_vars = list(mapping.keys())
        for fld, rule in transform_rules.items():
            if rule.get("type") == "formula" and rule.get("formula"):
                ok, reason = validate_formula(rule["formula"], available_vars)
                if not ok:
                    return Response(
                        {"error": f"Invalid formula for {fld}: {reason}"},
                        status=400,
                    )

        try:
            with imp.file_obj.open("rb") as fh:
                blob = fh.read()
        except Exception as e:
            return Response({"error": f"file unavailable: {e}"}, status=500)

        imported, created, updated, failed, errors = _import_file(
            imp, mapping, blob,
            transform_rules=transform_rules,
            constants=constants,
        )

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

        PricelistMapping.objects.update_or_create(
            seller=request.user, defaults={"mapping": mapping},
        )

        # Сохраняем/обновляем профиль поставщика для повторных загрузок
        if save_profile and imp.headers:
            fp = SupplierImportProfile.compute_fingerprint(imp.headers)
            SupplierImportProfile.objects.update_or_create(
                seller=request.user,
                headers_fingerprint=fp,
                defaults={
                    "source_headers": imp.headers,
                    "column_mapping": mapping,
                    "transform_rules": transform_rules,
                    "constants": constants,
                    "name": f"Auto · {imp.filename[:60]}",
                    "use_count": 1,
                },
            )

        # Что точно НЕ пришло из файла — для transparency после импорта
        # пользователь должен это видеть, чтобы понимать качество данных.
        missing_from_file = []
        FIELD_LABELS = {
            "weight_kg": "вес", "length_cm": "длина", "width_cm": "ширина",
            "height_cm": "высота", "stock": "остаток",
            "price_fob_sea": "FOB SEA", "price_fob_air": "FOB AIR",
            "warehouse_address": "адрес склада",
            "sea_port": "морпорт", "air_port": "аэропорт",
            "cross_number": "кросс-номер",
        }
        for key, label in FIELD_LABELS.items():
            val = mapping.get(key, "")
            if not val or (isinstance(val, str) and val.startswith("fix:")):
                missing_from_file.append({"key": key, "label": label})

        enrichment_stats = getattr(imp, "_enrichment_stats", {}) or {}
        return Response({
            "ok": True,
            "import_id": imp.id,
            "imported": imported,
            "created": created,
            "updated": updated,
            "failed": failed,
            "errors_preview": errors[:10],
            "missing_from_file": missing_from_file,
            "ai_estimated_count": len(imp.ai_estimates or {}),
            "reference_enriched": enrichment_stats.get("reference_hits", 0),
        })


def _ai_smart_questions(headers: list[str], sample_rows: list[list[str]],
                         total_rows: int | None, mapping: dict[str, str],
                         filename: str = "") -> dict:
    """Claude.ai-style: возвращает {intro, questions[]} структурированно.

    questions = [{
        field: "brand" | "condition" | "price_fob_sea" | ...,
        question: "Какой бренд?",
        options: ["Epiroc", "Caterpillar", ...],    # chip-кнопки
        default: "Epiroc",                          # для prefill
        placeholder: "напр. Москва",
        apply_as: "constant" | "formula",
        formula_template: "price_exw * (1 + {value}/100)",  # для markup
    }]
    """
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"intro": "", "questions": []}
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
    except Exception:
        return {"intro": "", "questions": []}

    mapped_from_file = [k for k, v in mapping.items()
                         if v and not v.startswith("fix:")]
    missing_keys = []
    for k, label, req, _, _ in STD_FIELDS:
        if k not in mapping or mapping[k].startswith("fix:"):
            if k not in mapped_from_file:
                missing_keys.append(k)

    sample_text = "\n".join(
        " | ".join(str(c)[:30] for c in row) for row in (sample_rows or [])[:3]
    )
    rows_info = f"{total_rows} позиций" if total_rows else f"{len(headers)} колонок"

    questionnaire_tool = {
        "name": "generate_questionnaire",
        "description": (
            "Сгенерировать дружелюбный intro + 2-4 умных вопроса "
            "для уточнения данных прайс-листа. Каждый вопрос имеет "
            "варианты ответа (chip-кнопки) или текстовое поле."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intro": {
                    "type": "string",
                    "description": ("1-2 строки приветствия: что увидел в файле "
                                     "(объём, распознанные поля). Эмодзи ok."),
                },
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "description": ("std-поле платформы (brand, condition, "
                                                 "warehouse_address, sea_port, air_port, "
                                                 "currency) ИЛИ price_fob_sea / "
                                                 "price_fob_air для наценок"),
                            },
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Chip-кнопки (3-5 вариантов)",
                            },
                            "default": {"type": "string"},
                            "placeholder": {"type": "string"},
                            "apply_as": {
                                "type": "string",
                                "enum": ["constant", "formula"],
                                "description": ("constant — прямое значение в "
                                                 "constants. formula — для наценок типа "
                                                 "FOB SEA +15%, применяется как "
                                                 "price_exw * (1 + value/100)"),
                            },
                        },
                        "required": ["field", "question", "apply_as"],
                    },
                },
            },
            "required": ["intro", "questions"],
        },
    }

    prompt = (
        f"Юзер загрузил прайс-лист в B2B-маркетплейс запчастей.\n\n"
        f"ФАЙЛ: {filename}\n"
        f"КОЛОНКИ ({len(headers)}): {', '.join(headers)}\n"
        f"ОБЪЁМ: {rows_info}\n"
        f"ПЕРВЫЕ СТРОКИ:\n{sample_text}\n\n"
        f"РАСПОЗНАНО ИЗ ФАЙЛА: {', '.join(mapped_from_file) or '—'}\n"
        f"НЕТ В ФАЙЛЕ: {', '.join(missing_keys[:10]) or '—'}\n\n"
        "Сгенерируй questionnaire через tool generate_questionnaire.\n"
        "ПРАВИЛА:\n"
        "- intro: 1-2 строки дружелюбно, как Claude.ai\n"
        "- Задавай 2-4 вопроса (не больше!) про САМОЕ ВАЖНОЕ:\n"
        "  бренд, состояние, наценка FOB SEA, наценка FOB AIR, склад\n"
        "- НЕ спрашивай про вес/габариты/остаток (это per-part)\n"
        "- options — 3-5 chip-вариантов плюс возможность ввести своё\n"
        "- Для наценок FOB используй apply_as=formula, options как «+10%, +15%, +20%»\n"
        "- Если бренд явный (Epiroc в имени файла) — default=«Epiroc»\n"
    )
    try:
        msg = client.messages.create(
            model=getattr(settings, "ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
            tools=[questionnaire_tool],
            tool_choice={"type": "tool", "name": "generate_questionnaire"},
        )
        for block in msg.content:
            if block.type == "tool_use" and block.name == "generate_questionnaire":
                inp = block.input or {}
                return {
                    "intro": inp.get("intro", ""),
                    "questions": inp.get("questions", []),
                }
        return {"intro": "", "questions": []}
    except Exception:
        logger.exception("AI questionnaire failed")
        return {"intro": "", "questions": []}


def _ai_estimate_per_part(items: list[dict],
                            on_partial=None) -> dict[str, dict]:
    """Claude tool use streaming: оценить вес/габариты per-part.

    items: [{oem, title, description?}] — до 50 штук за раз
    on_partial: callback(estimates_dict) вызывается когда из стрима
                распарсилась новая полная позиция. Это позволяет UI
                показывать прогресс ITEM-BY-ITEM как у claude.ai,
                а не batch-by-batch.
    returns: {oem: {weight_kg, length_cm, width_cm, height_cm}}
    """
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key or not items:
        return {}
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
    except Exception:
        return {}

    estimate_tool = {
        "name": "estimate_dimensions",
        "description": (
            "Оценить вес и габариты каждой запчасти на основе её "
            "названия, OEM-номера и описания. Используй типичные "
            "значения для подобных деталей спецтехники "
            "(Caterpillar, Komatsu, Hitachi, Liebherr и др.). "
            "Если не уверен — давай средние/консервативные оценки."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "estimates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oem": {"type": "string"},
                            "weight_kg": {"type": "number", "minimum": 0.01, "maximum": 5000},
                            "length_cm": {"type": "number", "minimum": 1, "maximum": 500},
                            "width_cm":  {"type": "number", "minimum": 1, "maximum": 500},
                            "height_cm": {"type": "number", "minimum": 1, "maximum": 500},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["oem", "weight_kg", "length_cm",
                                      "width_cm", "height_cm"],
                    },
                },
            },
            "required": ["estimates"],
        },
        # Prompt caching: tool definition не меняется между batch'ами →
        # 5x дешевле + быстрее на повторных вызовах.
        "cache_control": {"type": "ephemeral"},
    }

    items_text = "\n".join(
        f"- OEM:{it.get('oem','')[:30]} | {it.get('title','')[:80]}"
        + (f" | {it.get('description','')[:60]}" if it.get('description') else "")
        for it in items
    )
    prompt = (
        "Оцени вес (кг) и габариты (см) каждой запчасти спецтехники "
        "по её названию и OEM-номеру. Используй здравый смысл и "
        "типичные значения для подобных деталей.\n\n"
        f"Запчасти ({len(items)}):\n{items_text}\n\n"
        "Используй tool estimate_dimensions для ответа. "
        "Верни оценку для КАЖДОЙ позиции."
    )

    try:
        result: dict[str, dict] = {}
        accumulated = ""
        last_seen_oems: set = set()

        with client.messages.stream(
            model=getattr(settings, "ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
            tools=[estimate_tool],
            tool_choice={"type": "tool", "name": "estimate_dimensions"},
        ) as stream:
            for event in stream:
                # input_json_delta — частичный JSON tool input, приходит
                # по мере генерации Claude'ом. Аккумулируем и парсим.
                if event.type == "content_block_delta" and \
                   getattr(event.delta, "type", None) == "input_json_delta":
                    accumulated += event.delta.partial_json
                    # Эвристика: ищем закрытые объекты "{...}" с полями
                    # weight_kg/length_cm и парсим их по одному.
                    new_count = 0
                    while True:
                        obj_str, next_pos = _try_extract_next_estimate(accumulated)
                        if obj_str is None:
                            break
                        accumulated = accumulated[next_pos:]
                        try:
                            est = json.loads(obj_str)
                            oem = str(est.get("oem", "")).strip()
                            if oem and oem not in last_seen_oems:
                                result[oem] = {
                                    "weight_kg": float(est.get("weight_kg", 0) or 0),
                                    "length_cm": float(est.get("length_cm", 0) or 0),
                                    "width_cm":  float(est.get("width_cm", 0) or 0),
                                    "height_cm": float(est.get("height_cm", 0) or 0),
                                    "confidence": float(est.get("confidence", 0.8) or 0.8),
                                }
                                last_seen_oems.add(oem)
                                new_count += 1
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                    if new_count > 0 and on_partial:
                        try:
                            on_partial(dict(result))
                        except Exception:
                            pass

            # Финальный fallback — полный input из tool_use блока
            final_msg = stream.get_final_message()
            for block in final_msg.content:
                if block.type == "tool_use" and block.name == "estimate_dimensions":
                    for est in block.input.get("estimates", []):
                        oem = str(est.get("oem", "")).strip()
                        if oem and oem not in result:
                            result[oem] = {
                                "weight_kg": float(est.get("weight_kg", 0) or 0),
                                "length_cm": float(est.get("length_cm", 0) or 0),
                                "width_cm":  float(est.get("width_cm", 0) or 0),
                                "height_cm": float(est.get("height_cm", 0) or 0),
                                "confidence": float(est.get("confidence", 0.8) or 0.8),
                            }
        return result
    except Exception:
        logger.exception("AI per-part estimation failed")
        return {}


def _try_extract_next_estimate(buf: str) -> tuple[str | None, int]:
    """Ищет в buf первый закрытый JSON объект {..."oem":...,...}.

    Возвращает (obj_str, end_pos) где end_pos — позиция после `}`.
    Если объект не нашли — (None, 0).

    Простой brace-counter, игнорирует строки в кавычках с escape.
    """
    start = buf.find("{")
    if start < 0:
        return None, 0
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(buf)):
        ch = buf[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return buf[start:i + 1], i + 1
    return None, 0


class PricelistAiEstimateView(APIView):
    """POST /api/assistant/upload-pricelist/<id>/ai-estimate/

    Запускает AI-оценку per-part вес/габариты для всех строк прайса
    батчами параллельно (5 workers). Сохраняет в imp.ai_estimates.
    При commit'е применяются как per-part overrides.
    """
    permission_classes = [IsAuthenticated]

    MAX_ROWS = 1000       # покрываем большинство реальных прайсов
    BATCH_SIZE = 10       # компромисс: плавный прогресс vs много API calls
    PARALLEL_WORKERS = 20  # параллельные Claude calls (rate limit 50 RPM)

    def post(self, request, import_id):
        from marketplace.models import PricelistImport
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)
        if imp.status != "preview":
            return Response({"error": f"already in status {imp.status}"}, status=400)

        mapping = imp.suggested_mapping or {}
        oem_col = mapping.get("oem_number")
        title_col = mapping.get("title")
        desc_col = mapping.get("description")
        if not oem_col or not title_col:
            return Response(
                {"error": "Нужен маппинг oem_number и title для AI-оценки."},
                status=400,
            )
        if oem_col.startswith("fix:") or title_col.startswith("fix:"):
            return Response(
                {"error": "oem_number и title должны быть из файла, не fix."},
                status=400,
            )
        if oem_col not in imp.headers or title_col not in imp.headers:
            return Response({"error": "колонки не найдены в headers"}, status=400)
        oem_idx = imp.headers.index(oem_col)
        title_idx = imp.headers.index(title_col)
        desc_idx = (imp.headers.index(desc_col) if desc_col
                     and not desc_col.startswith("fix:")
                     and desc_col in imp.headers else None)

        try:
            with imp.file_obj.open("rb") as fh:
                blob = fh.read()
        except Exception as e:
            return Response({"error": f"file unavailable: {e}"}, status=500)

        items: list[dict] = []
        for row in _read_all(imp.filename, blob):
            if len(items) >= self.MAX_ROWS:
                break
            row_list = list(row)
            if oem_idx >= len(row_list) or title_idx >= len(row_list):
                continue
            oem = str(row_list[oem_idx] or "").strip()
            title = str(row_list[title_idx] or "").strip()
            if not oem or not title:
                continue
            item = {"oem": oem, "title": title}
            if desc_idx is not None and desc_idx < len(row_list):
                d = str(row_list[desc_idx] or "").strip()
                if d:
                    item["description"] = d
            items.append(item)

        if not items:
            return Response({"error": "Не нашёл валидных строк для оценки."}, status=400)

        if not getattr(settings, "ANTHROPIC_API_KEY", ""):
            return Response({
                "error": "AI-оценка недоступна (ANTHROPIC_API_KEY не настроен на сервере).",
            }, status=503)

        chunks = [items[i:i + self.BATCH_SIZE]
                   for i in range(0, len(items), self.BATCH_SIZE)]
        total = len(items)
        truncated = len(items) >= self.MAX_ROWS

        from concurrent.futures import ThreadPoolExecutor, as_completed
        from django.core.cache import cache
        progress_key = f"ai_estimate_progress_{imp.id}"
        cache.set(progress_key, {"current": 0, "total": total, "running": True}, 300)

        # Глобальный shared dict — обновляется streaming callback'ами
        # из разных потоков. OEM уникальны между batch'ами.
        from threading import Lock
        live_estimates: dict[str, dict] = {}
        lock = Lock()

        def on_partial(batch_partial):
            with lock:
                live_estimates.update(batch_partial)
                cache.set(progress_key, {
                    "current": len(live_estimates),
                    "total": total, "running": True,
                }, 300)

        estimates: dict[str, dict] = {}
        try:
            with ThreadPoolExecutor(max_workers=self.PARALLEL_WORKERS) as ex:
                futures = [ex.submit(_ai_estimate_per_part, c, on_partial)
                            for c in chunks]
                for fut in as_completed(futures):
                    try:
                        with lock:
                            estimates.update(fut.result() or {})
                            live_estimates.update(estimates)
                    except Exception:
                        logger.exception("AI estimate batch failed")
                    cache.set(progress_key, {
                        "current": len(live_estimates),
                        "total": total, "running": True,
                    }, 300)
                    imp.ai_estimates = dict(estimates)
                    imp.save(update_fields=["ai_estimates"])
        finally:
            cache.set(progress_key, {
                "current": len(estimates), "total": total, "running": False,
            }, 300)

        # Sample для review — приоритет позициям с низкой confidence,
        # чтобы юзер мог проверить именно их (Claude-style human-in-loop)
        items_by_oem = {it["oem"]: it for it in items}
        scored = []
        for oem, est in estimates.items():
            conf = est.get("confidence", 0.8)
            title = items_by_oem.get(oem, {}).get("title", "")
            scored.append((conf, oem, title, est))
        # Сортируем: сначала с низкой confidence — туда смотреть в первую очередь
        scored.sort(key=lambda x: x[0])
        review_sample = []
        for conf, oem, title, est in scored[:8]:
            review_sample.append({
                "oem": oem, "title": title,
                "weight_kg": est["weight_kg"],
                "length_cm": est["length_cm"],
                "width_cm": est["width_cm"],
                "height_cm": est["height_cm"],
                "confidence": conf,
            })
        low_conf_count = sum(1 for c, _, _, _ in scored if c < 0.6)

        return Response({
            "ok": True,
            "estimated": len(estimates),
            "total": total,
            "truncated": truncated,
            "review_sample": review_sample,
            "low_confidence_count": low_conf_count,
        })


class PricelistImportProgressView(APIView):
    """GET /api/assistant/upload-pricelist/<id>/import-progress/

    Возвращает текущий прогресс импорта (commit) для polling из UI.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id):
        from django.core.cache import cache
        from marketplace.models import PricelistImport
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)
        progress = cache.get(f"import_progress_{imp.id}") or {}
        return Response({
            "current": progress.get("current", 0),
            "running": progress.get("running", False),
            "status": imp.status,
        })


class PricelistSmartQuestionsView(APIView):
    """GET /api/assistant/upload-pricelist/<id>/smart-questions/

    Async-генерация Claude.ai-style questionnaire. Вызывается фронтом
    сразу после upload параллельно с показом базовой формы — чтобы
    upload-ответ не блокировался на 4 секундах Claude API.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id):
        from marketplace.models import PricelistImport
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)

        smart = _ai_smart_questions(
            imp.headers, imp.sample_rows, None,
            imp.suggested_mapping or {}, imp.filename,
        )
        return Response({
            "intro": smart.get("intro", ""),
            "questions": smart.get("questions", []),
        })


class PricelistAiEstimateProgressView(APIView):
    """GET /api/assistant/upload-pricelist/<id>/ai-estimate-progress/

    Возвращает текущий прогресс AI-оценки для polling из UI.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id):
        from django.core.cache import cache
        from marketplace.models import PricelistImport
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)
        progress = cache.get(f"ai_estimate_progress_{imp.id}") or {}
        return Response({
            "current": progress.get("current", 0),
            "total": progress.get("total", 0),
            "running": progress.get("running", False),
            "estimated_in_db": len(imp.ai_estimates or {}),
        })


class PricelistTemplateXlsxView(APIView):
    """GET /api/assistant/pricelist-template.xlsx — Excel-шаблон с
    инструкцией на отдельном листе + 16 колонок и 3 примера строк.
    """
    from rest_framework.permissions import AllowAny
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        from django.http import HttpResponse, FileResponse, Http404
        from django.conf import settings
        path = os.path.join(
            str(settings.BASE_DIR),
            "static", "templates", "consolidator_pricelist_template.xlsx",
        )
        if not os.path.exists(path):
            raise Http404("Template not found")
        resp = FileResponse(
            open(path, "rb"),
            content_type=("application/vnd.openxmlformats-officedocument."
                          "spreadsheetml.sheet"),
        )
        resp["Content-Disposition"] = (
            'attachment; filename="consolidator_pricelist_template.xlsx"'
        )
        return resp


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
