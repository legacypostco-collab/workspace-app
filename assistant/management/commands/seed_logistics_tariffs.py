"""Seed базовых тарифов LogisticsTariff.

Матрица: 11 origin-стран × 11 dest-стран × 3 mode (sea/air/auto).
Auto-режим только для маршрутов с реальным наземным коридором.
Все записи idempotent (get_or_create + update при изменении ставки).

Destinations: RU KZ BY UZ AM AZ GE KG TJ TM MN
Origins:      CN JP KR DE TR AE IN US IT NL ES KZ(→RU)

Usage:
    python manage.py seed_logistics_tariffs          # dev
    ALLOW_SEED_IN_PROD=1 python manage.py seed_logistics_tariffs
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from assistant.management._seed_guard import ensure_dev_only
from marketplace.models import LogisticsTariff

# ── Тарифная матрица ──────────────────────────────────────────────────────────
# (origin, dest, mode, rate_per_kg, min_charge, transit_days)
TARIFFS = [

    # ════════════════════════════════════════════════════════════════════════
    # CN — Китай (основной поставщик, все маршруты)
    # ════════════════════════════════════════════════════════════════════════
    # CN → RU
    ("CN", "RU", "sea",  Decimal("4.20"),  Decimal("420"),  42),
    ("CN", "RU", "air",  Decimal("9.50"),  Decimal("95"),    7),
    ("CN", "RU", "auto", Decimal("1.20"),  Decimal("70"),   18),
    # CN → KZ
    ("CN", "KZ", "sea",  Decimal("3.80"),  Decimal("380"),  35),
    ("CN", "KZ", "air",  Decimal("8.50"),  Decimal("85"),    5),
    ("CN", "KZ", "auto", Decimal("0.90"),  Decimal("55"),   12),
    # CN → BY
    ("CN", "BY", "sea",  Decimal("5.00"),  Decimal("500"),  45),
    ("CN", "BY", "air",  Decimal("10.00"), Decimal("100"),   8),
    ("CN", "BY", "auto", Decimal("1.40"),  Decimal("80"),   22),
    # CN → UZ
    ("CN", "UZ", "sea",  Decimal("4.50"),  Decimal("450"),  40),
    ("CN", "UZ", "air",  Decimal("9.00"),  Decimal("90"),    6),
    ("CN", "UZ", "auto", Decimal("1.10"),  Decimal("65"),   15),
    # CN → AM
    ("CN", "AM", "sea",  Decimal("5.20"),  Decimal("520"),  48),
    ("CN", "AM", "air",  Decimal("10.50"), Decimal("105"),   9),
    ("CN", "AM", "auto", Decimal("1.50"),  Decimal("85"),   25),
    # CN → AZ
    ("CN", "AZ", "sea",  Decimal("5.20"),  Decimal("520"),  48),
    ("CN", "AZ", "air",  Decimal("10.50"), Decimal("105"),   9),
    ("CN", "AZ", "auto", Decimal("1.40"),  Decimal("80"),   22),
    # CN → GE
    ("CN", "GE", "sea",  Decimal("5.50"),  Decimal("550"),  50),
    ("CN", "GE", "air",  Decimal("11.00"), Decimal("110"),   9),
    ("CN", "GE", "auto", Decimal("1.60"),  Decimal("90"),   28),
    # CN → KG
    ("CN", "KG", "sea",  Decimal("4.80"),  Decimal("480"),  42),
    ("CN", "KG", "air",  Decimal("9.50"),  Decimal("95"),    6),
    ("CN", "KG", "auto", Decimal("1.00"),  Decimal("60"),   13),
    # CN → TJ
    ("CN", "TJ", "sea",  Decimal("4.80"),  Decimal("480"),  42),
    ("CN", "TJ", "air",  Decimal("9.50"),  Decimal("95"),    7),
    ("CN", "TJ", "auto", Decimal("1.00"),  Decimal("60"),   14),
    # CN → TM
    ("CN", "TM", "sea",  Decimal("5.00"),  Decimal("500"),  44),
    ("CN", "TM", "air",  Decimal("9.80"),  Decimal("98"),    7),
    ("CN", "TM", "auto", Decimal("1.10"),  Decimal("65"),   16),
    # CN → MN (Монголия — граница с CN)
    ("CN", "MN", "sea",  Decimal("4.00"),  Decimal("400"),  20),
    ("CN", "MN", "air",  Decimal("8.00"),  Decimal("80"),    4),
    ("CN", "MN", "auto", Decimal("0.80"),  Decimal("50"),    8),

    # ════════════════════════════════════════════════════════════════════════
    # JP — Япония (морской + авиа; нет наземного коридора)
    # ════════════════════════════════════════════════════════════════════════
    ("JP", "RU", "sea",  Decimal("5.00"),  Decimal("500"),  28),
    ("JP", "RU", "air",  Decimal("10.00"), Decimal("100"),   5),
    ("JP", "RU", "auto", Decimal("2.80"),  Decimal("90"),   22),  # через Владивосток→авто
    ("JP", "KZ", "sea",  Decimal("5.50"),  Decimal("550"),  32),
    ("JP", "KZ", "air",  Decimal("11.00"), Decimal("110"),   6),
    ("JP", "BY", "sea",  Decimal("6.00"),  Decimal("600"),  40),
    ("JP", "BY", "air",  Decimal("11.50"), Decimal("115"),   8),
    ("JP", "UZ", "sea",  Decimal("6.00"),  Decimal("600"),  40),
    ("JP", "UZ", "air",  Decimal("11.50"), Decimal("115"),   8),
    ("JP", "AM", "sea",  Decimal("6.50"),  Decimal("650"),  45),
    ("JP", "AM", "air",  Decimal("12.00"), Decimal("120"),   9),
    ("JP", "AZ", "sea",  Decimal("6.50"),  Decimal("650"),  45),
    ("JP", "AZ", "air",  Decimal("12.00"), Decimal("120"),   9),
    ("JP", "GE", "sea",  Decimal("6.80"),  Decimal("680"),  50),
    ("JP", "GE", "air",  Decimal("12.50"), Decimal("125"),  10),
    ("JP", "KG", "sea",  Decimal("6.00"),  Decimal("600"),  40),
    ("JP", "KG", "air",  Decimal("11.50"), Decimal("115"),   8),
    ("JP", "TJ", "sea",  Decimal("6.00"),  Decimal("600"),  40),
    ("JP", "TJ", "air",  Decimal("11.50"), Decimal("115"),   8),
    ("JP", "TM", "sea",  Decimal("6.20"),  Decimal("620"),  42),
    ("JP", "TM", "air",  Decimal("11.80"), Decimal("118"),   9),
    ("JP", "MN", "sea",  Decimal("5.50"),  Decimal("550"),  25),
    ("JP", "MN", "air",  Decimal("10.50"), Decimal("105"),   5),

    # ════════════════════════════════════════════════════════════════════════
    # KR — Южная Корея
    # ════════════════════════════════════════════════════════════════════════
    ("KR", "RU", "sea",  Decimal("4.80"),  Decimal("480"),  25),
    ("KR", "RU", "air",  Decimal("9.80"),  Decimal("98"),    5),
    ("KR", "RU", "auto", Decimal("1.80"),  Decimal("70"),   16),  # через Владивосток→авто
    ("KR", "KZ", "sea",  Decimal("5.20"),  Decimal("520"),  30),
    ("KR", "KZ", "air",  Decimal("10.50"), Decimal("105"),   6),
    ("KR", "BY", "sea",  Decimal("5.80"),  Decimal("580"),  38),
    ("KR", "BY", "air",  Decimal("11.00"), Decimal("110"),   8),
    ("KR", "UZ", "sea",  Decimal("5.50"),  Decimal("550"),  35),
    ("KR", "UZ", "air",  Decimal("10.80"), Decimal("108"),   7),
    ("KR", "AM", "sea",  Decimal("6.00"),  Decimal("600"),  42),
    ("KR", "AM", "air",  Decimal("11.50"), Decimal("115"),   9),
    ("KR", "AZ", "sea",  Decimal("6.00"),  Decimal("600"),  42),
    ("KR", "AZ", "air",  Decimal("11.50"), Decimal("115"),   9),
    ("KR", "GE", "sea",  Decimal("6.20"),  Decimal("620"),  45),
    ("KR", "GE", "air",  Decimal("12.00"), Decimal("120"),  10),
    ("KR", "KG", "sea",  Decimal("5.50"),  Decimal("550"),  35),
    ("KR", "KG", "air",  Decimal("10.80"), Decimal("108"),   7),
    ("KR", "TJ", "sea",  Decimal("5.50"),  Decimal("550"),  35),
    ("KR", "TJ", "air",  Decimal("10.80"), Decimal("108"),   8),
    ("KR", "TM", "sea",  Decimal("5.80"),  Decimal("580"),  38),
    ("KR", "TM", "air",  Decimal("11.20"), Decimal("112"),   8),
    ("KR", "MN", "sea",  Decimal("5.00"),  Decimal("500"),  22),
    ("KR", "MN", "air",  Decimal("10.00"), Decimal("100"),   5),

    # ════════════════════════════════════════════════════════════════════════
    # DE — Германия (сухопутный коридор через Польшу/Беларусь)
    # ════════════════════════════════════════════════════════════════════════
    ("DE", "RU", "sea",  Decimal("5.50"),  Decimal("550"),  30),
    ("DE", "RU", "air",  Decimal("6.00"),  Decimal("60"),    3),
    ("DE", "RU", "auto", Decimal("2.20"),  Decimal("120"),  12),
    ("DE", "KZ", "sea",  Decimal("6.00"),  Decimal("600"),  35),
    ("DE", "KZ", "air",  Decimal("6.50"),  Decimal("65"),    4),
    ("DE", "KZ", "auto", Decimal("2.80"),  Decimal("140"),  18),
    ("DE", "BY", "sea",  Decimal("5.00"),  Decimal("500"),  25),
    ("DE", "BY", "air",  Decimal("5.80"),  Decimal("58"),    3),
    ("DE", "BY", "auto", Decimal("1.80"),  Decimal("100"),   8),
    ("DE", "UZ", "sea",  Decimal("6.50"),  Decimal("650"),  40),
    ("DE", "UZ", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("DE", "UZ", "auto", Decimal("3.00"),  Decimal("150"),  22),
    ("DE", "AM", "sea",  Decimal("6.80"),  Decimal("680"),  35),
    ("DE", "AM", "air",  Decimal("7.20"),  Decimal("72"),    5),
    ("DE", "AM", "auto", Decimal("2.50"),  Decimal("130"),  16),
    ("DE", "AZ", "sea",  Decimal("6.80"),  Decimal("680"),  35),
    ("DE", "AZ", "air",  Decimal("7.20"),  Decimal("72"),    5),
    ("DE", "AZ", "auto", Decimal("2.50"),  Decimal("130"),  16),
    ("DE", "GE", "sea",  Decimal("7.00"),  Decimal("700"),  38),
    ("DE", "GE", "air",  Decimal("7.50"),  Decimal("75"),    5),
    ("DE", "GE", "auto", Decimal("2.60"),  Decimal("135"),  18),
    ("DE", "KG", "sea",  Decimal("6.50"),  Decimal("650"),  40),
    ("DE", "KG", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("DE", "TJ", "sea",  Decimal("6.50"),  Decimal("650"),  40),
    ("DE", "TJ", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("DE", "TM", "sea",  Decimal("6.80"),  Decimal("680"),  42),
    ("DE", "TM", "air",  Decimal("7.20"),  Decimal("72"),    6),
    ("DE", "MN", "sea",  Decimal("7.00"),  Decimal("700"),  45),
    ("DE", "MN", "air",  Decimal("7.50"),  Decimal("75"),    7),

    # ════════════════════════════════════════════════════════════════════════
    # TR — Турция (коридор через Кавказ/Каспий)
    # ════════════════════════════════════════════════════════════════════════
    ("TR", "RU", "sea",  Decimal("4.00"),  Decimal("400"),  18),
    ("TR", "RU", "air",  Decimal("7.50"),  Decimal("75"),    3),
    ("TR", "RU", "auto", Decimal("1.50"),  Decimal("60"),   10),
    ("TR", "KZ", "sea",  Decimal("4.50"),  Decimal("450"),  22),
    ("TR", "KZ", "air",  Decimal("8.00"),  Decimal("80"),    4),
    ("TR", "KZ", "auto", Decimal("1.30"),  Decimal("55"),   14),
    ("TR", "BY", "sea",  Decimal("4.20"),  Decimal("420"),  20),
    ("TR", "BY", "air",  Decimal("7.80"),  Decimal("78"),    4),
    ("TR", "BY", "auto", Decimal("1.60"),  Decimal("65"),   12),
    ("TR", "UZ", "sea",  Decimal("5.00"),  Decimal("500"),  28),
    ("TR", "UZ", "air",  Decimal("8.50"),  Decimal("85"),    5),
    ("TR", "UZ", "auto", Decimal("1.40"),  Decimal("60"),   16),
    ("TR", "AM", "sea",  Decimal("3.50"),  Decimal("350"),  12),
    ("TR", "AM", "air",  Decimal("7.00"),  Decimal("70"),    3),
    ("TR", "AM", "auto", Decimal("1.20"),  Decimal("50"),    8),
    ("TR", "AZ", "sea",  Decimal("3.20"),  Decimal("320"),  10),
    ("TR", "AZ", "air",  Decimal("6.50"),  Decimal("65"),    2),
    ("TR", "AZ", "auto", Decimal("1.10"),  Decimal("45"),    6),
    ("TR", "GE", "sea",  Decimal("3.00"),  Decimal("300"),   8),
    ("TR", "GE", "air",  Decimal("6.20"),  Decimal("62"),    2),
    ("TR", "GE", "auto", Decimal("1.00"),  Decimal("40"),    4),
    ("TR", "KG", "sea",  Decimal("5.00"),  Decimal("500"),  28),
    ("TR", "KG", "air",  Decimal("8.50"),  Decimal("85"),    5),
    ("TR", "KG", "auto", Decimal("1.50"),  Decimal("65"),   18),
    ("TR", "TJ", "sea",  Decimal("5.20"),  Decimal("520"),  30),
    ("TR", "TJ", "air",  Decimal("8.80"),  Decimal("88"),    5),
    ("TR", "TM", "sea",  Decimal("5.00"),  Decimal("500"),  28),
    ("TR", "TM", "air",  Decimal("8.50"),  Decimal("85"),    5),
    ("TR", "MN", "sea",  Decimal("6.50"),  Decimal("650"),  45),
    ("TR", "MN", "air",  Decimal("10.00"), Decimal("100"),   8),

    # ════════════════════════════════════════════════════════════════════════
    # AE — ОАЭ (Дубай — хаб для Ближнего Востока и Южной Азии)
    # ════════════════════════════════════════════════════════════════════════
    ("AE", "RU", "sea",  Decimal("3.50"),  Decimal("350"),  20),
    ("AE", "RU", "air",  Decimal("6.00"),  Decimal("60"),    3),
    ("AE", "RU", "auto", Decimal("1.80"),  Decimal("80"),   15),
    ("AE", "KZ", "sea",  Decimal("4.00"),  Decimal("400"),  25),
    ("AE", "KZ", "air",  Decimal("6.50"),  Decimal("65"),    4),
    ("AE", "KZ", "auto", Decimal("2.00"),  Decimal("90"),   18),
    ("AE", "BY", "sea",  Decimal("4.50"),  Decimal("450"),  28),
    ("AE", "BY", "air",  Decimal("7.00"),  Decimal("70"),    4),
    ("AE", "UZ", "sea",  Decimal("4.20"),  Decimal("420"),  22),
    ("AE", "UZ", "air",  Decimal("6.80"),  Decimal("68"),    4),
    ("AE", "AM", "sea",  Decimal("3.80"),  Decimal("380"),  15),
    ("AE", "AM", "air",  Decimal("6.20"),  Decimal("62"),    3),
    ("AE", "AZ", "sea",  Decimal("3.80"),  Decimal("380"),  15),
    ("AE", "AZ", "air",  Decimal("6.20"),  Decimal("62"),    3),
    ("AE", "GE", "sea",  Decimal("4.00"),  Decimal("400"),  18),
    ("AE", "GE", "air",  Decimal("6.50"),  Decimal("65"),    3),
    ("AE", "KG", "sea",  Decimal("4.50"),  Decimal("450"),  28),
    ("AE", "KG", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("AE", "TJ", "sea",  Decimal("4.50"),  Decimal("450"),  28),
    ("AE", "TJ", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("AE", "TM", "sea",  Decimal("4.20"),  Decimal("420"),  22),
    ("AE", "TM", "air",  Decimal("6.80"),  Decimal("68"),    4),
    ("AE", "MN", "sea",  Decimal("6.00"),  Decimal("600"),  42),
    ("AE", "MN", "air",  Decimal("9.00"),  Decimal("90"),    7),

    # ════════════════════════════════════════════════════════════════════════
    # IN — Индия
    # ════════════════════════════════════════════════════════════════════════
    ("IN", "RU", "sea",  Decimal("3.80"),  Decimal("380"),  30),
    ("IN", "RU", "air",  Decimal("7.50"),  Decimal("75"),    5),
    ("IN", "RU", "auto", Decimal("2.20"),  Decimal("90"),   20),
    ("IN", "KZ", "sea",  Decimal("4.20"),  Decimal("420"),  35),
    ("IN", "KZ", "air",  Decimal("8.00"),  Decimal("80"),    6),
    ("IN", "BY", "sea",  Decimal("5.00"),  Decimal("500"),  38),
    ("IN", "BY", "air",  Decimal("8.50"),  Decimal("85"),    7),
    ("IN", "UZ", "sea",  Decimal("4.50"),  Decimal("450"),  32),
    ("IN", "UZ", "air",  Decimal("8.20"),  Decimal("82"),    6),
    ("IN", "AM", "sea",  Decimal("4.80"),  Decimal("480"),  35),
    ("IN", "AM", "air",  Decimal("8.50"),  Decimal("85"),    6),
    ("IN", "AZ", "sea",  Decimal("4.80"),  Decimal("480"),  35),
    ("IN", "AZ", "air",  Decimal("8.50"),  Decimal("85"),    6),
    ("IN", "GE", "sea",  Decimal("5.00"),  Decimal("500"),  38),
    ("IN", "GE", "air",  Decimal("8.80"),  Decimal("88"),    7),
    ("IN", "KG", "sea",  Decimal("4.50"),  Decimal("450"),  32),
    ("IN", "KG", "air",  Decimal("8.20"),  Decimal("82"),    6),
    ("IN", "TJ", "sea",  Decimal("4.50"),  Decimal("450"),  32),
    ("IN", "TJ", "air",  Decimal("8.20"),  Decimal("82"),    6),
    ("IN", "TM", "sea",  Decimal("4.80"),  Decimal("480"),  35),
    ("IN", "TM", "air",  Decimal("8.50"),  Decimal("85"),    7),
    ("IN", "MN", "sea",  Decimal("5.50"),  Decimal("550"),  42),
    ("IN", "MN", "air",  Decimal("9.50"),  Decimal("95"),    8),

    # ════════════════════════════════════════════════════════════════════════
    # US — США
    # ════════════════════════════════════════════════════════════════════════
    ("US", "RU", "sea",  Decimal("6.00"),  Decimal("600"),  35),
    ("US", "RU", "air",  Decimal("10.00"), Decimal("100"),   6),
    ("US", "KZ", "sea",  Decimal("6.50"),  Decimal("650"),  40),
    ("US", "KZ", "air",  Decimal("10.50"), Decimal("105"),   7),
    ("US", "BY", "sea",  Decimal("6.20"),  Decimal("620"),  38),
    ("US", "BY", "air",  Decimal("10.20"), Decimal("102"),   7),
    ("US", "UZ", "sea",  Decimal("6.80"),  Decimal("680"),  42),
    ("US", "UZ", "air",  Decimal("11.00"), Decimal("110"),   8),
    ("US", "AM", "sea",  Decimal("7.00"),  Decimal("700"),  42),
    ("US", "AM", "air",  Decimal("11.20"), Decimal("112"),   8),
    ("US", "AZ", "sea",  Decimal("7.00"),  Decimal("700"),  42),
    ("US", "AZ", "air",  Decimal("11.20"), Decimal("112"),   8),
    ("US", "GE", "sea",  Decimal("7.20"),  Decimal("720"),  45),
    ("US", "GE", "air",  Decimal("11.50"), Decimal("115"),   9),
    ("US", "KG", "sea",  Decimal("6.80"),  Decimal("680"),  42),
    ("US", "KG", "air",  Decimal("11.00"), Decimal("110"),   8),
    ("US", "TJ", "sea",  Decimal("6.80"),  Decimal("680"),  42),
    ("US", "TJ", "air",  Decimal("11.00"), Decimal("110"),   8),
    ("US", "TM", "sea",  Decimal("7.00"),  Decimal("700"),  44),
    ("US", "TM", "air",  Decimal("11.20"), Decimal("112"),   9),
    ("US", "MN", "sea",  Decimal("7.50"),  Decimal("750"),  50),
    ("US", "MN", "air",  Decimal("12.00"), Decimal("120"),  10),

    # ════════════════════════════════════════════════════════════════════════
    # IT — Италия
    # ════════════════════════════════════════════════════════════════════════
    ("IT", "RU", "sea",  Decimal("0.90"),  Decimal("75"),   26),
    ("IT", "RU", "air",  Decimal("6.20"),  Decimal("95"),    4),
    ("IT", "RU", "auto", Decimal("2.30"),  Decimal("110"),  13),
    ("IT", "KZ", "sea",  Decimal("5.80"),  Decimal("580"),  35),
    ("IT", "KZ", "air",  Decimal("6.80"),  Decimal("68"),    5),
    ("IT", "KZ", "auto", Decimal("3.00"),  Decimal("150"),  20),
    ("IT", "BY", "sea",  Decimal("4.80"),  Decimal("480"),  28),
    ("IT", "BY", "air",  Decimal("6.00"),  Decimal("60"),    4),
    ("IT", "BY", "auto", Decimal("2.00"),  Decimal("100"),  12),
    ("IT", "UZ", "sea",  Decimal("6.20"),  Decimal("620"),  38),
    ("IT", "UZ", "air",  Decimal("7.20"),  Decimal("72"),    5),
    ("IT", "AM", "sea",  Decimal("5.50"),  Decimal("550"),  30),
    ("IT", "AM", "air",  Decimal("6.50"),  Decimal("65"),    4),
    ("IT", "AZ", "sea",  Decimal("5.50"),  Decimal("550"),  30),
    ("IT", "AZ", "air",  Decimal("6.50"),  Decimal("65"),    4),
    ("IT", "GE", "sea",  Decimal("5.80"),  Decimal("580"),  32),
    ("IT", "GE", "air",  Decimal("6.80"),  Decimal("68"),    4),
    ("IT", "KG", "sea",  Decimal("6.50"),  Decimal("650"),  42),
    ("IT", "KG", "air",  Decimal("7.50"),  Decimal("75"),    6),

    # ════════════════════════════════════════════════════════════════════════
    # NL — Нидерланды (порт Роттердам — крупнейший в Европе)
    # ════════════════════════════════════════════════════════════════════════
    ("NL", "RU", "sea",  Decimal("0.95"),  Decimal("80"),   28),
    ("NL", "RU", "air",  Decimal("6.50"),  Decimal("100"),   4),
    ("NL", "RU", "auto", Decimal("2.20"),  Decimal("120"),  12),
    ("NL", "KZ", "sea",  Decimal("5.80"),  Decimal("580"),  35),
    ("NL", "KZ", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("NL", "KZ", "auto", Decimal("3.00"),  Decimal("150"),  20),
    ("NL", "BY", "sea",  Decimal("4.50"),  Decimal("450"),  25),
    ("NL", "BY", "air",  Decimal("6.20"),  Decimal("62"),    3),
    ("NL", "BY", "auto", Decimal("1.90"),  Decimal("95"),   10),
    ("NL", "UZ", "sea",  Decimal("6.20"),  Decimal("620"),  38),
    ("NL", "UZ", "air",  Decimal("7.30"),  Decimal("73"),    5),
    ("NL", "AM", "sea",  Decimal("5.80"),  Decimal("580"),  32),
    ("NL", "AM", "air",  Decimal("6.80"),  Decimal("68"),    5),
    ("NL", "AZ", "sea",  Decimal("5.80"),  Decimal("580"),  32),
    ("NL", "AZ", "air",  Decimal("6.80"),  Decimal("68"),    5),
    ("NL", "GE", "sea",  Decimal("6.00"),  Decimal("600"),  35),
    ("NL", "GE", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("NL", "KG", "sea",  Decimal("6.50"),  Decimal("650"),  42),
    ("NL", "KG", "air",  Decimal("7.50"),  Decimal("75"),    6),

    # ════════════════════════════════════════════════════════════════════════
    # ES — Испания
    # ════════════════════════════════════════════════════════════════════════
    ("ES", "RU", "sea",  Decimal("0.95"),  Decimal("80"),   30),
    ("ES", "RU", "air",  Decimal("6.30"),  Decimal("95"),    4),
    ("ES", "RU", "auto", Decimal("2.40"),  Decimal("120"),  14),
    ("ES", "KZ", "sea",  Decimal("6.00"),  Decimal("600"),  38),
    ("ES", "KZ", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("ES", "BY", "sea",  Decimal("5.00"),  Decimal("500"),  30),
    ("ES", "BY", "air",  Decimal("6.30"),  Decimal("63"),    4),
    ("ES", "UZ", "sea",  Decimal("6.50"),  Decimal("650"),  40),
    ("ES", "UZ", "air",  Decimal("7.30"),  Decimal("73"),    6),
    ("ES", "AM", "sea",  Decimal("5.80"),  Decimal("580"),  32),
    ("ES", "AM", "air",  Decimal("6.80"),  Decimal("68"),    5),
    ("ES", "AZ", "sea",  Decimal("5.80"),  Decimal("580"),  32),
    ("ES", "AZ", "air",  Decimal("6.80"),  Decimal("68"),    5),
    ("ES", "GE", "sea",  Decimal("6.00"),  Decimal("600"),  35),
    ("ES", "GE", "air",  Decimal("7.00"),  Decimal("70"),    5),
    ("ES", "KG", "sea",  Decimal("6.80"),  Decimal("680"),  44),
    ("ES", "KG", "air",  Decimal("7.80"),  Decimal("78"),    6),

    # ════════════════════════════════════════════════════════════════════════
    # KZ — Казахстан (транзитный хаб → RU)
    # ════════════════════════════════════════════════════════════════════════
    ("KZ", "RU", "sea",  Decimal("2.80"),  Decimal("120"),  18),
    ("KZ", "RU", "air",  Decimal("3.00"),  Decimal("40"),    3),
    ("KZ", "RU", "auto", Decimal("0.60"),  Decimal("30"),    6),
]


class Command(BaseCommand):
    help = "Засеять базовые тарифы LogisticsTariff (полная матрица)."

    def handle(self, *args, **opts):
        ensure_dev_only(self)
        created = 0
        updated = 0
        seen = set()
        for origin, dest, mode, rate, min_ch, days in TARIFFS:
            key = (origin, dest, mode)
            if key in seen:
                continue
            seen.add(key)
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
                if obj.rate_per_kg != rate or obj.transit_days != days:
                    obj.rate_per_kg = rate
                    obj.min_charge = min_ch
                    obj.transit_days = days
                    obj.is_active = True
                    obj.save()
                    updated += 1
                    self.stdout.write(f"  ОБНОВЛЁН: {origin}→{dest} {mode} ${rate}/kg")

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово: {created} создано, {updated} обновлено. "
            f"Пропущено дублей: {len(TARIFFS) - len(seen)}."
        ))
