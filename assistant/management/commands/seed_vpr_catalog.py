"""Импорт каталога ВПР — 9 поставщиков, ~2.5M позиций.

Создаёт:
- аккаунт продавца vpr / ВПР (если нет)
- 9 складов (по одному на источник)
- Part'ы из каждого файла (bulk_create, idempotent по slug)

Запуск:
    python manage.py seed_vpr_catalog
    python manage.py seed_vpr_catalog --only bartsparts fridayparts
    python manage.py seed_vpr_catalog --dry-run
    python manage.py seed_vpr_catalog --limit 500   # первые N строк каждого файла
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from assistant.management._seed_guard import (
    add_seed_password_argument,
    ensure_dev_only,
    require_seed_password,
)
from marketplace.models import Brand, Category, Part, SellerWarehouse, UserProfile

User = get_user_model()

DOWNLOADS = Path("/Users/kosta/Downloads")

# ── Описание источников ───────────────────────────────────────────────────────
SOURCES = {
    "bartsparts": {
        "file": "bartsparts_result-processed.jsonl",
        "fmt": "jsonl",
        "supplier": "Bart's Parts",
        "country": "NL",
        "currency_default": "EUR",
    },
    "fridayparts": {
        "file": "fridayparts.jsonl",
        "fmt": "jsonl",
        "supplier": "Friday Parts",
        "country": "US",
        "currency_default": "USD",
    },
    "ghinassi": {
        "file": "ghinassi_products_new_16-01-26.jsonl",
        "fmt": "jsonl",
        "supplier": "Ghinassi",
        "country": "IT",
        "currency_default": "EUR",
    },
    "greenway": {
        "file": "greenway_parts_scraper.json",
        "fmt": "nested_json",
        "supplier": "Greenway Parts",
        "country": "US",
        "currency_default": "USD",
    },
    "advancedtruckparts": {
        "file": "products_advancedtruckparts.json",
        "fmt": "nested_json",
        "supplier": "Advanced Truck Parts",
        "country": "US",
        "currency_default": "USD",
    },
    "pickettequip": {
        "file": "products_pickettequip (2).json",
        "fmt": "nested_json",
        "supplier": "Pickett Equipment",
        "country": "US",
        "currency_default": "USD",
    },
    "usrparts": {
        "file": "products_shop_usrparts.json",
        "fmt": "nested_json",
        "supplier": "USR Parts",
        "country": "US",
        "currency_default": "USD",
    },
    "rdoequipment": {
        "file": "rdoequipment.jsonl",
        "fmt": "jsonl",
        "supplier": "RDO Equipment",
        "country": "US",
        "currency_default": "USD",
    },
    "tractorparts": {
        "file": "tractorparts_store.json",
        "fmt": "nested_json",
        "supplier": "Tractor Parts Store",
        "country": "US",
        "currency_default": "USD",
    },
}

BATCH = 2000

# OEM: есть и буква и цифра, ≥5 символов; или ≥8 чистых цифр (каталожный номер)
_OEM_RE = re.compile(r'(?<!\w)([A-Z0-9][A-Z0-9/\-]{4,})(?!\w)')


def _extract_oem(text: str) -> str | None:
    """Вычленить первый OEM-парт-номер из строки названия."""
    for m in _OEM_RE.finditer(text.upper()):
        v = m.group(0)
        has_let = any(c.isalpha() for c in v)
        has_dig = any(c.isdigit() for c in v)
        if has_let and has_dig and len(v) >= 5:
            return v
        if has_dig and not has_let and len(v) >= 8:
            return v
    return None


def _normalize_greenway_sku(sku: str) -> str:
    """r561692 / sma-120814316 / sma-cr9934 → R561692 / 120814316 / CR9934."""
    s = sku.strip().lower()
    for pfx in ("sma-cr-", "sma-cr", "sma-"):
        if s.startswith(pfx):
            s = s[len(pfx):]
            break
    return s.upper()


# ── Парсеры ───────────────────────────────────────────────────────────────────

def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def _iter_nested_json(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        outer = json.load(fh)
    for row in outer:
        blob = row.get("jsonl", "")
        for line in blob.split("\n"):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def _iter(key: str, src: dict):
    path = DOWNLOADS / src["file"]
    if src["fmt"] == "jsonl":
        return _iter_jsonl(path)
    return _iter_nested_json(path)


# ── Нормализатор строки → Part-поля ───────────────────────────────────────────

def _stock_qty(val) -> int:
    """'2 parts in 1 Warehouse' → 2; 'IN_STOCK' → 1; числа → число."""
    if val is None:
        return 1
    s = str(val)
    if s.upper() in ("IN_STOCK", "INSTOCK", "YES", "TRUE", "1"):
        return 1
    if s.upper() in ("OUT_OF_STOCK", "NO", "FALSE", "0"):
        return 0
    m = re.match(r"(\d+)", s)
    return int(m.group(1)) if m else 1


def _avail(qty: int) -> str:
    return "in_stock" if qty > 0 else "out_of_stock"


def _price(val) -> Decimal | None:
    try:
        p = Decimal(str(val)).quantize(Decimal("0.01"))
        return p if p > 0 else None
    except (InvalidOperation, TypeError):
        return None


def _condition(val) -> str:
    if not val:
        return "oem"
    v = str(val).lower()
    if "aftermarket" in v or "after" in v:
        return "aftermarket"
    if "reman" in v:
        return "reman"
    return "oem"


def _normalize(key: str, row: dict, src: dict) -> dict | None:
    """Приводим поля любого источника к единой схеме."""
    name = (
        row.get("name") or row.get("product_name") or row.get("title") or ""
    ).strip()

    # OEM — источник-зависимая логика
    if key == "greenway":
        raw = (row.get("sku") or "").strip()
        oem = _normalize_greenway_sku(raw) if raw else ""
    elif key in ("tractorparts", "fridayparts"):
        oem = _extract_oem(name) or ""
    else:
        oem = (
            row.get("sku") or row.get("oem") or row.get("part_number") or ""
        ).strip()

    if not oem:
        return None
    if not name:
        name = oem

    price_raw = row.get("price") or row.get("price_value")
    price = _price(price_raw)
    if price is None:
        return None

    currency = (
        row.get("currency") or row.get("price_currency") or src["currency_default"]
    ).upper()[:3]

    brand_name = (
        row.get("brand") or row.get("brand_short") or row.get("producer") or ""
    ).strip() or None

    stock_raw = row.get("stock") or row.get("availability_in_stock")
    qty = _stock_qty(stock_raw)

    cond_raw = row.get("condition") or row.get("condition_short")
    condition = _condition(cond_raw)

    return {
        "oem": oem,
        "name": name[:500],
        "price": price,
        "currency": currency,
        "brand": brand_name,
        "qty": qty,
        "condition": condition,
    }


# ── Команда ───────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = "Импорт каталога ВПР (9 поставщиков)"

    def add_arguments(self, parser):
        add_seed_password_argument(parser)
        parser.add_argument("--only", nargs="*", choices=list(SOURCES),
                            help="Только эти источники")
        parser.add_argument("--limit", type=int, default=0,
                            help="Лимит строк на источник (0 = всё)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Показать статистику без записи в БД")

    def handle(self, *args, **options):
        ensure_dev_only(self)
        password = "" if options["dry_run"] else require_seed_password(options)
        dry = options["dry_run"]
        limit = options["limit"]
        only = set(options["only"] or SOURCES.keys())

        # ── Создать/получить аккаунт ВПР ──────────────────────────────────────
        seller = self._get_or_create_seller(password)
        self.stdout.write(self.style.SUCCESS(f"Seller: {seller.username} (id={seller.pk})"))

        # ── Дефолтная категория ────────────────────────────────────────────────
        cat = Category.objects.first()
        if not cat:
            self.stderr.write("Нет категорий в БД — сначала запусти seed_full_demo")
            return

        total_created = 0

        for key, src in SOURCES.items():
            if key not in only:
                continue
            path = DOWNLOADS / src["file"]
            if not path.exists():
                self.stdout.write(self.style.WARNING(f"  {key}: файл не найден, пропускаем"))
                continue

            # ── Склад ─────────────────────────────────────────────────────────
            wh = self._get_or_create_wh(seller, src)
            self.stdout.write(f"\n→ {src['supplier']} (wh id={wh.pk})")

            if dry:
                n = sum(1 for _ in _iter(key, src))
                self.stdout.write(f"   DRY-RUN: {n} строк в файле")
                continue

            created = self._import_source(key, src, wh, seller, cat, limit)
            self.stdout.write(self.style.SUCCESS(f"   создано: {created}"))
            total_created += created

        if not dry:
            self.stdout.write(self.style.SUCCESS(f"\n✓ Итого создано: {total_created}"))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_or_create_seller(self, password):
        user, created = User.objects.get_or_create(
            username="vpr",
            defaults={
                "first_name": "ВПР",
                "last_name": "Каталог",
                "email": "vpr@example.com",
            },
        )
        if created:
            user.set_password(password)
            user.save()
            self.stdout.write("  Создан пользователь vpr")
        profile, _ = UserProfile.objects.get_or_create(
            user=user,
            defaults={
                "role": "seller",
                "company_name": "ВПР",
                "supplier_status": "trusted",
            },
        )
        if profile.role != "seller":
            profile.role = "seller"
            profile.save(update_fields=["role"])
        return user

    def _get_brand(self, name: str) -> Brand:
        """get_or_create бренда с обработкой slug-коллизий."""
        try:
            b, _ = Brand.objects.get_or_create(
                name=name,
                defaults={"slug": slugify(name)[:110]},
            )
            return b
        except Exception:
            # slug уже занят другим брендом — ищем по name (iexact) или slug
            existing = Brand.objects.filter(name__iexact=name).first()
            if existing:
                return existing
            base = slugify(name)[:100]
            for i in range(2, 100):
                candidate = f"{base}-{i}"[:110]
                if not Brand.objects.filter(slug=candidate).exists():
                    return Brand.objects.create(name=name, slug=candidate)
            return Brand.objects.create(name=name, slug=f"{base}-{name.__hash__() % 99999}"[:110])

    def _get_or_create_wh(self, seller, src: dict) -> SellerWarehouse:
        name = f"{src['supplier']} · склад"
        wh, _ = SellerWarehouse.objects.get_or_create(
            seller=seller,
            supplier_name=src["supplier"],
            defaults={
                "name": name,
                "kind": "pricelist",
                "country_code": src.get("country", ""),
                "currency": src.get("currency_default", "USD"),
                "is_full_catalog": True,
            },
        )
        return wh

    def _import_source(self, key: str, src: dict, wh: SellerWarehouse,
                       seller, cat, limit: int) -> int:
        # Загрузить существующие slugs этого склада, чтобы пропускать дубли
        existing = set(
            Part.objects.filter(warehouse=wh)
            .values_list("slug", flat=True)
        )

        brand_cache: dict[str, Brand] = {}
        batch: list[Part] = []
        created_total = 0
        processed = 0

        for row in _iter(key, src):
            if limit and processed >= limit:
                break
            processed += 1
            if processed % 50_000 == 0:
                self.stdout.write(f"   ... {processed} строк обработано")

            norm = _normalize(key, row, src)
            if not norm:
                continue

            oem = norm["oem"]
            slug = slugify(f"vpr-{key}-{oem}")[:270]
            if slug in existing:
                continue

            # Бренд
            brand_name = norm["brand"]
            brand = None
            if brand_name:
                bn = brand_name[:100]
                if bn not in brand_cache:
                    brand_cache[bn] = self._get_brand(bn)
                brand = brand_cache[bn]

            existing.add(slug)
            batch.append(Part(
                slug=slug,
                title=norm["name"],
                oem_number=oem[:100],
                price=norm["price"],
                currency=norm["currency"],
                stock_quantity=norm["qty"],
                condition=norm["condition"],
                availability=_avail(norm["qty"]),
                availability_status="active",
                incoterm="EXW",
                seller=seller,
                warehouse=wh,
                category=cat,
                brand=brand,
                supplier_part_uid=f"{key}:{oem}"[:80],
                is_active=True,
            ))

            if len(batch) >= BATCH:
                Part.objects.bulk_create(batch, ignore_conflicts=True)
                created_total += len(batch)
                batch = []

        if batch:
            Part.objects.bulk_create(batch, ignore_conflicts=True)
            created_total += len(batch)

        return created_total
