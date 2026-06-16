"""Backfill: заполняет Part.title_ru детерминированным переводом title.

Идемпотентно: можно гонять повторно (например, после расширения словаря) —
просто перепишет title_ru. Пакетно, чтобы не держать 946К объектов в памяти.

    python manage.py translate_part_names               # все
    python manage.py translate_part_names --only-empty   # только без title_ru
    python manage.py translate_part_names --limit 1000   # образец
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from assistant.part_naming import translate_title
from marketplace.models import Part


class Command(BaseCommand):
    help = "Заполняет Part.title_ru переводом из словаря (assistant.part_naming)."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=2000,
                            help="Размер пакета bulk_update (по умолчанию 2000).")
        parser.add_argument("--only-empty", action="store_true",
                            help="Обрабатывать только записи с пустым title_ru.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Ограничить число записей (0 = все).")

    def handle(self, *args, **opts):
        batch = opts["batch"]
        qs = Part.objects.all().only("id", "title", "title_ru").order_by("id")
        if opts["only_empty"]:
            qs = qs.filter(title_ru="")
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        total = qs.count()
        self.stdout.write(f"К обработке: {total:,} позиций")

        processed = translated = changed = 0
        buf: list[Part] = []
        for p in qs.iterator(chunk_size=batch):
            processed += 1
            new = translate_title(p.title)
            if new:
                translated += 1
            if new != p.title_ru:
                p.title_ru = new
                buf.append(p)
            if len(buf) >= batch:
                with transaction.atomic():
                    Part.objects.bulk_update(buf, ["title_ru"])
                changed += len(buf)
                buf.clear()
                self.stdout.write(f"  …{processed:,}/{total:,}")
        if buf:
            with transaction.atomic():
                Part.objects.bulk_update(buf, ["title_ru"])
            changed += len(buf)

        pct = (100 * translated / processed) if processed else 0
        self.stdout.write(self.style.SUCCESS(
            f"Готово. Обработано {processed:,}, переведено {translated:,} "
            f"({pct:.1f}%), записано изменений {changed:,}."
        ))
