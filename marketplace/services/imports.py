from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils.text import get_valid_filename, slugify

from marketplace.models import Brand, Category, Part


class UploadLimitError(Exception):
    def __init__(self, message: str, status_code: int = 413):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ImportResult:
    mode: str
    created: int
    updated: int
    skipped_no_price: int
    skipped_invalid: int
    errors: list[dict[str, Any]]


def _normalize_header(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _first_of(normalized_to_original: dict[str, str], *variants: str) -> str | None:
    for key in variants:
        if key in normalized_to_original:
            return normalized_to_original[key]
    return None


def _parse_price(raw: str) -> Decimal | None:
    cleaned = (raw or "").strip()
    cleaned = cleaned.replace("€", "").replace("$", "").replace("₽", "").strip()
    cleaned = cleaned.replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        value = Decimal(cleaned).quantize(Decimal("0.01"))
        return value if value > 0 else None
    except Exception:
        return None


def process_seller_csv_upload(
    *,
    seller,
    upload,
    category_name: str,
    default_stock: int,
    import_mode: str,
    default_image_url: str = "/static/marketplace/epiroc-logo.webp",
) -> ImportResult:
    filename = get_valid_filename(upload.name.lower())
    if not filename.endswith(".csv"):
        raise ValueError("Поддерживается только CSV (UTF-8).")

    file_size = int(getattr(upload, "size", 0) or 0)
    if file_size > int(settings.MAX_IMPORT_FILE_BYTES):
        raise UploadLimitError(
            f"Файл слишком большой. Максимум: {settings.MAX_IMPORT_FILE_BYTES} байт.",
            status_code=413,
        )

    raw = upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV должен быть в кодировке UTF-8.") from exc

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV не содержит заголовок.")

    normalized_to_original = {_normalize_header(h): h for h in reader.fieldnames if h}
    part_col = _first_of(normalized_to_original, "partnumber", "partno", "partnum", "part#", "number", "sku", "article", "artikul")
    desc_col = _first_of(normalized_to_original, "description", "descriptionenglisch", "name", "title", "productname")
    price_col = _first_of(normalized_to_original, "unitprice", "price", "priceusd", "unitcost", "cost")
    currency_col = _first_of(normalized_to_original, "currency", "curr")
    stock_col = _first_of(normalized_to_original, "stock", "stockqty", "qty", "quantity")
    oem_col = _first_of(normalized_to_original, "oem", "oemnumber", "oemno")

    if not (part_col and desc_col and price_col):
        raise ValueError(
            "CSV должен содержать колонки для Part Number, Description и Unitprice "
            "(поддерживаются варианты: Partnumber/Part Number, Description, Unitprice/Price)."
        )

    category_slug = slugify(category_name)[:140] or "import-category"
    category, _ = Category.objects.get_or_create(slug=category_slug, defaults={"name": category_name})
    if category.name != category_name:
        category.name = category_name
        category.save(update_fields=["name"])
    brand = Brand.objects.filter(name__iexact=category_name).first()

    created = 0
    updated = 0
    skipped_no_price = 0
    skipped_invalid = 0
    batch_size = 1000
    errors: list[dict[str, Any]] = []
    row_count = 0

    existing_parts = Part.objects.filter(seller=seller).only("id", "oem_number")
    existing_by_oem = {p.oem_number: p.id for p in existing_parts}
    pending_new_by_oem: dict[str, Part] = {}
    to_create: list[Part] = []
    to_update: list[Part] = []

    def flush_updates():
        nonlocal updated
        if not to_update:
            return
        with transaction.atomic():
            Part.objects.bulk_update(
                to_update,
                [
                    "title",
                    "description",
                    "price",
                    "stock_quantity",
                    "condition",
                    "category",
                    "brand",
                    "image_url",
                    "currency",
                    "is_active",
                ],
                batch_size=batch_size,
            )
        updated += len(to_update)
        to_update.clear()

    def flush_creates():
        nonlocal created
        if not to_create:
            return
        with transaction.atomic():
            Part.objects.bulk_create(to_create, batch_size=batch_size)
        created += len(to_create)
        to_create.clear()
        pending_new_by_oem.clear()

    for row_num, row in enumerate(reader, start=2):
        row_count += 1
        if row_count > int(settings.MAX_IMPORT_ROWS):
            raise UploadLimitError(
                f"Превышен лимит строк. Максимум: {settings.MAX_IMPORT_ROWS}.",
                status_code=413,
            )
        part_number = (row.get(part_col) or "").strip()
        description = (row.get(desc_col) or "").strip()
        price_raw = (row.get(price_col) or "").strip()

        if not part_number:
            skipped_invalid += 1
            if len(errors) < 100:
                errors.append({"row": row_num, "reason": "Пустой Part Number"})
            continue

        price = _parse_price(price_raw)
        if price is None:
            skipped_no_price += 1
            if len(errors) < 100:
                errors.append({"row": row_num, "reason": f"Некорректная цена: {price_raw or 'empty'}"})
            continue
        title = description if description else f"Part {part_number}"

        stock_value = default_stock
        if stock_col:
            raw_stock = (row.get(stock_col) or "").strip()
            if raw_stock:
                try:
                    stock_value = max(0, int(float(raw_stock.replace(",", "."))))
                except Exception:
                    stock_value = default_stock

        currency_value = "USD"
        if currency_col:
            raw_currency = (row.get(currency_col) or "").strip().upper()
            if raw_currency in {"USD", "EUR", "RUB", "CNY"}:
                currency_value = raw_currency

        oem_number_value = (row.get(oem_col) or "").strip() if oem_col else part_number
        if not oem_number_value:
            oem_number_value = part_number

        if import_mode == "preview":
            if existing_by_oem.get(part_number):
                updated += 1
            else:
                created += 1
            continue

        existing_id = existing_by_oem.get(part_number)
        if existing_id:
            to_update.append(
                Part(
                    id=existing_id,
                    seller=seller,
                    title=title[:255],
                    oem_number=oem_number_value,
                    description=description,
                    price=price,
                    stock_quantity=stock_value,
                    condition="oem",
                    image_url=default_image_url,
                    category=category,
                    brand=brand,
                    currency=currency_value,
                    is_active=True,
                )
            )
            if len(to_update) >= batch_size:
                flush_updates()
            continue

        if part_number in pending_new_by_oem:
            obj = pending_new_by_oem[part_number]
            obj.title = title[:255]
            obj.description = description
            obj.price = price
            continue

        base = slugify(f"{part_number}-{title}")[:220] or "part"
        new_part = Part(
            seller=seller,
            title=title[:255],
            slug=f"{base}-{uuid4().hex[:8]}",
            oem_number=oem_number_value,
            description=description,
            price=price,
            stock_quantity=stock_value,
            condition="oem",
            image_url=default_image_url,
            category=category,
            brand=brand,
            currency=currency_value,
            is_active=True,
        )
        to_create.append(new_part)
        pending_new_by_oem[part_number] = new_part
        if len(to_create) >= batch_size:
            flush_creates()

    if import_mode != "preview":
        flush_updates()
        flush_creates()

    return ImportResult(
        mode=import_mode,
        created=created,
        updated=updated,
        skipped_no_price=skipped_no_price,
        skipped_invalid=skipped_invalid,
        errors=errors,
    )
