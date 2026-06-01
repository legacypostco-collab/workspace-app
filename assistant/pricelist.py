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
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .actions import ActionResult, register

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
    ("stock",             "Остаток (Quantity)",      False, None, "1"),
    ("condition",         "Тип товара",              False, ["OEM", "AFTERMARKET", "REMAN"], "OEM"),
    ("availability",      "Наличие",                 False, ["IN_STOCK", "BACKORDER"], "IN_STOCK"),
    ("manufacturer",      "Завод-производитель",     False, None, ""),
    ("manufacturer_visible", "Показывать завод",     False, ["Да", "Нет"], "Да"),
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


# ── EN-translation для названий запчастей ──────────────────────
# Каталог маркетплейса международный → выходной XLSX всегда на английском.
# Известные термины переводим, остальное оставляем (лучше частичный перевод
# чем сломанный текст).
_EN_TERMS: dict[str, str] = {
    # Chinese (упрощённый) — экскаватор/спецтехника
    "岩石型斗齿": "Rock-type bucket tooth",
    "斗齿": "Bucket tooth", "齿座": "Tooth holder",
    "铲斗": "Bucket", "岩石铲斗": "Rock bucket",
    "立方": "m³",
    "履带": "Track", "履带板": "Track shoe",
    "拖链轮": "Carrier roller", "拖轮": "Carrier roller",
    "链轨": "Track link", "链节": "Track link",
    "驱动轮": "Drive sprocket", "支重轮": "Track roller",
    "引导轮": "Front idler", "托链轮": "Carrier roller",
    "回转支承": "Slewing bearing", "回转马达": "Swing motor",
    "行走马达": "Travel motor",
    "动臂": "Boom", "斗杆": "Stick", "斗杆缸": "Stick cylinder",
    "动臂缸": "Boom cylinder", "铲斗缸": "Bucket cylinder",
    "液压泵": "Hydraulic pump", "主泵": "Main pump",
    # Chinese (упрощённый) — крепёж и базовые запчасти
    "螺栓": "Bolt", "螺钉": "Screw", "螺丝": "Screw", "螺母": "Nut",
    "垫圈": "Washer", "弹垫": "Spring washer", "平垫": "Flat washer",
    "销": "Pin", "卡簧": "Snap ring", "开口销": "Cotter pin",
    "轴承": "Bearing", "密封": "Seal", "油封": "Oil seal",
    "滤芯": "Filter", "滤清器": "Filter", "机油滤芯": "Oil filter",
    "空气滤芯": "Air filter", "燃油滤芯": "Fuel filter",
    "皮带": "Belt", "链条": "Chain", "齿轮": "Gear",
    "活塞": "Piston", "活塞环": "Piston ring", "气缸": "Cylinder",
    "曲轴": "Crankshaft", "凸轮轴": "Camshaft", "连杆": "Connecting rod",
    "水泵": "Water pump", "油泵": "Oil pump", "燃油泵": "Fuel pump",
    "喷油器": "Injector", "喷嘴": "Nozzle", "火花塞": "Spark plug",
    "传感器": "Sensor", "继电器": "Relay", "保险丝": "Fuse",
    "电池": "Battery", "起动机": "Starter", "发电机": "Alternator",
    "散热器": "Radiator", "水箱": "Radiator", "风扇": "Fan",
    "刹车片": "Brake pad", "刹车盘": "Brake disc", "离合器": "Clutch",
    "轮胎": "Tire", "钢圈": "Rim", "车轮": "Wheel",
    "软管": "Hose", "管": "Pipe", "接头": "Fitting", "法兰": "Flange",
    "支架": "Bracket", "盖": "Cover", "板": "Plate", "壳体": "Housing",
    "弹簧": "Spring", "阀": "Valve", "阀门": "Valve", "电磁阀": "Solenoid valve",
    "套": "Bushing", "衬套": "Bushing", "护套": "Sleeve",
    # Russian
    "болт": "Bolt", "винт": "Screw", "гайка": "Nut",
    "шайба": "Washer", "штифт": "Pin", "шплинт": "Cotter pin",
    "подшипник": "Bearing", "сальник": "Oil seal", "уплотнение": "Seal",
    "фильтр": "Filter", "масляный фильтр": "Oil filter",
    "воздушный фильтр": "Air filter", "топливный фильтр": "Fuel filter",
    "ремень": "Belt", "цепь": "Chain", "шестерня": "Gear",
    "поршень": "Piston", "кольцо": "Ring", "цилиндр": "Cylinder",
    "коленвал": "Crankshaft", "распредвал": "Camshaft", "шатун": "Connecting rod",
    "помпа": "Pump", "насос": "Pump", "форсунка": "Injector",
    "свеча": "Spark plug", "датчик": "Sensor", "реле": "Relay",
    "предохранитель": "Fuse", "аккумулятор": "Battery",
    "стартер": "Starter", "генератор": "Alternator",
    "радиатор": "Radiator", "вентилятор": "Fan",
    "тормозная колодка": "Brake pad", "колодка": "Brake pad",
    "тормозной диск": "Brake disc", "сцепление": "Clutch",
    "шланг": "Hose", "трубка": "Pipe", "трубa": "Pipe",
    "кронштейн": "Bracket", "крышка": "Cover", "пластина": "Plate",
    "корпус": "Housing", "пружина": "Spring", "клапан": "Valve",
    "втулка": "Bushing", "уплотнитель": "Seal",
}


def _looks_non_ascii(text: str) -> bool:
    """True если в строке есть символы кириллицы или CJK."""
    for ch in text:
        c = ord(ch)
        # Cyrillic + CJK Unified Ideographs ranges
        if 0x0400 <= c <= 0x04FF or 0x4E00 <= c <= 0x9FFF or 0x3400 <= c <= 0x4DBF:
            return True
    return False


def _translate_to_en(text: str) -> str:
    """Переводит CJK/Cyrillic токены на английский по словарю.
    Неизвестные слова оставляет как есть. Если в строке нет non-ASCII —
    возвращает без изменений (fast path)."""
    if not text or not _looks_non_ascii(text):
        return text
    out = text
    # сортируем по длине: длинные термины сначала, чтобы "масляный фильтр"
    # сматчился раньше чем "фильтр".
    for src in sorted(_EN_TERMS.keys(), key=len, reverse=True):
        if src in out:
            out = out.replace(src, _EN_TERMS[src])
        # Capitalized вариант для русских терминов
        if src and src[0].isalpha() and src != src.capitalize():
            cap = src.capitalize()
            if cap in out:
                out = out.replace(cap, _EN_TERMS[src])
    return out


# ── Formula whitelist engine ────────────────────────────────────

import ast
import operator as _op
from datetime import UTC

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
MAX_AI_CALLS_PER_DAY = 50  # увеличил с 3 — словарь + value-detection
                            # покрывают большинство кейсов, AI как fallback


def _read_xlsx_rows(blob: bytes, max_rows: int | None = None):
    """Iter всех строк xlsx как кортежи. Максимум первый лист.

    Hybrid стратегия:
    • max_rows ≤ 100 (preview) → openpyxl read_only (быстрый старт)
    • max_rows > 100 или None (full read) → python-calamine Rust-based
      (на 616k+ ~6× быстрее openpyxl). Fallback на openpyxl.
    """
    use_calamine = max_rows is None or max_rows > 100
    if use_calamine:
        try:
            from python_calamine import CalamineWorkbook
            wb = CalamineWorkbook.from_filelike(io.BytesIO(blob))
            sheet = wb.get_sheet_by_index(0)
            rows = sheet.to_python(nrows=max_rows) if max_rows else sheet.to_python()
            for row in rows:
                yield tuple("" if v is None else str(v).strip() for v in row)
            return
        except ImportError:
            pass
        except Exception as e:
            logger.warning("calamine failed, falling back to openpyxl: %s", e)

    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    sheet = wb.worksheets[0]
    n = 0
    for row in sheet.iter_rows(values_only=True):
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


def _detect_by_values(headers: list[str], sample_rows: list[list[str]],
                       already_mapped: set[str]) -> dict[str, str]:
    """Детекция канонических полей по СОДЕРЖИМОМУ колонок (sample rows).

    Работает когда заголовки непонятные (PRO_ID, ABCD123, etc):
    смотрит на значения, угадывает что это:
      - price_exw — числа с decimal > 1
      - stock — небольшие целые
      - oem_number — alphanumeric codes (дефисы, точки)
      - title — длинные строки с пробелами
      - brand — известные бренды
      - condition — OEM/AFTERMARKET/REMAN/NEW
      - currency — USD/EUR/RUB/CNY
      - weight_kg — small decimals < 100

    already_mapped: каноники которые уже сматчились через словарь —
                    пропускаем (не дублируем).

    Returns: {header: canonical_key}
    """
    if not sample_rows:
        return {}

    import re as _re
    KNOWN_BRANDS = {
        "epiroc", "caterpillar", "cat", "komatsu", "hitachi", "liebherr",
        "sandvik", "atlas copco", "volvo", "kobelco", "doosan", "hyundai",
        "cummins", "deutz", "perkins", "bosch", "bobcat", "jcb",
    }
    CONDITION_VALUES = {"oem", "original", "aftermarket", "reman", "new",
                         "used", "remanufactured"}
    CURRENCY_VALUES = {"usd", "eur", "rub", "cny", "rmb", "jpy", "kzt"}

    n_cols = len(headers)
    n_rows = len(sample_rows)
    if n_rows == 0:
        return {}

    # Собираем колонки (по-столбцово)
    cols = [[] for _ in range(n_cols)]
    for row in sample_rows:
        for i in range(n_cols):
            if i < len(row):
                v = row[i]
                cols[i].append("" if v is None else str(v).strip())
            else:
                cols[i].append("")

    detected: dict[str, str] = {}
    used_canonical = set(already_mapped)

    for i, vals in enumerate(cols):
        if not vals:
            continue
        non_empty = [v for v in vals if v]
        if not non_empty:
            continue
        header = headers[i] if i < len(headers) else f"col{i}"

        # Стрипуем единицы измерения для числовых проверок
        decimals = []
        for v in non_empty:
            m = _re.search(r"-?\d+[.,]?\d*", v.replace(" ", ""))
            if m:
                try:
                    decimals.append(float(m.group(0).replace(",", ".")))
                except ValueError:
                    pass

        # Считаем характеристики
        all_decimal_ratio = len(decimals) / len(non_empty)
        avg_len = sum(len(v) for v in non_empty) / len(non_empty)
        has_spaces = sum(1 for v in non_empty if " " in v) / len(non_empty)
        has_dashes = sum(1 for v in non_empty if "-" in v) / len(non_empty)

        # ── currency
        if "currency" not in used_canonical:
            if all(v.lower() in CURRENCY_VALUES for v in non_empty):
                detected[header] = "currency"; used_canonical.add("currency"); continue

        # ── condition
        if "condition" not in used_canonical:
            if all(v.lower() in CONDITION_VALUES for v in non_empty):
                detected[header] = "condition"; used_canonical.add("condition"); continue

        # ── brand
        if "brand" not in used_canonical:
            if all(v.lower() in KNOWN_BRANDS for v in non_empty):
                detected[header] = "brand"; used_canonical.add("brand"); continue

        # ── price_exw — все числа, среднее > 1
        if "price" not in used_canonical and all_decimal_ratio >= 0.9:
            avg_dec = sum(decimals) / max(len(decimals), 1)
            if avg_dec > 1 and max(decimals) > 1:
                detected[header] = "price"
                used_canonical.add("price"); continue

        # ── weight (decimals < 100 in average)
        if "weight" not in used_canonical and all_decimal_ratio >= 0.9:
            avg_dec = sum(decimals) / max(len(decimals), 1)
            if avg_dec < 100:
                detected[header] = "weight"
                used_canonical.add("weight"); continue

        # ── stock — целые числа
        if "stock" not in used_canonical and all_decimal_ratio >= 0.9:
            if all(d == int(d) and 0 <= d < 10000 for d in decimals):
                detected[header] = "stock"; used_canonical.add("stock"); continue

        # ── part_number — alphanumeric codes (дефисы/точки, короткие)
        if "part_number" not in used_canonical:
            if has_dashes >= 0.5 and avg_len <= 25 and has_spaces < 0.3:
                detected[header] = "part_number"
                used_canonical.add("part_number"); continue
            # Числовые коды (Komatsu OEM)
            if all_decimal_ratio >= 0.8 and avg_len >= 6 and has_spaces < 0.2:
                detected[header] = "part_number"
                used_canonical.add("part_number"); continue

        # ── description/title — длинная строка с пробелами
        if "description" not in used_canonical:
            if has_spaces >= 0.4 or avg_len > 15:
                detected[header] = "description"
                used_canonical.add("description"); continue

    return detected


def _smart_mapping(headers: list[str], sample_rows: list[list[str]],
                    seller=None
                    ) -> tuple[dict[str, str], list[str], bool, str]:
    """Умная автоматическая загрузка прайса.

      1. Словарь COLUMN_MAP + LearnedColumnSynonym (БД)
      2. Value-based детекция по содержимому колонок (для unknown headers)
      3. Для оставшихся — AI-запрос (max 20 заголовков)
      4. AI-ответы → learn_synonym() в БД
      5. Возвращает (mapping_std, unknown, ai_called, status)
    """
    from .price_mappings import (
        CANONICAL_TO_STD,
        COLUMN_MAP,
        learn_synonym,
        load_learned_lookup,
        match_headers,
    )

    learned = load_learned_lookup()
    canonical_map, unknown_headers = match_headers(headers, learned=learned)

    # Value-based fallback для нераспознанных через словарь
    if unknown_headers and sample_rows:
        already = set(canonical_map.keys())
        by_values = _detect_by_values(headers, sample_rows, already)
        for header, canonical in by_values.items():
            if canonical not in canonical_map and header in unknown_headers:
                canonical_map[canonical] = header
                unknown_headers.remove(header)
                # Learn для будущих импортов
                try:
                    learn_synonym(canonical, header, source="value-detect")
                except Exception:
                    pass

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
    # SQLite ограничивает WHERE IN до 999 параметров — режем на чанки.
    refs = []
    CHUNK = 900
    for i in range(0, len(oems), CHUNK):
        batch = oems[i:i + CHUNK]
        refs.extend(PartReference.objects.filter(oem_number__in=batch))
    qs = sorted(refs, key=lambda r: (r.oem_number, -float(r.confidence or 0)))
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
                "cross_numbers": ref.cross_numbers or None,
                "country_of_origin": ref.country_of_origin or None,
                "source":    ref.source,
                "confidence": ref.confidence,
                "_score":    score,
            }
    for v in best.values():
        v.pop("_score", None)
    return best


def _supplier_wide_value(key: str, mapping: dict, constants: dict | None) -> str:
    """Возвращает supplier-wide константное значение для поля:
    приоритет constants → mapping['fix:...'], '' если нигде нет."""
    c = (constants or {}).get(key)
    if c not in (None, "", 0):
        return str(c).strip()
    m = (mapping or {}).get(key, "")
    if isinstance(m, str) and m.startswith("fix:"):
        return m[4:].strip()
    return ""


def _resolve_warehouse_for_import(seller, mapping: dict, constants: dict | None,
                                    filename: str = ""):
    """Создаёт НОВЫЙ виртуальный склад на каждую загрузку прайса.

    Правило: «один импорт — один склад». Даже если ports+address совпадают
    с существующим складом — это отдельная загрузка, отдельная папка.
    Продавец сам может потом переименовать или объединить.

    Возвращает SellerWarehouse (никогда None — всегда создаёт папку).
    """
    from marketplace.models import SellerWarehouse
    sea = _supplier_wide_value("sea_port", mapping, constants)
    air = _supplier_wide_value("air_port", mapping, constants)
    addr = _supplier_wide_value("warehouse_address", mapping, constants)
    currency = _supplier_wide_value("currency", mapping, constants) or "USD"

    # Страна выводится из ISO-кода в начале портового кода (TRMER → TR)
    cc = ""
    for src in (sea, air):
        if src and len(src) >= 2:
            head = src.split()[0]
            if len(head) >= 2 and head[:2].isalpha():
                cc = head[:2].upper()
                break

    # Автоимя: «Склад #N · Страна · Город» либо «Склад #N · <filename>»
    idx = SellerWarehouse.objects.filter(seller=seller).count() + 1
    country_label = {
        "TR": "Турция", "CN": "Китай", "RU": "Россия",
        "AE": "ОАЭ", "NL": "Нидерланды", "KZ": "Казахстан",
    }.get(cc, cc or "")
    # Город — третий токен в портовой строке (Анкара, Мерсин, ...)
    city = ""
    for src in (sea, air):
        parts = [p.strip() for p in src.split("·")]
        if len(parts) >= 3:
            city = parts[2]
            break
    name_parts = [f"Склад #{idx}"]
    if country_label:
        name_parts.append(country_label)
    if city:
        name_parts.append(city)
    if len(name_parts) == 1 and filename:
        # Если логистики нет — используем имя файла как fallback
        short = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0][:50]
        name_parts.append(short)
    name = " · ".join(name_parts)

    return SellerWarehouse.objects.create(
        seller=seller, name=name[:120],
        country_code=cc[:2], sea_port=sea[:120], air_port=air[:120],
        address=addr[:1000], currency=currency[:3] or "USD",
    )


def _import_file(import_obj, mapping: dict[str, str], blob: bytes,
                 transform_rules: dict | None = None,
                 constants: dict | None = None,
                 warehouse=None):
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
    # Constants имеют приоритет над любыми дефолтами (свежий ответ юзера
    # должен перезаписывать значения из сохранённого профиля).
    # Для supplier-wide логистики (порты, склад) constants ВСЕГДА выигрывают
    # даже над замапленной колонкой — потому что эти поля обязаны быть одинаковы
    # для всей загрузки, и колонка в маркетплейс-XLSX часто бывает пустой.
    SUPPLIER_WIDE_OVERRIDE = {"sea_port", "air_port", "warehouse_address"}
    for fld, val in constants.items():
        if val is None or val == "":
            continue
        if fld in col_idx and fld not in SUPPLIER_WIDE_OVERRIDE:
            continue  # колонка из файла — приоритет
        # Если supplier-wide constant есть — удаляем col_idx чтобы fixed_vals выиграл
        if fld in col_idx and fld in SUPPLIER_WIDE_OVERRIDE:
            col_idx.pop(fld, None)
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
    brand_cache: dict[str, Brand] = {
        b.name.lower(): b for b in Brand.objects.all()
    }
    brand_cache.setdefault("generic", generic_brand)

    # Fallback-бренд: если Brand-колонка пустая, пробуем определить по имени
    # файла (например komatsu*.xlsx → Komatsu, sandvik*.xlsx → Sandvik).
    filename_brand = None
    known_brands_l = ["epiroc", "caterpillar", "komatsu", "hitachi", "sandvik",
                       "liebherr", "atlas copco"]
    fname_lc = (getattr(import_obj, "filename", "") or "").lower()
    fname_clean = fname_lc.replace("_", "").replace(" ", "").replace("-", "")
    for b in known_brands_l:
        if b.replace(" ", "") in fname_clean:
            key = b
            filename_brand = brand_cache.get(key)
            if not filename_brand:
                filename_brand = Brand.objects.filter(name__iexact=b).first()
                if filename_brand:
                    brand_cache[key] = filename_brand
            break

    # Прогресс импорта для polling из UI
    from django.core.cache import cache
    progress_key = f"import_progress_{import_obj.id}"
    cache.set(progress_key, {"current": 0, "total": 0, "running": True}, 600)

    # ───────── Pass 1: парсим все строки в payload'ы ─────────
    payloads: list[dict] = []
    for row_n, row in enumerate(_read_all(import_obj.filename, blob), start=2):
            # Обновление прогресса каждые 500 строк во время парсинга:
            # без этого UI висел бы на "0 строк" пока разбираем 160k XLSX.
            if row_n % 500 == 0:
                cache.set(progress_key, {
                    "current": len(payloads),
                    "total": row_n,
                    "phase": "parsing",
                    "running": True,
                }, 600)
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
            # Fallback: если в файле пустое название — берём OEM как title
            # (всё равно лучше чем пропустить позицию)
            if oem and not title:
                title = oem
            # Переводим title на английский для единообразия каталога
            # и сопоставления с конкурентами. Если уже ASCII — fast-path.
            if title:
                title = _translate_to_en(title)
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

            brand_name = _translate_to_en((get("brand") or "").strip())
            if brand_name:
                key = brand_name.lower()
                brand = brand_cache.get(key)
                if not brand:
                    brand = Brand.objects.create(
                        name=brand_name[:200],
                        slug=slugify(brand_name)[:200] or generic_brand.slug,
                    )
                    brand_cache[key] = brand
            elif filename_brand:
                # Колонка Brand пустая, но имя файла подсказывает бренд
                brand = filename_brand
            else:
                brand = generic_brand

            stock = _coerce_int(get("stock")) or 0
            currency = _normalize_currency(get("currency"))
            cond_raw = (get("condition") or "").strip().lower()
            condition = ("oem" if "oem" in cond_raw else
                          "reman" if "reman" in cond_raw else
                          "aftermarket" if "after" in cond_raw else
                          "oem")
            # availability: IN_STOCK / BACKORDER → in_stock / backorder
            avail_raw = (get("availability") or "").strip().lower()
            availability = "backorder" if "backorder" in avail_raw or "back" in avail_raw else "in_stock"
            # manufacturer + visible flag — переводим на английский
            manufacturer = _translate_to_en((get("manufacturer") or "")[:200])
            mvis_raw = (get("manufacturer_visible") or "").strip().lower()
            manufacturer_visible = mvis_raw not in ("нет", "no", "false", "0", "hidden", "скрыть")
            cross_number = (get("cross_number") or "")[:500]
            price_fob_sea = _coerce_decimal(get("price_fob_sea")) or Decimal("0")
            price_fob_air = _coerce_decimal(get("price_fob_air")) or Decimal("0")
            warehouse_addr = (get("warehouse_address") or "")[:255]
            sea_port = (get("sea_port") or "")[:120]
            air_port = (get("air_port") or "")[:120]
            # AI-оценки per-part — fast-path skip если их нет (типичный случай).
            # Когда есть — приоритет: файл > AI > fix-дефолт.
            ai_est = ai_estimates.get(oem) if ai_estimates else None

            def _dim_fast(field, default_val):
                raw = _coerce_decimal(get(field))
                return raw if raw and raw > 0 else default_val

            def _dim_with_ai(field, default_val, ai_key):
                if field in col_idx:
                    raw = _coerce_decimal(get(field))
                    if raw and raw > 0:
                        return raw
                if ai_est and ai_est.get(ai_key):
                    try:
                        v = Decimal(str(ai_est[ai_key]))
                        if v > 0:
                            return v
                    except Exception:
                        pass
                raw = _coerce_decimal(get(field))
                return raw if raw and raw > 0 else default_val

            _dim = _dim_with_ai if ai_est else _dim_fast
            if ai_est:
                weight = _dim_with_ai("weight_kg", Decimal("0.5"), "weight_kg")
                length = _dim_with_ai("length_cm", Decimal("1.0"), "length_cm")
                width = _dim_with_ai("width_cm", Decimal("1.0"), "width_cm")
                height = _dim_with_ai("height_cm", Decimal("1.0"), "height_cm")
            else:
                weight = _dim_fast("weight_kg", Decimal("0.5"))
                length = _dim_fast("length_cm", Decimal("1.0"))
                width = _dim_fast("width_cm", Decimal("1.0"))
                height = _dim_fast("height_cm", Decimal("1.0"))

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
                    "availability": availability,
                    "manufacturer": manufacturer,
                    "manufacturer_visible": manufacturer_visible,
                    "cross_numbers": cross_number,
                    "price_fob_sea": price_fob_sea,
                    "price_fob_air": price_fob_air,
                    "warehouse_address": warehouse_addr,
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

    # ───────── Pass 1b: дедупликация внутри файла ─────────────────────
    # Поставщики (Sandvik, Atlas Copco) часто пишут одну позицию N раз
    # с Quantity=1 вместо одной строки с Quantity=N. Если цена совпадает —
    # суммируем количества в одну позицию. Если цены различаются — берём
    # первую и логируем как ошибку (это или опечатка, или две модификации).
    merged_payloads: dict[str, dict] = {}
    merged_count = 0
    price_conflicts = 0
    for pl in payloads:
        key = pl["oem"].lower()
        if key not in merged_payloads:
            merged_payloads[key] = pl
            continue
        # Дубль: проверяем цены
        existing_pl = merged_payloads[key]
        cur_price = existing_pl["fields"].get("price")
        new_price = pl["fields"].get("price")
        if cur_price == new_price:
            # Одинаковая цена → одна позиция. Qty берём MAX, не сумму:
            # поставщики типа Sandvik пишут один OEM в N строках с уже
            # агрегированным Qty в каждой (например BH00022188 × 5 строк
            # с Qty=22). Сумма (110) даст ложный остаток, MAX (22) — верный.
            cur_qty = existing_pl["fields"].get("stock_quantity", 0) or 0
            new_qty = pl["fields"].get("stock_quantity", 0) or 0
            existing_pl["fields"]["stock_quantity"] = max(cur_qty, new_qty)
            merged_count += 1
        else:
            # Разные цены → не сливаем, отмечаем в errors
            price_conflicts += 1
            if len(errors) < 50:
                errors.append({
                    "row": pl["row_n"], "oem": pl["oem"][:60],
                    "reason": f"price conflict: {cur_price} vs {new_price}",
                })
    payloads = list(merged_payloads.values())
    if merged_count:
        logger.info("In-file dedupe: merged %d duplicate rows by Qty sum", merged_count)
    if price_conflicts:
        logger.warning("In-file dedupe: %d price conflicts skipped", price_conflicts)
    # Сохраним статистику для отчёта в UI
    import_obj._dedupe_stats = {
        "merged": merged_count, "price_conflicts": price_conflicts,
        "unique_oems": len(payloads),
    }

    # ───────── Pass 2a: только {oem_lower: part_id} — не полный Part ─────
    # Раньше загружали полные Part объекты (десятки полей × 600k = много
    # RAM и Python overhead). Для классификации create/update нужен только
    # ID существующей записи. values_list в 5-10× быстрее .objects.filter().
    # С составным индексом (seller_id, oem_number) чанк в 900 — почти O(1).
    oems = [p["oem"] for p in payloads]
    existing_id_by_oem: dict[str, int] = {}
    CHUNK = 900
    if oems:
        cache.set(progress_key, {
            "current": 0, "total": len(oems), "phase": "matching",
            "running": True,
        }, 600)
        seller_id = seller.id
        last_progress = 0
        for i in range(0, len(oems), CHUNK):
            batch = oems[i:i + CHUNK]
            for pid, oem in (Part.objects
                              .filter(seller_id=seller_id, oem_number__in=batch)
                              .values_list("id", "oem_number")):
                existing_id_by_oem[oem.lower()] = pid
            # Прогресс каждый ~10k OEM — чаще чем раньше (раз в 9k → 5k),
            # пользователь видит непрерывное движение, нет «зависов».
            if (i - last_progress) >= 5000:
                cache.set(progress_key, {
                    "current": min(i + CHUNK, len(oems)), "total": len(oems),
                    "phase": "matching", "running": True,
                }, 600)
                last_progress = i

    # ───────── Pass 2b: enrichment из PartReference — fast-path skip ────
    # Если эталонная база пуста для данного seller'а — пропускаем целиком.
    # Эта таблица почти всегда пустая в demo/прод, не тратим время.
    from marketplace.models import PartReference
    has_refs = PartReference.objects.filter(oem_number__in=oems[:CHUNK]).exists()
    reference_hits = 0
    if has_refs:
        reference_data = _lookup_reference_data(oems)
        for pl in payloads:
            ref = reference_data.get(pl["oem"])
            if not ref:
                continue
            fields = pl["fields"]
            applied = False
            for key, target in (("weight_kg", "gross_weight_kg"),
                                  ("length_cm", "length_cm"),
                                  ("width_cm", "width_cm"),
                                  ("height_cm", "height_cm")):
                if key in col_idx:
                    continue
                if ref.get(key) is not None:
                    fields[target] = Decimal(str(ref[key]))
                    applied = True
            if "cross_number" not in col_idx and ref.get("cross_numbers"):
                if not (fields.get("cross_numbers") or "").strip():
                    fields["cross_numbers"] = ref["cross_numbers"][:500]
                    applied = True
            if applied:
                reference_hits += 1
    import_obj._enrichment_stats = {"reference_hits": reference_hits}

    # ───────── Pass 3: split create/update без построения Part объектов ──
    # Раньше создавали Part(seller=...) и setattr — Python overhead.
    # Теперь только разделяем payloads на 2 списка по наличию ID, всё
    # дальнейшее — кортежи прямо из pl["fields"].
    to_create_payloads: list = []
    to_update_payloads: list = []  # (part_id, fields_dict)
    for pl in payloads:
        pid = existing_id_by_oem.get(pl["oem"].lower())
        if pid:
            to_update_payloads.append((pid, pl["fields"]))
        else:
            to_create_payloads.append(pl["fields"])

    # ───────── Pass 4: BULK INSERT через raw SQL (5-10x быстрее ORM) ─────
    # ORM bulk_create делает много overhead'а на signal'ах, валидации,
    # auto-now полях. Для массового импорта используем raw INSERT
    # через executemany с явной транзакцией.
    from datetime import datetime

    from django.db import connection
    now = datetime.now(tz=UTC)

    insert_cols = [
        "title", "slug", "oem_number", "description", "price", "stock_quantity",
        "condition", "image_url", "seller_id", "brand_id", "category_id",
        "availability", "availability_status", "currency", "incoterm", "moq",
        "production_lead_days", "prep_to_ship_days", "shipping_lead_days",
        "gross_weight_kg", "length_cm", "width_cm", "height_cm",
        "country_of_origin", "cross_numbers",
        "price_fob_sea", "price_fob_air", "warehouse_address", "sea_port", "air_port",
        "hs_code", "backorder_allowed", "mapping_status", "supplier_part_uid",
        "data_updated_at", "is_active", "admin_note",
        "manufacturer", "manufacturer_visible",
        "warehouse_id",
        "created_at", "updated_at",
    ]
    table = Part._meta.db_table
    placeholders = ", ".join(["%s"] * len(insert_cols))
    insert_sql = (f"INSERT INTO {table} ({', '.join(insert_cols)}) "
                   f"VALUES ({placeholders})")

    def _val(obj, attr, default):
        v = getattr(obj, attr, None)
        return v if v is not None else default

    wh_id = warehouse.id if warehouse else None
    # Прогресс между matching и INSERT/UPDATE: пользователь видит что
    # сопоставление закончилось и теперь идёт запись в БД.
    total_pending = len(to_create_payloads) + len(to_update_payloads)
    cache.set(progress_key, {
        "current": 0, "total": total_pending, "phase": "writing",
        "running": True,
    }, 600)
    with transaction.atomic():
        if to_create_payloads:
            rows = []
            for f in to_create_payloads:
                rows.append((
                    f.get("title") or "", f.get("slug") or "",
                    f.get("oem_number"), "",
                    f.get("price"), f.get("stock_quantity") or 0,
                    f.get("condition") or "oem", "",
                    seller.id, f["brand"].id, f["category"].id,
                    f.get("availability") or "in_stock",
                    "active", f.get("currency") or "USD",
                    "FOB", 1, 1, 1, 1,
                    f.get("gross_weight_kg"), f.get("length_cm"),
                    f.get("width_cm"), f.get("height_cm"),
                    "Unknown", f.get("cross_numbers") or "",
                    f.get("price_fob_sea"), f.get("price_fob_air"),
                    f.get("warehouse_address") or "",
                    f.get("sea_port") or "", f.get("air_port") or "",
                    "", False, "auto", "",
                    now, True, "",
                    f.get("manufacturer") or "",
                    f.get("manufacturer_visible", True),
                    wh_id,
                    now, now,  # created_at, updated_at
                ))
            INS_BATCH = 50000
            try:
                with connection.cursor() as cur:
                    for batch_start in range(0, len(rows), INS_BATCH):
                        batch = rows[batch_start:batch_start + INS_BATCH]
                        cur.executemany(insert_sql, batch)
                        created += len(batch)
                        cache.set(progress_key, {
                            "current": created, "total": total_pending,
                            "phase": "writing", "running": True,
                        }, 600)
            except Exception as e:
                logger.exception("bulk INSERT failed for %d rows (batch %d)",
                                  len(rows), batch_start)
                failed += len(rows) - created
                err_entry = {"row": 0, "reason": f"raw insert: {type(e).__name__}: {str(e)[:200]}"}
                errors.insert(0, err_entry)
            cache.set(progress_key, {
                "current": created, "total": len(payloads), "running": True,
            }, 600)

        # Raw SQL bulk UPDATE через executemany — намного быстрее
        # Django bulk_update который генерит CASE WHEN на каждое поле.
        # 160k UPDATE: было 110с (bulk_update), стало ~5с (executemany).
        if to_update_payloads:
            # slug НЕ обновляем — он уникален и генерится из OEM, который
            # совпадает (мы матчим по seller+oem_number). Обновление slug
            # ловит UNIQUE constraint если он отличается от исходного.
            upd_cols = [
                "title", "price", "currency", "stock_quantity", "condition",
                "availability", "manufacturer", "manufacturer_visible",
                "cross_numbers", "price_fob_sea", "price_fob_air",
                "warehouse_address", "sea_port", "air_port",
                "gross_weight_kg", "length_cm", "width_cm", "height_cm",
                "brand_id", "category_id", "is_active",
                "warehouse_id",
                "data_updated_at", "updated_at",
            ]
            update_sql = (f"UPDATE {table} SET "
                           + ", ".join(f"{c} = %s" for c in upd_cols)
                           + " WHERE id = %s")
            upd_rows = []
            for pid, f in to_update_payloads:
                upd_rows.append((
                    f.get("title") or "", f.get("price"),
                    f.get("currency") or "USD",
                    f.get("stock_quantity") or 0, f.get("condition") or "oem",
                    f.get("availability") or "in_stock",
                    f.get("manufacturer") or "",
                    f.get("manufacturer_visible", True),
                    f.get("cross_numbers") or "",
                    f.get("price_fob_sea"), f.get("price_fob_air"),
                    f.get("warehouse_address") or "",
                    f.get("sea_port") or "", f.get("air_port") or "",
                    f.get("gross_weight_kg"), f.get("length_cm"),
                    f.get("width_cm"), f.get("height_cm"),
                    f["brand"].id, f["category"].id, True,
                    wh_id,
                    now, now,
                    pid,
                ))
            # Чанкуем UPDATE на пачки по 50k — каждая пачка ~3-5с в SQLite.
            # Между пачками cache.set обновляет прогресс, чтобы UI не висел
            # на «616473/616473» по 30+ секунд.
            UPD_BATCH = 50000
            try:
                with connection.cursor() as cur:
                    for batch_start in range(0, len(upd_rows), UPD_BATCH):
                        batch = upd_rows[batch_start:batch_start + UPD_BATCH]
                        cur.executemany(update_sql, batch)
                        updated += len(batch)
                        cache.set(progress_key, {
                            "current": created + updated,
                            "total": total_pending,
                            "phase": "writing", "running": True,
                        }, 600)
            except Exception as e:
                logger.exception("bulk UPDATE failed for %d rows (batch %d)",
                                  len(upd_rows), batch_start)
                # Уже успешно записанные UPDATE остаются — failed только
                # для строк которые не дошли до записи.
                failed += len(upd_rows) - updated
                err_entry = {"row": 0, "reason": f"raw update: {type(e).__name__}: {str(e)[:200]}"}
                errors.insert(0, err_entry)
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
            PricelistImport,
            PricelistMapping,
            SupplierImportProfile,
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
        smart_status: str = "ok"  # default; will be set if _smart_mapping called
        transform_rules: dict = {}
        constants: dict = {}
        from_profile = False

        def _file_mapped_count(s):
            return sum(1 for v in (s or {}).values()
                        if v and not (isinstance(v, str) and v.startswith("fix:")))

        def _required_satisfied(s):
            # Профиль засчитывается только если purchase-критичные поля
            # есть среди замапленных колонок (а не один случайный «Quantity»).
            for f in REQUIRED_FIELDS:
                v = (s or {}).get(f)
                if not v or (isinstance(v, str) and v.startswith("fix:")):
                    return False
            return True

        # Supplier-wide поля (порты, склад) НЕ восстанавливаем из профиля —
        # юзер должен выбрать их заново для каждой загрузки. Профиль
        # переиспользуется только для маппинга колонок и formula-правил.
        SUPPLIER_WIDE_RESET = {"sea_port", "air_port", "warehouse_address"}
        suggested = {}
        if profile:
            suggested = {
                k: v for k, v in profile.column_mapping.items()
                if v and (v.startswith("fix:") or v in headers)
                and k not in SUPPLIER_WIDE_RESET
            }
            transform_rules = profile.transform_rules or {}
            constants = {
                k: v for k, v in (profile.constants or {}).items()
                if k not in SUPPLIER_WIDE_RESET
            }
            # Однако supplier-wide колонки В ФАЙЛЕ — это другое: если
            # SeaPort/AirPort/WarehouseAddress есть как заголовки, мапим
            # их детерминированно через словарь (НЕ через профиль, т.е.
            # значения там per-row, а не supplier-wide константа).
            from assistant.price_mappings import CANONICAL_TO_STD, _build_lookup, normalize
            _lookup_local = _build_lookup()
            for h in headers:
                canonical = _lookup_local.get(normalize(h))
                std = CANONICAL_TO_STD.get(canonical) if canonical else None
                if std in SUPPLIER_WIDE_RESET and std not in suggested:
                    suggested[std] = h
            from_profile = True
        # Если профиль не покрывает required-поля файла (oem, title, price) —
        # его маппинг бесполезен (он от другого поставщика). Делаем fresh
        # smart-mapping вместо того чтобы сохранять одну случайную колонку.
        if not profile or not _required_satisfied(suggested):
            prev = PricelistMapping.objects.filter(seller=request.user).first()
            prev_mapped = {}
            if prev and prev.mapping:
                prev_mapped = {k: v for k, v in prev.mapping.items()
                                if v and (v.startswith("fix:") or v in headers)}
            if _required_satisfied(prev_mapped):
                suggested = prev_mapped
                from_profile = False
            else:
                # Свежий smart_mapping (словарь + AI для unknown headers)
                fresh, unknown, ai_called, smart_status = _smart_mapping(
                    headers, sample, seller=request.user,
                )
                suggested = fresh
                from_profile = False
                transform_rules = {}
                constants = {}
        # AI quota exceeded — НЕ фатально. У нас есть результат словаря +
        # value detection. Просто отмечаем для UI чтобы юзер знал что
        # уникальные колонки не дораспознаны AI'ом — пусть поправит руками.
        ai_quota_exceeded = (smart_status == "quota_exceeded")

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

        # total_rows НЕ считаем здесь — это требует прохода всего файла
        # (для 616k Komatsu = +2с). Используем размер файла как proxy.
        # Если нужно точное число — клиент дозапросит отдельно.
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
            "ai_quota_exceeded": ai_quota_exceeded,
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
            PricelistImport,
            PricelistMapping,
            SupplierImportProfile,
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

        # Supplier-wide логистика (склад/порты) — если seller заполнил в KYB-
        # профиле и в текущей загрузке не передал ни колонку, ни константу —
        # подставляем из профиля. Без этого валидатор отклонит загрузку
        # даже когда значение уже известно платформе.
        try:
            kyb = getattr(request.user, "kyb_profile", None)
        except Exception:
            kyb = None
        for _sw_fld in ("warehouse_address", "sea_port", "air_port"):
            if constants.get(_sw_fld) or mapping.get(_sw_fld):
                continue
            if kyb:
                v = getattr(kyb, _sw_fld, "") or ""
                if v:
                    constants[_sw_fld] = v
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

        # Применяем ответы на вопросы: constants ПРИОРИТЕТ над дефолтами.
        # Раньше скипались если fld уже в mapping — но mapping мог содержать
        # пустой default (fix:""), который блокировал юзерский ответ.
        for fld, val in (constants or {}).items():
            if val is None or val == "":
                continue
            cur = mapping.get(fld, "")
            # Перезатираем если: (а) нет в mapping, (б) пустой default fix:""
            #                   (в) колонка не замаплена на файл
            if (not cur
                or (isinstance(cur, str) and cur.startswith("fix:") and
                    cur[4:].strip() in ("", str(FIELD_DEFAULTS.get(fld, ""))))):
                mapping[fld] = f"fix:{val}"

        for f_key in REQUIRED_FIELDS:
            if not mapping.get(f_key):
                return Response(
                    {"error": f"required field '{f_key}' not mapped"}, status=400,
                )

        # Жёсткий чек обязательных supplier-wide полей.
        # Адрес склада — обязателен. Без него рассчитать логистику нельзя.
        SUPPLIER_WIDE_KEYS = {"warehouse_address", "sea_port", "air_port"}

        def _has_value(key: str) -> bool:
            """Проверяет что для supplier-wide поля есть данные:
            - constants из формы (приоритет)
            - fix:VALUE в mapping
            - mapped file column. Для supplier-wide колонок: ВСЕ строки sample
              должны быть заполнены одним значением (иначе констант важнее —
              иначе разольёмся пустотой по 160k строк маркетплейс-формата).
              Для не-supplier-wide: хотя бы одна непустая ячейка.
            """
            c = (constants or {}).get(key)
            if c not in (None, "", 0):
                return True
            m = mapping.get(key, "")
            if isinstance(m, str) and m.startswith("fix:") and m[4:].strip():
                return True
            if m and isinstance(m, str) and m in (imp.headers or []):
                idx = imp.headers.index(m)
                rows = imp.sample_rows or []
                if key in SUPPLIER_WIDE_KEYS:
                    seen = set()
                    for row in rows:
                        cell = str(row[idx]).strip() if idx < len(row) else ""
                        if not cell:
                            return False
                        seen.add(cell)
                    return bool(rows) and len(seen) == 1
                for row in rows:
                    if idx < len(row) and str(row[idx]).strip():
                        return True
            return False

        if not _has_value("warehouse_address"):
            return Response({
                "error": "warehouse_address_required",
                "message": (
                    "Укажите адрес склада отгрузки — обязательное поле. "
                    "В колонке WarehouseAddress нет данных или она не заполнена — "
                    "впишите адрес в форме «📎 общих полей поставщика»."
                ),
            }, status=400)
        if not _has_value("sea_port"):
            return Response({
                "error": "sea_port_required",
                "message": (
                    "Укажите ближайший к складу морпорт отгрузки — обязательное "
                    "поле для расчёта FOB SEA. Впишите в вопросе мастера или в "
                    "форме «📎 общих полей поставщика»."
                ),
            }, status=400)
        if not _has_value("air_port"):
            return Response({
                "error": "air_port_required",
                "message": (
                    "Укажите ближайший к складу аэропорт отгрузки — обязательное "
                    "поле для расчёта FOB AIR. Впишите в вопросе мастера или в "
                    "форме «📎 общих полей поставщика»."
                ),
            }, status=400)

        # Бренд — обязателен. Должен быть в колонке файла (с данными),
        # в constants/fix:VALUE, ИЛИ детектится из имени файла.
        # Без бренда позиции попадут в "Generic" и потеряют сопоставление
        # с конкурентами — нам важны точные данные.
        if not _has_value("brand"):
            # Fallback: имя файла содержит известный бренд?
            fname_lc = (imp.filename or "").lower()
            fname_clean = fname_lc.replace("_", "").replace(" ", "").replace("-", "")
            known = ["epiroc", "caterpillar", "komatsu", "hitachi", "sandvik",
                     "liebherr", "atlascopco"]
            if not any(b in fname_clean for b in known):
                return Response({
                    "error": "brand_required",
                    "message": (
                        "Не удалось определить бренд: колонка Brand пуста, "
                        "имя файла не содержит известного бренда. "
                        "Укажите бренд в смарт-вопросе или назовите файл "
                        "включив бренд (напр. komatsu_pricelist.xlsx)."
                    ),
                }, status=400)
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

        # Виртуальный склад для этой загрузки — папка с фиксированной
        # логистикой. Ищем существующий по совпадению ports+address,
        # иначе создаём новый.
        warehouse = _resolve_warehouse_for_import(request.user, mapping, constants, imp.filename)

        imported, created, updated, failed, errors = _import_file(
            imp, mapping, blob,
            transform_rules=transform_rules,
            constants=constants,
            warehouse=warehouse,
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

        # Категоризация полей:
        #   mandatory      — без этого нельзя сохранить (warehouse_address)
        #   rating_bonus   — необязательно, но повышает рейтинг карточки;
        #                    мы подтягиваем из эталонной базы (catalogs/customs)
        #   optional       — нейтрально, можно дозаполнить в каталоге
        FIELD_LABELS = {
            "weight_kg": "вес", "length_cm": "длина", "width_cm": "ширина",
            "height_cm": "высота", "stock": "остаток",
            "price_fob_sea": "FOB SEA", "price_fob_air": "FOB AIR",
            "warehouse_address": "адрес склада",
            "sea_port": "морпорт", "air_port": "аэропорт",
            "cross_number": "кросс-номер",
        }
        MANDATORY = {"warehouse_address", "sea_port", "air_port"}
        RATING_BONUS = {"length_cm", "width_cm", "height_cm", "cross_number"}

        def _filled(key: str) -> bool:
            # из файла
            val = mapping.get(key, "")
            if val and not (isinstance(val, str) and val.startswith("fix:")):
                return True
            # supplier-wide constant из формы
            cval = (constants or {}).get(key)
            if cval not in (None, "", 0):
                return True
            return False

        missing_mandatory = []
        missing_rating_bonus = []
        missing_optional = []
        for key, label in FIELD_LABELS.items():
            if _filled(key):
                continue
            entry = {"key": key, "label": label}
            if key in MANDATORY:
                missing_mandatory.append(entry)
            elif key in RATING_BONUS:
                missing_rating_bonus.append(entry)
            else:
                missing_optional.append(entry)

        # Legacy combined list — для обратной совместимости со старым UI.
        missing_from_file = (
            missing_mandatory + missing_rating_bonus + missing_optional
        )

        enrichment_stats = getattr(imp, "_enrichment_stats", {}) or {}
        dedupe_stats = getattr(imp, "_dedupe_stats", {}) or {}
        return Response({
            "ok": True,
            "import_id": imp.id,
            "imported": imported,
            "created": created,
            "updated": updated,
            "failed": failed,
            "errors_preview": errors[:10],
            "missing_from_file": missing_from_file,
            "missing_mandatory": missing_mandatory,
            "missing_rating_bonus": missing_rating_bonus,
            "missing_optional": missing_optional,
            "ai_estimated_count": len(imp.ai_estimates or {}),
            "reference_enriched": enrichment_stats.get("reference_hits", 0),
            "merged_duplicates": dedupe_stats.get("merged", 0),
            "price_conflicts": dedupe_stats.get("price_conflicts", 0),
            "unique_oems": dedupe_stats.get("unique_oems", 0),
        })


def _generate_marketplace_xlsx(import_obj, mapping: dict, transform_rules: dict,
                                  constants: dict) -> bytes:
    """Генерирует выходной XLSX в формате маркетплейса (как у claude.ai).

    Использует openpyxl WriteOnlyWorkbook — в 5-10 раз быстрее обычного
    Workbook потому что не создаёт Cell objects в памяти, пишет
    напрямую в zip stream.

    Прогресс пишется в Django cache → виден в UI через polling
    /generate-output-progress/ endpoint.
    """
    from django.core.cache import cache

    OUTPUT_COLS = [
        ("PartNumber",       "oem_number"),
        ("CrossNumber",      "cross_number"),
        ("Brand",            "brand"),
        ("Name",             "title"),
        ("Manufacturer",     "manufacturer"),
        ("Quantity",         "stock"),
        ("Condition",        "condition"),
        ("Availability",     "availability"),
        ("Price_EXW",        "price_exw"),
        ("Currency",         "currency"),
        ("WarehouseAddress", "warehouse_address"),
        ("Price_FOB_SEA",    "price_fob_sea"),
        ("Price_FOB_AIR",    "price_fob_air"),
        ("SeaPort",          "sea_port"),
        ("AirPort",          "air_port"),
        ("Weight",           "weight_kg"),
        ("Length",           "length_cm"),
        ("Width",            "width_cm"),
        ("Height",           "height_cm"),
    ]

    headers = import_obj.headers
    ai_estimates = getattr(import_obj, "ai_estimates", None) or {}

    # col_idx + fixed_vals (как в _import_file)
    col_idx: dict[str, int] = {}
    fixed_vals: dict[str, str] = {}
    for fld, dflt in FIELD_DEFAULTS.items():
        fixed_vals[fld] = str(dflt)
    for fld, val in (mapping or {}).items():
        if not val:
            continue
        if isinstance(val, str) and val.startswith("fix:"):
            fixed_vals[fld] = val[4:]
        elif val in headers:
            col_idx[fld] = headers.index(val)
            fixed_vals.pop(fld, None)
    # Constants имеют приоритет — даже если mapping уже содержит fix:default,
    # юзерский ответ должен перебить (свежий ответ юзера > старый профиль).
    for fld, val in (constants or {}).items():
        if val is None or val == "":
            continue
        if fld in col_idx:
            continue  # колонка замаплена на файл — это приоритет
        fixed_vals[fld] = str(val)

    # xlsxwriter — в 2-3 раза быстрее openpyxl для массовой записи.
    # constant_memory=True пишет напрямую в файл (не накапливает в RAM).
    import xlsxwriter
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "in_memory": True})
    ws = wb.add_worksheet("Pricelist")

    hdr_fmt = wb.add_format({
        "bold": True, "font_color": "#FFFFFF", "bg_color": "#2D7A3E",
        "align": "center", "valign": "vcenter",
    })

    # Header row
    for col_n, (label, _key) in enumerate(OUTPUT_COLS):
        ws.write(0, col_n, label, hdr_fmt)
        ws.set_column(col_n, col_n, max(12, len(label) + 2))
    ws.set_row(0, 22)

    # Прогресс — для polling в UI
    progress_key = f"generate_output_progress_{import_obj.id}"
    cache.set(progress_key, {"current": 0, "total": 0, "running": True}, 600)

    try:
        with import_obj.file_obj.open("rb") as fh:
            blob = fh.read()
    except Exception:
        cache.set(progress_key, {"current": 0, "total": 0, "running": False}, 60)
        wb.close()
        raise

    written = 0
    row_idx = 1  # после header
    DIM_KEYS = ("weight_kg", "length_cm", "width_cm", "height_cm")
    PREVIEW_LIMIT = 100  # сохраняем HTML preview первых N строк
    preview_rows_html = []
    for row in _read_all(import_obj.filename, blob):
        if not any(str(c).strip() for c in row):
            continue

        # row_vars для формул
        row_vars = {}
        for fld in col_idx:
            idx = col_idx[fld]
            raw = str(row[idx]).strip() if idx < len(row) else ""
            dec = _coerce_decimal(raw)
            row_vars[fld] = dec if dec is not None else raw

        def get(field):
            if field in col_idx:
                idx = col_idx[field]
                raw = str(row[idx]).strip() if idx < len(row) else ""
            else:
                raw = fixed_vals.get(field, "")
            return _apply_transform(field, raw, transform_rules or {}, row_vars)

        oem = get("oem_number")
        if not oem:
            continue
        ai_est = ai_estimates.get(oem) if ai_estimates else None

        def _dim(field, ai_key):
            if field in col_idx:
                raw = _coerce_decimal(get(field))
                if raw and raw > 0:
                    return str(raw)
            if ai_est and ai_est.get(ai_key):
                return str(ai_est[ai_key])
            raw = _coerce_decimal(get(field))
            return str(raw) if raw and raw > 0 else ""

        # write_row — быстрее чем write по одной cell
        # Текстовые поля переводим на английский (каталог международный).
        EN_FIELDS = {"title", "manufacturer", "brand", "warehouse_address",
                     "sea_port", "air_port", "condition", "availability"}
        row_values = []
        for _label, key in OUTPUT_COLS:
            if key in DIM_KEYS:
                row_values.append(_dim(key, key))
            else:
                val = get(key) or ""
                if key in EN_FIELDS and isinstance(val, str):
                    val = _translate_to_en(val)
                row_values.append(val)
        ws.write_row(row_idx, 0, row_values)
        # Накопляем HTML preview первых строк параллельно с записью в XLSX
        if written < PREVIEW_LIMIT:
            from html import escape as _h
            cells = "".join(
                f"<td>{_h(str(v)[:50])}</td>" for v in row_values
            )
            preview_rows_html.append(f"<tr>{cells}</tr>")
        row_idx += 1
        written += 1
        if written % 1000 == 0:
            cache.set(progress_key, {
                "current": written, "total": 0, "running": True,
            }, 600)

    wb.close()
    cache.set(progress_key, {"current": written, "total": written, "running": False}, 60)

    # Кэшируем preview HTML в БД — мгновенный показ без openpyxl re-parse
    from html import escape as _h
    headers_html = "".join(f"<th>{_h(label)}</th>" for label, _ in OUTPUT_COLS)
    truncated = written > PREVIEW_LIMIT
    note = (f'<div class="opx-note">Показаны первые {PREVIEW_LIMIT} '
             f'из {written} строк</div>') if truncated else ""
    preview_html = (
        '<style>'
        'body{margin:0;padding:14px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#fff;}'
        '.opx-note{font-size:11.5px;color:rgba(0,0,0,0.55);margin-bottom:10px;font-style:italic;}'
        'table{border-collapse:collapse;width:100%;font-size:11.5px;font-family:"SF Mono",Menlo,monospace;}'
        'th{background:#2D7A3E;color:#fff;padding:6px 8px;text-align:left;position:sticky;top:0;font-weight:600;letter-spacing:0.02em;}'
        'td{padding:4px 8px;border-bottom:1px solid rgba(0,0,0,0.05);color:#1a1a1a;white-space:nowrap;}'
        'tr:nth-child(even) td{background:rgba(45,122,62,0.03);}'
        '</style>'
        + note
        + f'<table><thead><tr>{headers_html}</tr></thead>'
        + '<tbody>' + ''.join(preview_rows_html) + '</tbody></table>'
    )
    try:
        import_obj.output_preview_html = preview_html
        import_obj.output_total_rows = written
        import_obj.save(update_fields=["output_preview_html", "output_total_rows"])
    except Exception:
        pass

    return buf.getvalue()


HARDCODED_QUESTIONS = [
    {
        "field": "brand",
        "question": "Бренд / производитель?",
        "options": ["Epiroc", "Caterpillar", "Komatsu", "Hitachi", "Sandvik"],
        "default": "",
        "placeholder": "Или впишите свой бренд",
        "apply_as": "constant",
    },
    {
        "field": "condition",
        "question": "Тип товара (Condition)?",
        "options": ["OEM", "AFTERMARKET", "REMAN"],
        "default": "OEM",
        "apply_as": "constant",
    },
    {
        "field": "availability",
        "question": "Наличие?",
        "options": ["IN_STOCK", "BACKORDER"],
        "default": "IN_STOCK",
        "apply_as": "constant",
    },
    {
        "field": "manufacturer",
        "question": "Завод-производитель? Для OEM — OEM-завод, для AFTERMARKET — завод аналога, для REMAN — компания восстановления.",
        "options": [],
        "default": "",
        "placeholder": "напр.: Epiroc Sweden AB, или завод аналога",
        "apply_as": "constant",
    },
    {
        "field": "manufacturer_visible",
        "question": "Показывать завод клиенту? (для публичных брендов — да; для непубличных — нет, останется только внутри)",
        "options": ["Да", "Нет"],
        "default": "Да",
        "apply_as": "constant",
    },
    {
        "field": "warehouse_address",
        "question": "Адрес склада отгрузки? (обязательно — нужен для расчёта логистики до покупателя)",
        "options": [],
        "default": "",
        "placeholder": "напр.: Shenzhen, China — 1 Industrial Rd",
        "required": True,
        "apply_as": "constant",
    },
    {
        "field": "sea_port",
        "question": "Ближайший к складу морпорт отгрузки? (обязательно — для расчёта FOB SEA)",
        "options": [],
        "default": "",
        "placeholder": "напр.: Shanghai (SHA), Hamburg (HAM), Vladivostok (VVO)",
        "required": True,
        "apply_as": "constant",
    },
    {
        "field": "air_port",
        "question": "Ближайший к складу аэропорт отгрузки? (обязательно — для расчёта FOB AIR)",
        "options": [],
        "default": "",
        "placeholder": "напр.: PVG (Shanghai Pudong), FRA (Frankfurt), SVO (Sheremetyevo)",
        "required": True,
        "apply_as": "constant",
    },
    {
        "field": "price_fob_sea",
        "question": "Наценка FOB SEA (морем) к цене EXW?",
        "options": ["+5%", "+10%", "+15%"],
        "default": "+10%",
        "apply_as": "formula",
    },
    {
        "field": "price_fob_air",
        "question": "Наценка FOB AIR (авиа) к цене EXW?",
        "options": ["+7%", "+15%", "+20%"],
        "default": "+15%",
        "apply_as": "formula",
    },
]


def _ai_smart_questions(headers: list[str], sample_rows: list[list[str]],
                         total_rows: int | None, mapping: dict[str, str],
                         filename: str = "", seller=None) -> dict:
    """Возвращает захардкоженный список вопросов + краткое intro.

    AI-генерация заменена на детерминированный список — у нас фикс-набор
    статусов товара (OEM/AFTERMARKET/REMAN), наличия (IN_STOCK/BACKORDER),
    наценок (FOB SEA: +5/+10/+15%, FOB AIR: +7/+15/+20%).
    """
    # Бренд по умолчанию подставляем из имени файла если узнаём (Epiroc, ...)
    known_brands = ["Epiroc", "Caterpillar", "Komatsu", "Hitachi", "Sandvik",
                     "Liebherr", "Atlas Copco"]
    detected_brand = ""
    fname_lower = (filename or "").lower()
    for b in known_brands:
        if b.lower().replace(" ", "") in fname_lower.replace("_", "").replace(" ", ""):
            detected_brand = b
            break

    # Если поле уже замаплено на колонку файла — вопрос задавать не нужно.
    # mapping[field] = "Column" — из файла; "fix:..." — дефолт, спрашиваем.
    SUPPLIER_WIDE_KEYS = {"warehouse_address", "sea_port", "air_port"}

    def _from_file(field: str) -> bool:
        """True если поле замаплено на колонку файла И значение в порядке.
        Для обычных полей: достаточно хотя бы одной непустой ячейки.
        Для supplier-wide (склад/порты): колонка должна быть полностью
        заполнена одним и тем же значением — иначе нужен один константный
        ответ от юзера, чтобы не разлились пустые/смешанные адреса на 160k
        строк (типичный кейс маркетплейс-формата с пустым WarehouseAddress).
        """
        v = (mapping or {}).get(field)
        if not v or (isinstance(v, str) and v.startswith("fix:")):
            return False
        if v not in (headers or []):
            return False
        idx = headers.index(v)
        rows = sample_rows or []
        if field in SUPPLIER_WIDE_KEYS:
            seen = set()
            for row in rows:
                cell = str(row[idx]).strip() if idx < len(row) else ""
                if not cell:
                    return False  # хоть одна пустая — не подходит
                seen.add(cell)
            return bool(rows) and len(seen) == 1
        for row in rows:
            if idx < len(row) and str(row[idx]).strip():
                return True
        return False

    # Supplier-wide поля (склад, порты) подтягиваем из KYB-профиля если есть —
    # это глобальные настройки логистики, не должны спрашиваться каждый раз.
    profile_supplier_wide = {}
    if seller is not None:
        try:
            kyb = getattr(seller, "kyb_profile", None)
            if kyb:
                for fld in ("warehouse_address", "sea_port", "air_port"):
                    v = getattr(kyb, fld, "") or ""
                    if v:
                        profile_supplier_wide[fld] = v
        except Exception:
            pass

    questions = []
    for q in HARDCODED_QUESTIONS:
        field = q["field"]
        if _from_file(field):
            continue  # значение придёт из колонки файла
        # supplier-wide поле уже есть в KYB-профиле — не спрашиваем повторно.
        if field in SUPPLIER_WIDE_KEYS and profile_supplier_wide.get(field):
            continue
        # manufacturer_visible — управляющий флаг, не data-поле. Если
        # manufacturer пришёл из файла, презюмируем «показывать = Да»
        # (раз положили в файл, значит хотят показывать) и не спрашиваем.
        if field == "manufacturer_visible" and _from_file("manufacturer"):
            continue
        q_copy = dict(q)
        if q_copy["field"] == "brand" and detected_brand:
            q_copy["default"] = detected_brand
        questions.append(q_copy)

    # Базовое intro
    rows_label = f"{total_rows} позиций" if total_rows else f"{len(headers)} колонок"
    if questions:
        intro = (f"📋 Я распознал в файле **{rows_label}**. "
                 f"Уточню {len(questions)} "
                 f"{'деталь' if len(questions) == 1 else 'детали' if 2 <= len(questions) <= 4 else 'деталей'}"
                 f" чтобы корректно заполнить карточки товара:")
    else:
        intro = (f"📋 Файл распознан полностью ({rows_label}). "
                 f"Уточнять нечего — все колонки на месте.")

    return {"intro": intro, "questions": questions}


def _ai_smart_questions_legacy_AI(headers: list[str], sample_rows: list[list[str]],
                         total_rows: int | None, mapping: dict[str, str],
                         filename: str = "") -> dict:
    """[legacy] AI-генерация вопросов через Claude tool use.

    Оставлено для возврата если понадобится динамика. Заменено на
    _ai_smart_questions с захардкоженным списком.
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
            "total": progress.get("total", 0),
            "phase": progress.get("phase", ""),
            "running": progress.get("running", False),
            "status": imp.status,
        })


class PricelistGenerateOutputProgressView(APIView):
    """GET /api/assistant/upload-pricelist/<id>/generate-output-progress/

    Polling endpoint для прогресс-бара генерации output XLSX.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id):
        from django.core.cache import cache
        progress = cache.get(f"generate_output_progress_{import_id}") or {}
        return Response({
            "current": progress.get("current", 0),
            "total": progress.get("total", 0),
            "running": progress.get("running", False),
        })


class PricelistOutputPreviewView(APIView):
    """GET /api/assistant/upload-pricelist/<id>/output-preview/

    Возвращает HTML preview сгенерированного output_file (XLSX).
    Используется для side panel в чате (как у claude.ai).
    """
    permission_classes = [IsAuthenticated]

    PREVIEW_ROWS = 100  # ограничиваем превью

    def get(self, request, import_id):
        from django.http import HttpResponse

        from marketplace.models import PricelistImport
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)
        if not imp.output_file:
            return Response({"error": "output file not generated"}, status=404)

        # Если есть кэш HTML preview — отдаём моментально (10-100 мс
        # вместо 15+ сек openpyxl re-parse 36MB файла).
        if imp.output_preview_html:
            # Перевод применяется и к старому кэшу (генерация была до фикса
            # _translate_to_en). _looks_non_ascii fast-path делает это
            # бесплатным для уже-английского HTML.
            body = _translate_to_en(imp.output_preview_html)
            html = (
                '<!doctype html><html><head><meta charset="utf-8"></head>'
                '<body>' + body + '</body></html>'
            )
            return HttpResponse(html, content_type="text/html; charset=utf-8")

        from openpyxl import load_workbook
        try:
            with imp.output_file.open("rb") as fh:
                wb = load_workbook(io.BytesIO(fh.read()), read_only=True, data_only=True)
            ws = wb.worksheets[0]
        except Exception as e:
            return Response({"error": f"cannot read XLSX: {e}"}, status=500)

        rows_html = []
        total = 0
        for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if row_idx > self.PREVIEW_ROWS + 1:
                # просто считаем общее количество
                total = row_idx - 1
                continue
            cells = []
            for col_idx, val in enumerate(row):
                v = "" if val is None else str(val)
                if len(v) > 50:
                    v = v[:50] + "…"
                tag = "th" if row_idx == 1 else "td"
                cls = ' class="opx-num"' if row_idx == 1 else ''
                cells.append(f"<{tag}{cls}>{v}</{tag}>")
            rows_html.append(f"<tr>{''.join(cells)}</tr>")
        if total == 0:
            total = ws.max_row - 1 if ws.max_row else 0
        try:
            wb.close()
        except Exception:
            pass

        truncated = total > self.PREVIEW_ROWS
        more_note = (f'<div class="opx-note">Показаны первые {self.PREVIEW_ROWS} '
                      f'из {total} строк</div>') if truncated else ""

        html = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<style>'
            'body{margin:0;padding:14px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#fff;}'
            '.opx-note{font-size:11.5px;color:rgba(0,0,0,0.55);margin-bottom:10px;font-style:italic;}'
            'table{border-collapse:collapse;width:100%;font-size:11.5px;font-family:"SF Mono",Menlo,monospace;}'
            'th{background:#2D7A3E;color:#fff;padding:6px 8px;text-align:left;position:sticky;top:0;font-weight:600;letter-spacing:0.02em;}'
            'td{padding:4px 8px;border-bottom:1px solid rgba(0,0,0,0.05);color:#1a1a1a;white-space:nowrap;}'
            'tr:nth-child(even) td{background:rgba(45,122,62,0.03);}'
            '</style></head><body>'
            + more_note
            + '<table><thead>' + (rows_html[0] if rows_html else "") + '</thead>'
            + '<tbody>' + ''.join(rows_html[1:]) + '</tbody>'
            + '</table></body></html>'
        )
        return HttpResponse(html, content_type="text/html; charset=utf-8")


class PricelistGenerateOutputView(APIView):
    """POST /api/assistant/upload-pricelist/<id>/generate-output/

    Генерирует XLSX в формате маркетплейса (как у claude.ai).
    Body: {mapping, transform_rules, constants, ai_estimates_override}
    Returns: {filename, size, download_url}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, import_id):
        from django.core.files.base import ContentFile

        from marketplace.models import PricelistImport
        try:
            imp = PricelistImport.objects.get(id=import_id, seller=request.user)
        except PricelistImport.DoesNotExist:
            return Response({"error": "import not found"}, status=404)

        mapping = request.data.get("mapping") or imp.suggested_mapping or {}
        transform_rules = request.data.get("transform_rules") or {}
        constants = request.data.get("constants") or {}
        ai_overrides = request.data.get("ai_estimates_override") or {}

        # Применяем ai_overrides поверх ai_estimates
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
                cur["confidence"] = 1.0
                current[oem] = cur
            imp.ai_estimates = current

        try:
            xlsx_bytes = _generate_marketplace_xlsx(
                imp, mapping, transform_rules, constants,
            )
        except Exception as e:
            logger.exception("XLSX generation failed")
            return Response({"error": f"Не удалось сгенерировать XLSX: {e}"}, status=500)

        # Сохраняем в FileField
        base_name = (imp.filename or "pricelist").rsplit(".", 1)[0]
        out_name = f"{base_name}_marketplace.xlsx"
        imp.output_file.save(out_name, ContentFile(xlsx_bytes), save=True)

        return Response({
            "filename": out_name,
            "size": len(xlsx_bytes),
            "download_url": imp.output_file.url if imp.output_file else "",
            "rows_generated": xlsx_bytes.count(b"<row") if False else None,
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
            seller=request.user,
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
        from django.conf import settings
        from django.http import FileResponse, Http404
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
