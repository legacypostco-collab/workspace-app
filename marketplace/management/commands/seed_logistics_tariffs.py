"""Сидит базовые логистические тарифы для главных направлений.

Базируется на актуальных индустриальных ставках 2024-2025 (FBX/Drewry
weekly index, средние spot-rates крупных форвардеров). Цифры округлены
и помечены source='internal' — продавец/оператор может переопределить.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from marketplace.models import LogisticsTariff

TARIFFS = [
    # (origin_port_or_country, dest_country, mode, rate_kg, min_charge, days)
    # ── Китай → Россия ────────────────────────────────────────
    ("CN", "RU", "sea",  "0.45",  "40",  35),
    ("CN", "RU", "air",  "4.50",  "60",  7),
    ("CN", "RU", "auto", "1.20",  "70",  18),  # авто через Казахстан/Монголию
    ("CNNGB", "RU", "sea", "0.40", "40", 30),
    ("CNSHA", "RU", "sea", "0.42", "40", 32),
    ("PKX", "RU", "air", "4.20", "60", 6),
    # ── Турция → Россия ───────────────────────────────────────
    ("TR", "RU", "sea",  "0.55",  "50",  18),
    ("TR", "RU", "air",  "3.80",  "55",  4),
    ("TR", "RU", "auto", "1.50",  "60",  10),  # через Грузию/Азербайджан
    ("TRMER", "RU", "sea", "0.50", "45", 16),
    ("ESB", "RU", "air", "3.60", "55", 3),
    # ── ОАЭ → Россия ──────────────────────────────────────────
    ("AE", "RU", "sea",  "0.60",  "60",  22),
    ("AE", "RU", "air",  "4.80",  "70",  5),
    ("AE", "RU", "auto", "1.80",  "80",  15),  # реально редко, но возможно
    # ── Нидерланды → Россия ───────────────────────────────────
    ("NL", "RU", "sea",  "0.95",  "80",  28),
    ("NL", "RU", "air",  "6.50",  "100", 4),
    ("NL", "RU", "auto", "2.20",  "120", 12),
    # ── Казахстан → Россия (наземно через границу) ───────────
    ("KZ", "RU", "auto", "0.60",  "30",  6),   # основной mode
    ("KZ", "RU", "air",  "3.00",  "40",  3),
    # ── Китай → Казахстан ─────────────────────────────────────
    ("CN", "KZ", "sea",  "0.40",  "35", 25),
    ("CN", "KZ", "air",  "4.20",  "55", 6),
    ("CN", "KZ", "auto", "0.90",  "55", 12),
    # ── Турция → Казахстан ────────────────────────────────────
    ("TR", "KZ", "sea",  "0.50",  "45", 20),
    ("TR", "KZ", "air",  "3.50",  "50", 4),
    ("TR", "KZ", "auto", "1.30",  "55", 14),
]


class Command(BaseCommand):
    help = "Сидит базовые логистические тарифы для главных маршрутов"

    def handle(self, *args, **opts):
        created = updated = 0
        for origin, dest, mode, rate, min_chg, days in TARIFFS:
            obj, was_created = LogisticsTariff.objects.update_or_create(
                origin_port=origin, dest_country=dest, mode=mode,
                source="internal",
                defaults=dict(
                    rate_per_kg=Decimal(rate),
                    min_charge=Decimal(min_chg),
                    transit_days=days,
                    is_active=True,
                ),
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(
            f"Тарифы засеяны: {created} создано, {updated} обновлено"
        ))
