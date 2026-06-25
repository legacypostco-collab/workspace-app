"""Seed базовых тарифов LogisticsTariff.

Покрывает основные маршруты CN/JP/KR/DE/TR → RU/KZ/BY/UZ/AM
для трёх режимов: sea, air, auto (авто = road).

Все записи idempotent (get_or_create).

Usage:
    python manage.py seed_logistics_tariffs          # dev
    ALLOW_SEED_IN_PROD=1 python manage.py seed_logistics_tariffs
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from assistant.management._seed_guard import ensure_dev_only
from marketplace.models import LogisticsTariff

# (origin, dest, mode, rate_per_kg, min_charge, transit_days)
TARIFFS = [
    # ── CN → основные направления ──────────────────────────────────────────
    ("CN", "RU", "sea",  Decimal("4.20"),  Decimal("420"),  42),
    ("CN", "RU", "air",  Decimal("9.50"),  Decimal("95"),   7),
    ("CN", "KZ", "sea",  Decimal("3.80"),  Decimal("380"),  35),
    ("CN", "KZ", "air",  Decimal("8.50"),  Decimal("85"),   5),
    ("CN", "BY", "sea",  Decimal("5.00"),  Decimal("500"),  45),
    ("CN", "BY", "air",  Decimal("10.00"), Decimal("100"),  8),
    ("CN", "UZ", "sea",  Decimal("4.50"),  Decimal("450"),  40),
    ("CN", "UZ", "air",  Decimal("9.00"),  Decimal("90"),   6),
    ("CN", "AM", "sea",  Decimal("5.20"),  Decimal("520"),  48),
    ("CN", "AM", "air",  Decimal("10.50"), Decimal("105"),  9),
    ("CN", "KG", "sea",  Decimal("4.80"),  Decimal("480"),  42),
    ("CN", "KG", "air",  Decimal("9.50"),  Decimal("95"),   6),
    ("CN", "GE", "sea",  Decimal("5.50"),  Decimal("550"),  50),
    ("CN", "GE", "air",  Decimal("11.00"), Decimal("110"),  9),
    ("CN", "AZ", "sea",  Decimal("5.20"),  Decimal("520"),  48),
    ("CN", "AZ", "air",  Decimal("10.50"), Decimal("105"),  9),
    # ── CN авто (road) ──────────────────────────────────────────────────────
    ("CN", "RU", "air",  Decimal("9.50"),  Decimal("95"),   7),  # дубль (idempotent)
    ("CN", "KZ", "air",  Decimal("8.50"),  Decimal("85"),   5),
    # ── JP → основные направления ──────────────────────────────────────────
    ("JP", "RU", "sea",  Decimal("5.00"),  Decimal("500"),  28),
    ("JP", "RU", "air",  Decimal("10.00"), Decimal("100"),  5),
    ("JP", "KZ", "sea",  Decimal("5.50"),  Decimal("550"),  32),
    ("JP", "KZ", "air",  Decimal("11.00"), Decimal("110"),  6),
    # ── KR → основные направления ──────────────────────────────────────────
    ("KR", "RU", "sea",  Decimal("4.80"),  Decimal("480"),  25),
    ("KR", "RU", "air",  Decimal("9.80"),  Decimal("98"),   5),
    ("KR", "KZ", "sea",  Decimal("5.20"),  Decimal("520"),  30),
    ("KR", "KZ", "air",  Decimal("10.50"), Decimal("105"),  6),
    # ── DE → основные направления ──────────────────────────────────────────
    ("DE", "RU", "sea",  Decimal("5.50"),  Decimal("550"),  30),
    ("DE", "RU", "air",  Decimal("6.00"),  Decimal("60"),   3),
    ("DE", "KZ", "sea",  Decimal("6.00"),  Decimal("600"),  35),
    ("DE", "KZ", "air",  Decimal("6.50"),  Decimal("65"),   4),
    # ── TR → основные направления ──────────────────────────────────────────
    ("TR", "RU", "sea",  Decimal("4.00"),  Decimal("400"),  18),
    ("TR", "RU", "air",  Decimal("7.50"),  Decimal("75"),   3),
    ("TR", "KZ", "sea",  Decimal("4.50"),  Decimal("450"),  22),
    ("TR", "KZ", "air",  Decimal("8.00"),  Decimal("80"),   4),
    # ── AE (UAE) → основные направления ────────────────────────────────────
    ("AE", "RU", "sea",  Decimal("3.50"),  Decimal("350"),  20),
    ("AE", "RU", "air",  Decimal("6.00"),  Decimal("60"),   3),
    ("AE", "KZ", "sea",  Decimal("4.00"),  Decimal("400"),  25),
    ("AE", "KZ", "air",  Decimal("6.50"),  Decimal("65"),   4),
    # ── IN (India) ──────────────────────────────────────────────────────────
    ("IN", "RU", "sea",  Decimal("3.80"),  Decimal("380"),  30),
    ("IN", "RU", "air",  Decimal("7.50"),  Decimal("75"),   5),
    ("IN", "KZ", "sea",  Decimal("4.20"),  Decimal("420"),  35),
    ("IN", "KZ", "air",  Decimal("8.00"),  Decimal("80"),   6),
    # ── US → основные направления ──────────────────────────────────────────
    ("US", "RU", "sea",  Decimal("6.00"),  Decimal("600"),  35),
    ("US", "RU", "air",  Decimal("10.00"), Decimal("100"),  6),
    ("US", "KZ", "sea",  Decimal("6.50"),  Decimal("650"),  40),
    ("US", "KZ", "air",  Decimal("10.50"), Decimal("105"),  7),
]


class Command(BaseCommand):
    help = "Засеять базовые тарифы LogisticsTariff."

    def handle(self, *args, **opts):
        ensure_dev_only(self)
        created = 0
        updated = 0
        for origin, dest, mode, rate, min_ch, days in TARIFFS:
            obj, made = LogisticsTariff.objects.get_or_create(
                origin_port=origin,
                dest_country=dest,
                mode=mode,
                defaults={
                    "rate_per_kg": rate,
                    "min_charge": min_ch,
                    "transit_days": days,
                    "source": "internal",
                    "is_active": True,
                },
            )
            if made:
                created += 1
                self.stdout.write(f"  СОЗДАН: {origin}→{dest} {mode} ${rate}/kg")
            else:
                # обновляем если ставка изменилась
                if obj.rate_per_kg != rate or obj.transit_days != days:
                    obj.rate_per_kg = rate
                    obj.min_charge = min_ch
                    obj.transit_days = days
                    obj.save()
                    updated += 1
                    self.stdout.write(f"  ОБНОВЛЁН: {origin}→{dest} {mode} ${rate}/kg")

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово: {created} создано, {updated} обновлено."
        ))
