"""Seed demo catalog — части для демо-покупателя.

OEM: те, что demo_buyer реально ищет в чате.
Запуск (прод):
    ALLOW_SEED_IN_PROD=1 python manage.py seed_demo_catalog
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from assistant.management._seed_guard import ensure_dev_only
from marketplace.models import Brand, Category, Part, SellerWarehouse

User = get_user_model()

ITEMS = [
    # (oem_number, title, brand_name, price_usd, qty, weight_kg)
    ("421-43-25411", "Fuel Filter Assy Komatsu WA380/WA400",   "Komatsu",      Decimal("64.00"),   80,   Decimal("0.450")),
    ("11Q6-10101",   "Hydraulic Pump Assy Hyundai R210",        "Hyundai",      Decimal("1490.00"), 4,    Decimal("14.200")),
    ("4110000556",   "Oil Filter Element XCMG/LiuGong",         "Komatsu",      Decimal("18.50"),   300,  Decimal("0.290")),
    ("6I-2502",      "Fuel Filter Element CAT 320C/325C",       "Caterpillar",  Decimal("44.00"),   120,  Decimal("0.400")),
    ("2W-1223",      "O-Ring Seal CAT 3306/3406 (bulk pack)",   "Caterpillar",  Decimal("2.80"),    8000, Decimal("0.005")),
]

# Вторые цены (другой «поставщик» — та же учётка, склад B)
ITEMS_B = [
    ("421-43-25411", "Fuel Filter 421-43-25411 (alt supply)",   "Komatsu",      Decimal("71.00"),   30,   Decimal("0.450")),
    ("6I-2502",      "Fuel Filter 6I-2502 Aftermarket",        "Caterpillar",  Decimal("38.50"),   200,  Decimal("0.390")),
    ("2W-1223",      "O-Ring 2W-1223 (bag 100 pcs)",           "Caterpillar",  Decimal("3.20"),    5000, Decimal("0.006")),
]


class Command(BaseCommand):
    help = "Засеять демо-каталог (OEM для demo_buyer)."

    def handle(self, *args, **opts):
        ensure_dev_only(self)
        seller = User.objects.filter(username="demo_seller").first()
        if not seller:
            self.stderr.write("demo_seller не найден — запусти seed_chat_demo сначала")
            return

        cat = Category.objects.first()
        if not cat:
            self.stderr.write("Нет ни одной Category в БД")
            return

        wh_a, _ = SellerWarehouse.objects.get_or_create(
            seller=seller, name="Склад А — Шэньчжэнь",
            defaults={"kind": "pricelist", "address": "Shenzhen, China",
                      "sea_port": "Shenzhen"},
        )
        wh_b, _ = SellerWarehouse.objects.get_or_create(
            seller=seller, name="Склад Б — Гуанчжоу",
            defaults={"kind": "pricelist", "address": "Guangzhou, China",
                      "sea_port": "Guangzhou"},
        )

        created = 0
        for rows, wh in [(ITEMS, wh_a), (ITEMS_B, wh_b)]:
            for oem, title, brand_name, price, qty, weight in rows:
                brand, _ = Brand.objects.get_or_create(
                    name=brand_name,
                    defaults={"slug": slugify(brand_name)},
                )
                slug_base = f"demo-{slugify(oem)}-{wh.id}"
                part, made = Part.objects.get_or_create(
                    slug=slug_base,
                    defaults=dict(
                        title=title,
                        oem_number=oem,
                        price=price,
                        stock_quantity=qty,
                        condition="oem",
                        availability="in_stock",
                        availability_status="active",
                        currency="USD",
                        incoterm="FOB",
                        seller=seller,
                        brand=brand,
                        category=cat,
                        warehouse=wh,
                        country_of_origin="CN",
                        manufacturer="Demo Factory Co., Ltd.",
                        gross_weight_kg=weight,
                        length_cm=Decimal("12"),
                        width_cm=Decimal("8"),
                        height_cm=Decimal("6"),
                        is_active=True,
                        price_fob_sea=price * Decimal("0.88"),
                        price_fob_air=price * Decimal("1.35"),
                    ),
                )
                status = "СОЗДАН" if made else "уже есть"
                if made:
                    created += 1
                self.stdout.write(f"  {status}: {oem} ${price} qty={qty} [{wh.name}]")

        self.stdout.write(self.style.SUCCESS(f"\nГотово: {created} позиций создано."))
