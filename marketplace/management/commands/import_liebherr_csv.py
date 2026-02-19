import csv
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from marketplace.models import Brand, Category, Part


def _normalize_header(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


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


class Command(BaseCommand):
    help = "Safely reload Liebherr parts from CSV: remove existing rows for same Partnumber, then import fresh."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True, help="Absolute path to Liebherr CSV")
        parser.add_argument(
            "--brand",
            default="Liebherr",
            help="Brand name to assign (default: Liebherr)",
        )
        parser.add_argument(
            "--category",
            default="Рабочее оборудование",
            help="Category name to assign for imported rows",
        )
        parser.add_argument(
            "--seller",
            default="",
            help="Username or email of seller owner (optional)",
        )
        parser.add_argument(
            "--default-stock",
            type=int,
            default=20,
            help="Default stock quantity",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Batch size for create/delete",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"]).expanduser()
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        seller = None
        seller_ref = (options["seller"] or "").strip()
        if seller_ref:
            seller = User.objects.filter(username=seller_ref).first() or User.objects.filter(email__iexact=seller_ref).first()
            if not seller:
                raise CommandError(f"Seller not found: {seller_ref}")
        else:
            seller = User.objects.filter(is_superuser=True).first() or User.objects.order_by("id").first()

        if not seller:
            raise CommandError("No users found. Create at least one user first.")

        brand_name = options["brand"].strip()
        brand_slug = slugify(brand_name)[:180] or "liebherr"
        brand, _ = Brand.objects.get_or_create(
            slug=brand_slug,
            defaults={"name": brand_name, "region": "global"},
        )
        if brand.name != brand_name:
            brand.name = brand_name
            brand.save(update_fields=["name"])

        category_name = options["category"].strip()
        category = Category.objects.filter(name=category_name).first()
        if not category:
            category_slug = slugify(category_name)[:140] or f"category-{uuid4().hex[:8]}"
            category, _ = Category.objects.get_or_create(slug=category_slug, defaults={"name": category_name})

        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
                delimiter = dialect.delimiter
            except Exception:
                delimiter = ","
            reader = csv.DictReader(f, delimiter=delimiter)
            if not reader.fieldnames:
                raise CommandError("CSV has no header row.")

            normalized = {_normalize_header(h): h for h in reader.fieldnames if h}
            part_col = normalized.get("partnumber") or normalized.get("partno")
            desc_col = normalized.get("description") or normalized.get("descriptionenglisch")
            desc_en_col = normalized.get("descriptionenglisch")
            price_col = normalized.get("unitprice") or normalized.get("price")
            if not (part_col and desc_col and price_col):
                raise CommandError(
                    "Required headers missing. Need Partnumber/Part Number, Description, Unitprice/Price."
                )

            data = {}
            skipped_no_price = 0
            for row in reader:
                part_number = (row.get(part_col) or "").strip()
                if not part_number:
                    continue
                title = (row.get(desc_en_col) or "").strip() if desc_en_col else ""
                if not title:
                    title = (row.get(desc_col) or "").strip() or f"Part {part_number}"
                description = (row.get(desc_col) or "").strip()
                price = _parse_price(row.get(price_col) or "")
                if price is None:
                    skipped_no_price += 1
                    continue
                data[part_number] = {
                    "title": title[:255],
                    "description": description,
                    "price": price,
                }

        part_numbers = set(data.keys())
        self.stdout.write(f"Parsed CSV rows: {len(data)} (skipped without valid price: {skipped_no_price})")

        batch_size = options["batch_size"]
        existing_by_oem = {}
        for p in Part.objects.only("id", "oem_number", "slug").iterator(chunk_size=5000):
            if p.oem_number in part_numbers:
                existing_by_oem[p.oem_number] = p

        # Update existing rows in place (safe for rows linked to orders), create only missing rows.
        to_update = []
        to_create = []
        updated = 0
        created = 0
        default_stock = max(0, int(options["default_stock"]))
        for part_number, payload in data.items():
            existing = existing_by_oem.get(part_number)
            if existing:
                existing.title = payload["title"]
                existing.description = payload["description"]
                existing.price = payload["price"]
                existing.stock_quantity = default_stock
                existing.condition = "oem"
                existing.image_url = "/static/marketplace/liebherr-logo.svg"
                existing.category = category
                existing.brand = brand
                existing.seller = seller
                existing.is_active = True
                to_update.append(existing)
                if len(to_update) >= batch_size:
                    with transaction.atomic():
                        Part.objects.bulk_update(
                            to_update,
                            [
                                "title",
                                "description",
                                "price",
                                "stock_quantity",
                                "condition",
                                "image_url",
                                "category",
                                "brand",
                                "seller",
                                "is_active",
                            ],
                            batch_size=batch_size,
                        )
                    updated += len(to_update)
                    to_update.clear()
                continue

            base = slugify(f"{part_number}-{payload['title']}")[:220] or "part"
            to_create.append(
                Part(
                    seller=seller,
                    title=payload["title"],
                    slug=f"{base}-{uuid4().hex[:8]}",
                    oem_number=part_number,
                    description=payload["description"],
                    price=payload["price"],
                    stock_quantity=default_stock,
                    condition="oem",
                    image_url="/static/marketplace/liebherr-logo.svg",
                    category=category,
                    brand=brand,
                    is_active=True,
                )
            )
            if len(to_create) >= batch_size:
                with transaction.atomic():
                    Part.objects.bulk_create(to_create, batch_size=batch_size)
                created += len(to_create)
                to_create.clear()

        if to_update:
            with transaction.atomic():
                Part.objects.bulk_update(
                    to_update,
                    [
                        "title",
                        "description",
                        "price",
                        "stock_quantity",
                        "condition",
                        "image_url",
                        "category",
                        "brand",
                        "seller",
                        "is_active",
                    ],
                    batch_size=batch_size,
                )
            updated += len(to_update)

        if to_create:
            with transaction.atomic():
                Part.objects.bulk_create(to_create, batch_size=batch_size)
            created += len(to_create)

        self.stdout.write(self.style.SUCCESS(f"Import complete. Created: {created}, Updated: {updated}"))
