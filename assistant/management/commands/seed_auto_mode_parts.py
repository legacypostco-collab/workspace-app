"""Seed test parts that satisfy AUTO-mode RFQ classification.

ТЗ §4.1 (auto-режим) требует, чтобы для каждой позиции:
  • было ≥3 актуальных предложений (trusted + sandbox)
  • был ≥1 «надёжный» (trusted) поставщик
  • cheapest-исполнитель тоже был trusted
  • buyer был верифицирован KYB

Эта команда берёт существующих trusted-продавцов и создаёт по
одной копии каждой test-OEM с разными ценами, чтобы каждая такая
позиция автоматически проходила в AUTO mode.

Запуск:
    python manage.py seed_auto_mode_parts
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from marketplace.models import Brand, Category, Part, UserProfile

User = get_user_model()


# Test OEMs: каждый с несколькими ценовыми точками от разных продавцов.
# Cheapest должен оказаться у trusted (порядок trusted_users в команде:
# [demo_seller, seller_1, seller_5, seller_6] — все trusted в БД).
TEST_OEMS = [
    # (oem_number, title, brand_name, category_slug, base_price_usd)
    ("AUTO-TEST-9F77-CRAWLER-PIN", "Палец гусеничной цепи D85 (тест AUTO)",  "Komatsu",  "running-gear",  85),
    ("AUTO-TEST-5K22-FUEL-FILTER", "Топливный фильтр CAT 320 (тест AUTO)",   "Caterpillar", "filters",    42),
    ("AUTO-TEST-3C18-HYD-PUMP",    "Гидронасос HPV0102 (тест AUTO)",         "Komatsu",  "hydraulics",    1200),
    ("AUTO-TEST-7M44-INJECTOR",    "Форсунка common-rail (тест AUTO)",        "Caterpillar", "engine",     320),
    ("AUTO-TEST-2B61-BUSHING",     "Втулка ковша 60×80×95 (тест AUTO)",       "Liebherr", "running-gear",  68),
]

# Список trusted-юзеров — берём существующих в БД
TRUSTED_USERNAMES = ["demo_seller", "seller_1", "seller_5", "seller_6"]


class Command(BaseCommand):
    help = "Засеять тестовые OEM-номера, проходящие AUTO-mode классификатор."

    def handle(self, *args, **opts):
        # 1. Найти trusted-продавцов
        trusted_users = []
        for uname in TRUSTED_USERNAMES:
            try:
                u = User.objects.get(username=uname)
            except User.DoesNotExist:
                self.stderr.write(f"⚠ нет пользователя {uname}, пропускаю")
                continue
            prof = UserProfile.objects.filter(user=u).first()
            if not prof or prof.supplier_status != "trusted":
                self.stderr.write(f"⚠ {uname}: profile.supplier_status={prof and prof.supplier_status}, не trusted")
                continue
            trusted_users.append(u)

        if len(trusted_users) < 3:
            self.stderr.write(self.style.ERROR(
                f"Нужно ≥3 trusted-продавцов; нашлось {len(trusted_users)}. Сначала посей trusted-юзеров."
            ))
            return

        self.stdout.write(f"Trusted-продавцы: {[u.username for u in trusted_users]}")

        # 2. Для каждого OEM — создать копию у каждого trusted с возрастающей ценой.
        # cheapest достанется первому = trusted (это и matched_part).
        created_total = 0
        with transaction.atomic():
            for oem, title, brand_name, cat_slug, base_price in TEST_OEMS:
                # Brand
                brand, _ = Brand.objects.get_or_create(
                    name=brand_name,
                    defaults={"slug": slugify(brand_name)},
                )
                # Category — обязателен
                cat = Category.objects.filter(slug=cat_slug).first()
                if cat is None:
                    cat = Category.objects.first()
                    if cat is None:
                        self.stderr.write("⚠ нет категорий в БД, создаю «Запчасти»")
                        cat = Category.objects.create(name="Запчасти", slug="parts")

                for i, seller in enumerate(trusted_users):
                    price = Decimal(base_price) + Decimal(i * 7)  # возрастает: cheapest у первого trusted
                    obj, created = Part.objects.update_or_create(
                        oem_number=oem, seller=seller,
                        defaults={
                            "title": f"{title} · {seller.username}",
                            "slug": slugify(f"{oem}-{seller.username}")[:280],
                            "description": f"Тестовая позиция AUTO-mode — поставщик {seller.username}",
                            "price": price,
                            "stock_quantity": 25,
                            "condition": "oem",
                            "brand": brand,
                            "category": cat,
                            "availability": "in_stock",
                            "availability_status": "active",
                            "currency": "USD",
                            "incoterm": "FOB",
                            "moq": 1,
                            "production_lead_days": 7,
                            "prep_to_ship_days": 2,
                            "shipping_lead_days": 14,
                            "gross_weight_kg": Decimal("3.000"),
                            "country_of_origin": "China",
                            "is_active": True,
                            "mapping_status": "confirmed",
                        },
                    )
                    if created:
                        created_total += 1
                        self.stdout.write(f"  + {oem} @ {seller.username}: ${price}")
                    else:
                        self.stdout.write(f"  ↻ {oem} @ {seller.username}: ${price} (updated)")

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово. Создано/обновлено {len(TEST_OEMS)*len(trusted_users)} parts. "
            f"OEMs пригодны для AUTO-mode тестов."
        ))
        self.stdout.write("\nOEM-номера для теста:")
        for oem, title, *_ in TEST_OEMS:
            self.stdout.write(f"  • {oem} — {title}")
