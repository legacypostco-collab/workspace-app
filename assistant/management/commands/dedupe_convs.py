"""Разовая чистка дубликатов Conversation в одной категории на пользователя.

Накопились до того, как find_or_create_conv стал инвариантом «один активный
conv на (user, category)». Команда оставляет самый свежий активный conv
в каждой категории и архивирует остальные (is_active=False) — данные не
удаляются, можно откатить точечно.

Запуск:
    python manage.py dedupe_convs           # dry-run, показывает что будет сделано
    python manage.py dedupe_convs --apply   # применяет изменения
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from assistant.models import Conversation


class Command(BaseCommand):
    help = "Архивирует дубликаты Conversation в одной категории на пользователя."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Применить (без флага — dry-run)")

    def handle(self, *args, **opts):
        apply = bool(opts.get("apply"))
        # Группируем активные convs по (user_id, category)
        groups: dict[tuple[int, str], list[Conversation]] = defaultdict(list)
        for conv in Conversation.objects.filter(is_active=True).order_by("-updated_at"):
            groups[(conv.user_id, conv.category)].append(conv)

        total_dupes = 0
        affected_users = set()
        for (uid, cat), convs in groups.items():
            if len(convs) <= 1:
                continue
            keep = convs[0]  # самый свежий
            arch = convs[1:]
            total_dupes += len(arch)
            affected_users.add(uid)
            self.stdout.write(
                f"user={uid} cat={cat}: keep #{keep.id} '{keep.title[:40]}', "
                f"archive {len(arch)}: {[c.id for c in arch]}"
            )
            if apply:
                Conversation.objects.filter(
                    id__in=[c.id for c in arch]
                ).update(is_active=False)

        if not apply:
            self.stdout.write(self.style.WARNING(
                f"\nDRY-RUN: нашлось {total_dupes} дубликатов у {len(affected_users)} пользователей. "
                "Запустите с --apply чтобы применить."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\nАрхивировано {total_dupes} дубликатов у {len(affected_users)} пользователей."
            ))
