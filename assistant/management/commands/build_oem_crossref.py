"""
Нормализует oem_number → oem_clean для всех Part и строит кроссреференс-отчёт.

Нормализация: убрать всё кроме A-Z0-9, привести к uppercase.
  CNH.98489690 → CNH98489690
  3K-3124      → 3K3124
  6505-67-5920 → 6505675920

Запуск:
    python manage.py build_oem_crossref            # заполнить oem_clean
    python manage.py build_oem_crossref --report   # + вывести топ кроссреференсов
"""
from __future__ import annotations

import re
from django.core.management.base import BaseCommand
from django.db import connection

from marketplace.models import Part


OEM_NORM_RE = re.compile(r"[^A-Z0-9]")


def normalize(oem: str) -> str:
    return OEM_NORM_RE.sub("", oem.upper())


BATCH = 5000


class Command(BaseCommand):
    help = "Заполнить oem_clean + кроссреференс"

    def add_arguments(self, parser):
        parser.add_argument("--report", action="store_true",
                            help="Вывести топ кроссреференсов после заполнения")
        parser.add_argument("--only-empty", action="store_true",
                            help="Обновить только записи где oem_clean пустой")

    def handle(self, *args, **options):
        qs = Part.objects.only("id", "oem_number", "oem_clean")
        if options["only_empty"]:
            qs = qs.filter(oem_clean="")

        total = qs.count()
        self.stdout.write(f"Обновляем {total:,} позиций...")

        updated = 0
        batch = []
        for p in qs.iterator(chunk_size=BATCH):
            clean = normalize(p.oem_number)
            if clean != p.oem_clean:
                p.oem_clean = clean
                batch.append(p)
                updated += 1
            if len(batch) >= BATCH:
                Part.objects.bulk_update(batch, ["oem_clean"])
                batch = []
                self.stdout.write(f"  ... {updated:,}")
        if batch:
            Part.objects.bulk_update(batch, ["oem_clean"])

        self.stdout.write(self.style.SUCCESS(f"✓ Обновлено: {updated:,} из {total:,}"))

        if options["report"]:
            self._print_report()

    def _print_report(self):
        self.stdout.write("\n=== Топ кроссреференсов (один OEM у 3+ складов) ===")
        with connection.cursor() as cur:
            cur.execute("""
                SELECT p.oem_clean,
                       COUNT(DISTINCT p.warehouse_id) AS wh_cnt,
                       COUNT(p.id)                    AS parts_cnt,
                       MIN(p.price)                   AS min_price,
                       MAX(p.price)                   AS max_price,
                       GROUP_CONCAT(DISTINCT b.name)  AS brands
                FROM marketplace_part p
                LEFT JOIN marketplace_brand b ON b.id = p.brand_id
                WHERE p.oem_clean != ''
                  AND LENGTH(p.oem_clean) >= 5
                GROUP BY p.oem_clean
                HAVING wh_cnt >= 3
                ORDER BY wh_cnt DESC, parts_cnt DESC
                LIMIT 20
            """)
            rows = cur.fetchall()

        self.stdout.write(f"{'OEM':<20} {'Складов':>7} {'Позиций':>8} {'Цена min–max':>22}  Бренды")
        self.stdout.write("-" * 90)
        for oem, wh_cnt, parts_cnt, mn, mx, brands in rows:
            price_range = f"{mn} – {mx}"
            brands_short = (brands or "")[:40]
            self.stdout.write(f"{oem:<20} {wh_cnt:>7} {parts_cnt:>8} {price_range:>22}  {brands_short}")
