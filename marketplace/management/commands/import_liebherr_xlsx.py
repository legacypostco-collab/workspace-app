from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from marketplace.models import Brand, Category, Part

NS_MAIN = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
NS_REL = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_letters(cell_ref: str) -> str:
    match = re.match(r"([A-Z]+)", cell_ref or "")
    return match.group(1) if match else ""


def _decimal_or_none(value) -> Decimal | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace(",", "").replace(" ", "")
    try:
        dec = Decimal(raw).quantize(Decimal("0.01"))
        return dec if dec > 0 else None
    except Exception:
        return None


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for si in root.findall("x:si", NS_MAIN):
        values.append("".join((t.text or "") for t in si.findall(".//x:t", NS_MAIN)))
    return values


def _sheet_path(zf: zipfile.ZipFile, index: int = 0) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    sheets = workbook.find("x:sheets", NS_MAIN)
    if sheets is None or not list(sheets):
        raise CommandError("No sheets found in workbook.")
    target_sheet = list(sheets)[index]
    rid = target_sheet.attrib.get(f"{REL_NS}id")
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("r:Relationship", NS_REL)}
    if rid not in rel_map:
        raise CommandError("Cannot resolve worksheet path.")
    return "xl/" + rel_map[rid]


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value_tag = cell.find("x:v", NS_MAIN)
    if value_tag is None:
        return ""
    raw = (value_tag.text or "").strip()
    if cell_type == "s" and raw.isdigit():
        idx = int(raw)
        return shared[idx] if 0 <= idx < len(shared) else raw
    return raw


def _row_map(row: ET.Element, shared: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for cell in row.findall("x:c", NS_MAIN):
        col = _col_letters(cell.attrib.get("r", ""))
        if col:
            values[col] = _cell_text(cell, shared)
    return values


class Command(BaseCommand):
    help = "Import Liebherr XLSX price list with English titles."

    def add_arguments(self, parser):
        parser.add_argument("--xlsx", required=True, help="Absolute path to xlsx file")
        parser.add_argument("--seller", required=True, help="Seller username/email")
        parser.add_argument("--brand", default="Liebherr", help="Brand name")
        parser.add_argument("--category", default="Рабочее оборудование", help="Category")
        parser.add_argument("--default-stock", type=int, default=20, help="Default stock quantity")
        parser.add_argument("--batch-size", type=int, default=1000, help="Batch size")

    def handle(self, *args, **options):
        xlsx_path = Path(options["xlsx"]).expanduser()
        if not xlsx_path.exists():
            raise CommandError(f"File not found: {xlsx_path}")

        seller_ref = (options["seller"] or "").strip()
        seller = User.objects.filter(username=seller_ref).first() or User.objects.filter(email__iexact=seller_ref).first()
        if not seller:
            raise CommandError(f"Seller not found: {seller_ref}")

        brand_name = options["brand"].strip()
        brand_slug = slugify(brand_name)[:180] or "liebherr"
        brand, _ = Brand.objects.get_or_create(slug=brand_slug, defaults={"name": brand_name, "region": "global"})
        if brand.name != brand_name:
            brand.name = brand_name
            brand.save(update_fields=["name"])

        category_name = options["category"].strip()
        category = Category.objects.filter(name=category_name).first()
        if not category:
            category = Category.objects.create(name=category_name, slug=slugify(category_name)[:140] or f"cat-{uuid4().hex[:8]}")

        with zipfile.ZipFile(xlsx_path) as zf:
            shared = _load_shared_strings(zf)
            root = ET.fromstring(zf.read(_sheet_path(zf, index=0)))
            sheet_data = root.find("x:sheetData", NS_MAIN)
            if sheet_data is None:
                raise CommandError("No sheet data found.")
            rows = list(sheet_data.findall("x:row", NS_MAIN))

        if not rows:
            raise CommandError("No rows found.")

        existing_by_oem = {p.oem_number: p for p in Part.objects.filter(brand=brand).only("id", "oem_number").iterator(5000)}
        to_create: list[Part] = []
        to_update: list[Part] = []
        batch_size = max(1, int(options["batch_size"]))
        default_stock = max(0, int(options["default_stock"]))
        created = 0
        updated = 0
        skipped_no_price = 0
        skipped_no_part = 0

        def flush():
            nonlocal created, updated
            if to_update:
                with transaction.atomic():
                    Part.objects.bulk_update(
                        to_update,
                        ["title", "description", "price", "stock_quantity", "condition", "image_url", "category", "brand", "seller", "is_active"],
                        batch_size=batch_size,
                    )
                updated += len(to_update)
                to_update.clear()
            if to_create:
                with transaction.atomic():
                    Part.objects.bulk_create(to_create, batch_size=batch_size)
                created += len(to_create)
                to_create.clear()

        for row in rows[1:]:
            data = _row_map(row, shared)
            part_number = (data.get("A") or "").strip()
            if not part_number:
                skipped_no_part += 1
                continue
            german_title = (data.get("B") or "").strip()
            price = _decimal_or_none(data.get("C"))
            english_title = (data.get("G") or "").strip()
            if price is None:
                skipped_no_price += 1
                continue
            title = english_title or german_title or f"Part {part_number}"
            description = german_title

            existing = existing_by_oem.get(part_number)
            if existing:
                existing.title = title[:255]
                existing.description = description[:1000]
                existing.price = price
                existing.stock_quantity = default_stock
                existing.condition = "oem"
                existing.image_url = "/static/marketplace/liebherr-logo.svg"
                existing.category = category
                existing.brand = brand
                existing.seller = seller
                existing.is_active = True
                to_update.append(existing)
            else:
                base = slugify(f"{part_number}-{title}")[:220] or "part"
                to_create.append(
                    Part(
                        seller=seller,
                        title=title[:255],
                        slug=f"{base}-{uuid4().hex[:8]}",
                        oem_number=part_number,
                        description=description[:1000],
                        price=price,
                        stock_quantity=default_stock,
                        condition="oem",
                        image_url="/static/marketplace/liebherr-logo.svg",
                        category=category,
                        brand=brand,
                        is_active=True,
                    )
                )

            if len(to_update) >= batch_size or len(to_create) >= batch_size:
                flush()

        flush()
        self.stdout.write(
            self.style.SUCCESS(
                f"Liebherr import done. Created: {created}, Updated: {updated}, "
                f"Skipped no price: {skipped_no_price}, Skipped no part: {skipped_no_part}"
            )
        )
