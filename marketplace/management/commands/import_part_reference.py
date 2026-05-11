"""Импорт эталонных данных запчастей в PartReference.

Источники:
  - customs:  таможенные базы (CSV с HS code + вес)
  - dealer:   каталоги дилеров (Caterpillar, Komatsu, ...)
  - oem:      OEM-каталоги

Формат CSV: oem_number;brand;title;weight_kg;length_cm;width_cm;height_cm;hs_code;country

Запуск:
  python manage.py import_part_reference data/customs_2024.csv --source customs --ref-id "decl-12345"
  python manage.py import_part_reference data/komatsu_2024.csv --source dealer --brand Komatsu
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Импорт эталонных данных запчастей (customs/dealer/OEM)"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str, help="Путь к CSV-файлу")
        parser.add_argument("--source", type=str, default="manual",
                             choices=["customs", "dealer", "oem", "manual"])
        parser.add_argument("--brand", type=str, default="",
                             help="Дефолтный бренд (если не указан в файле)")
        parser.add_argument("--ref-id", type=str, default="",
                             help="ID ссылки на источник (номер декларации и т.п.)")
        parser.add_argument("--confidence", type=float, default=1.0,
                             help="0.0-1.0 (customs/dealer = 1.0)")
        parser.add_argument("--delimiter", type=str, default=";")

    def handle(self, *args, **opts):
        from marketplace.models import PartReference

        path = opts["csv_path"]
        source = opts["source"]
        default_brand = opts["brand"]
        ref_id = opts["ref_id"]
        confidence = opts["confidence"]
        delim = opts["delimiter"]

        try:
            f = open(path, "r", encoding="utf-8-sig")
        except OSError as e:
            raise CommandError(f"Cannot open {path}: {e}")

        reader = csv.DictReader(f, delimiter=delim)
        required = {"oem_number"}
        if not required.issubset({c.lower() for c in (reader.fieldnames or [])}):
            raise CommandError(
                f"CSV must have at least: oem_number. Got: {reader.fieldnames}"
            )

        def _dec(val):
            if not val:
                return None
            try:
                return Decimal(str(val).strip().replace(",", "."))
            except (InvalidOperation, ValueError):
                return None

        created = 0
        updated = 0
        skipped = 0
        with transaction.atomic():
            for row in reader:
                oem = (row.get("oem_number") or "").strip()
                if not oem:
                    skipped += 1
                    continue
                brand = (row.get("brand") or default_brand or "").strip()
                obj, was_created = PartReference.objects.update_or_create(
                    oem_number=oem,
                    brand=brand,
                    source=source,
                    defaults={
                        "title":             (row.get("title") or "").strip()[:255],
                        "weight_kg":         _dec(row.get("weight_kg")),
                        "length_cm":         _dec(row.get("length_cm")),
                        "width_cm":          _dec(row.get("width_cm")),
                        "height_cm":         _dec(row.get("height_cm")),
                        "hs_code":           (row.get("hs_code") or "").strip()[:20],
                        "country_of_origin": (row.get("country") or "").strip()[:80],
                        "source_ref":        ref_id,
                        "confidence":        confidence,
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        f.close()
        self.stdout.write(self.style.SUCCESS(
            f"PartReference: created={created} updated={updated} skipped={skipped} "
            f"source={source} confidence={confidence}"
        ))
