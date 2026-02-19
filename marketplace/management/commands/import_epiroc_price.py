from __future__ import annotations

import re
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable
from uuid import uuid4
from xml.etree import ElementTree as ET

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from marketplace.models import Brand, Category, Part


NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref)
    if not match:
        return 0
    result = 0
    for char in match.group(1):
        result = result * 26 + (ord(char) - 64)
    return result - 1


def _normalize_price(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return Decimal("0.00")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _iter_rows_xlsx(path: Path) -> Iterable[list[str]]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))

        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        sheets = workbook.findall(".//a:sheets/a:sheet", NS)
        if not sheets:
            return

        rid = sheets[0].attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        if not rid or rid not in rel_map:
            return
        target = rel_map[rid]
        if not target.startswith("xl/"):
            target = f"xl/{target}"

        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            sst = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for si in sst.findall(".//a:si", NS):
                fragments = [node.text or "" for node in si.findall(".//a:t", NS)]
                shared.append("".join(fragments))

        worksheet = ET.fromstring(archive.read(target))
        rows = worksheet.findall(".//a:sheetData/a:row", NS)

        for row in rows:
            values_by_col: dict[int, str] = {}
            for cell in row.findall("a:c", NS):
                ref = cell.attrib.get("r", "A1")
                idx = _column_index(ref)
                value_tag = cell.find("a:v", NS)
                if value_tag is None:
                    continue

                raw = value_tag.text or ""
                if cell.attrib.get("t") == "s":
                    if raw.isdigit() and int(raw) < len(shared):
                        values_by_col[idx] = shared[int(raw)]
                    else:
                        values_by_col[idx] = raw
                else:
                    values_by_col[idx] = raw

            if not values_by_col:
                continue
            max_idx = max(values_by_col)
            yield [values_by_col.get(i, "") for i in range(max_idx + 1)]


class Command(BaseCommand):
    help = "Imports Epiroc price list from .xlsx into marketplace parts."

    def add_arguments(self, parser):
        parser.add_argument("xlsx_path", type=str)
        parser.add_argument("--category", default="Epiroc")
        parser.add_argument("--seller", default="", help="Username of seller owner (optional).")
        parser.add_argument("--stock", type=int, default=20, help="Default stock quantity for imported parts.")
        parser.add_argument("--batch-size", type=int, default=2000)

    def handle(self, *args, **options):
        source = Path(options["xlsx_path"]).expanduser()
        if not source.exists():
            raise CommandError(f"File not found: {source}")

        category, _ = Category.objects.get_or_create(
            slug=slugify(options["category"])[:140] or "epiroc",
            defaults={"name": options["category"]},
        )
        if category.name != options["category"]:
            category.name = options["category"]
            category.save(update_fields=["name"])

        seller = None
        if options["seller"]:
            seller = User.objects.filter(username=options["seller"]).first()
            if not seller:
                raise CommandError(f"Seller user not found: {options['seller']}")

        iterator = _iter_rows_xlsx(source)
        try:
            header = next(iterator)
        except StopIteration:
            raise CommandError("Empty workbook.")

        header_normalized = [h.strip().lower() for h in header]
        try:
            idx_part = header_normalized.index("part number")
            idx_desc = header_normalized.index("description")
            idx_price = header_normalized.index("unitprice")
        except ValueError as exc:
            try:
                idx_part = header_normalized.index("partnumber")
                idx_desc = header_normalized.index("description")
                idx_price = header_normalized.index("unitprice")
            except ValueError as exc2:
                raise CommandError(f"Unexpected header row: {header}") from exc2

        brand = Brand.objects.filter(name__iexact=options["category"]).first()

        existing_map = {p.oem_number: p for p in Part.objects.filter(category=category).only("id", "oem_number")}
        to_create: list[Part] = []
        created = 0
        updated = 0
        default_stock = max(0, int(options["stock"]))
        batch_size = max(1, int(options["batch_size"]))

        with transaction.atomic():
            for row_num, row in enumerate(iterator, start=2):
                if len(row) <= max(idx_part, idx_desc, idx_price):
                    continue

                part_number = (row[idx_part] or "").strip()
                description = (row[idx_desc] or "").strip()
                price_raw = (row[idx_price] or "").strip()

                if not part_number:
                    continue

                price = _normalize_price(price_raw)
                title = description if description else f"Part {part_number}"
                existing = existing_map.get(part_number)

                if existing:
                    Part.objects.filter(id=existing.id).update(
                        title=title[:255],
                        description=description,
                        price=price,
                        stock_quantity=default_stock,
                        category=category,
                        brand=brand,
                        seller=seller,
                        is_active=True,
                    )
                    updated += 1
                else:
                    slug_base = slugify(f"epiroc-{part_number}")[:230] or "epiroc-part"
                    to_create.append(
                        Part(
                            title=title[:255],
                            slug=f"{slug_base}-{uuid4().hex[:8]}",
                            oem_number=part_number,
                            description=description,
                            price=price,
                            stock_quantity=default_stock,
                            condition="oem",
                            image_url="",
                            category=category,
                            brand=brand,
                            seller=seller,
                            is_active=True,
                        )
                    )

                if len(to_create) >= batch_size:
                    Part.objects.bulk_create(to_create, batch_size=batch_size)
                    created += len(to_create)
                    to_create.clear()
                    self.stdout.write(f"Imported rows up to {row_num} | created={created} updated={updated}")

            if to_create:
                Part.objects.bulk_create(to_create, batch_size=batch_size)
                created += len(to_create)

        self.stdout.write(self.style.SUCCESS(f"Done. created={created}, updated={updated}, category={category.name}"))
