"""Seed FAQ_ENTRIES (хардкод в support_hub) → таблицу KnowledgeBaseEntry.

Запускается один раз при первом деплое (или после reset БД на dev).
Идемпотентна: пропускает entry если такой `question` уже есть.

Маппинг category emoji→slug:
  📝 Регистрация       → registration
  🛡 KYB / Верификация → kyb
  💰 Заказ и оплата    → payment
  🚚 Доставка и сроки  → delivery
  🧾 Рекламации        → claims
  👥 Контакты сторон   → contacts
  🎁 Бонусы            → bonuses
  ⚙️ Платформа         → platform
  🛟 Поддержка         → support

Запуск:
    python manage.py seed_kb_faq
    python manage.py seed_kb_faq --reset    # сначала очистить таблицу
"""
from django.core.management.base import BaseCommand

from assistant.support_hub import FAQ_ENTRIES
from marketplace.models import KnowledgeBaseEntry

_CATEGORY_MAP = {
    "Регистрация":         "registration",
    "KYB / Верификация":   "kyb",
    "Заказы и оплата":     "payment",
    "Доставка и сроки":    "delivery",
    "Рекламации":          "claims",
    "Связь со сторонами":  "contacts",
    "Бонусы":              "bonuses",
    "Платформа":           "platform",
    "Поддержка":           "support",
}


class Command(BaseCommand):
    help = "Seed FAQ from hardcoded FAQ_ENTRIES into KnowledgeBaseEntry."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Drop all KB entries before seeding.")

    def handle(self, *args, **opts):
        if opts["reset"]:
            n = KnowledgeBaseEntry.objects.all().count()
            KnowledgeBaseEntry.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"  ⚠ Удалено {n} существующих entry"))

        created, skipped = 0, 0
        for i, e in enumerate(FAQ_ENTRIES):
            cat_slug = _CATEGORY_MAP.get(e["category"], "other")
            obj, was_created = KnowledgeBaseEntry.objects.get_or_create(
                question=e["q"],
                defaults={
                    "category":  cat_slug,
                    "answer":    e["a"],
                    "sort_order": (i + 1) * 10,  # шаг 10 — оператор сможет вставлять между
                    "is_active": True,
                },
            )
            if was_created:
                created += 1
                self.stdout.write(f"  + [{cat_slug}] {e['q'][:60]}")
            else:
                skipped += 1
        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Создано {created} · пропущено (уже было) {skipped}"
        ))
