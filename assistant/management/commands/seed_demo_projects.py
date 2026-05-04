"""Seed 4 demo projects for the Chat-First UI.

Usage:
    python manage.py seed_demo_projects
    python manage.py seed_demo_projects --user demo_buyer
    python manage.py seed_demo_projects --reset  # delete existing first
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from assistant.models import Project, ProjectDocument

User = get_user_model()


PROJECTS = [
    {
        "name": "Norilsk Q2 procurement",
        "code": "NORQ2",
        "customer": "Norilsk Nickel — Kola Division",
        "tags": ["квартальная закупка", "CAT 988H", "793F"],
        "deadline_offset": 15,  # days from today
        "dot_color": "green",
        "description": "Квартальная закупка запчастей для парка горной техники Кольской дивизии.",
        "documents": [
            {"name": "spec_q2_2026.xlsx", "doctype": "spec", "size_bytes": 38 * 1024,
             "meta": {"summary": "39 позиций · 4 категории"}},
            {"name": "fleet_kola.pdf", "doctype": "fleet", "size_bytes": 412 * 1024,
             "meta": {"summary": "12 единиц · CAT 988H, 793F, D8T"}},
            {"name": "drawing_track_shoes.pdf", "doctype": "drawing", "size_bytes": 156 * 1024,
             "meta": {"summary": "Чертёж track shoes для D8T"}},
            {"name": "regulation_to_caterpillar.pdf", "doctype": "regulation", "size_bytes": 1240 * 1024,
             "meta": {"summary": "Регламент ТО Caterpillar"}},
            {"name": "delivery_conditions_2026.docx", "doctype": "conditions", "size_bytes": 28 * 1024,
             "meta": {"summary": "Incoterms DAP Мурманск"}},
            {"name": "contract_xcmg_2026.pdf", "doctype": "contract", "size_bytes": 220 * 1024,
             "meta": {"summary": "Рамочный договор XCMG"}},
        ],
    },
    {
        "name": "Polyus Olimpiada",
        "code": "POLOL",
        "customer": "Полюс — Олимпиадинское ГОК",
        "tags": ["золотодобыча", "Komatsu", "PC4000"],
        "deadline_offset": 30,
        "dot_color": "orange",
        "description": "Поставка запчастей для экскаваторов Komatsu PC4000.",
        "documents": [
            {"name": "spec_olimpiada.xlsx", "doctype": "spec", "size_bytes": 22 * 1024,
             "meta": {"summary": "18 позиций"}},
            {"name": "fleet_polyus.pdf", "doctype": "fleet", "size_bytes": 186 * 1024,
             "meta": {"summary": "5 экскаваторов PC4000"}},
        ],
    },
    {
        "name": "SUEK Borodino",
        "code": "SUEKB",
        "customer": "СУЭК — Бородинский разрез",
        "tags": ["уголь", "БЕЛАЗ-75710", "конвейерные ленты"],
        "deadline_offset": 60,
        "dot_color": "blue",
        "description": "Конвейерные ленты и запчасти для БЕЛАЗ-75710.",
        "documents": [
            {"name": "spec_borodino_belaz.xlsx", "doctype": "spec", "size_bytes": 19 * 1024,
             "meta": {"summary": "8 позиций"}},
        ],
    },
    {
        "name": "EuroChem Kovdor",
        "code": "EUKOV",
        "customer": "ЕвроХим — Ковдорский ГОК",
        "tags": ["апатит", "MetsoOutotec", "дробилки"],
        "deadline_offset": 45,
        "dot_color": "purple",
        "description": "Запчасти к дробильному оборудованию Metso Outotec.",
        "documents": [
            {"name": "spec_kovdor_crusher.xlsx", "doctype": "spec", "size_bytes": 14 * 1024,
             "meta": {"summary": "11 позиций"}},
            {"name": "drawing_jaw_plates.pdf", "doctype": "drawing", "size_bytes": 95 * 1024,
             "meta": {"summary": "Чертёж щёк дробилки"}},
        ],
    },
]


class Command(BaseCommand):
    help = "Seed demo projects for Chat-First UI sidebar"

    def add_arguments(self, parser):
        parser.add_argument("--user", action="append", default=[],
                            help="Username(s) to own these projects (repeat for multiple)")
        parser.add_argument("--all-demo", action="store_true",
                            help="Seed for demo_buyer, demo_seller, demo_operator, Kosta")
        parser.add_argument("--reset", action="store_true",
                            help="Delete existing projects for these user(s) first")

    def _seed_user(self, user, reset):
        if reset:
            n, _ = Project.objects.filter(owner=user).delete()
            self.stdout.write(self.style.WARNING(f"  [{user.username}] deleted {n} existing project(s)"))

        today = date.today()
        created, skipped = 0, 0

        for proj in PROJECTS:
            existing = Project.objects.filter(owner=user, code=proj["code"]).first()
            if existing:
                skipped += 1
                continue

            p = Project.objects.create(
                owner=user,
                name=proj["name"],
                code=proj["code"],
                customer=proj["customer"],
                tags=proj["tags"],
                deadline=today + timedelta(days=proj["deadline_offset"]),
                description=proj["description"],
                dot_color=proj["dot_color"],
            )
            for doc in proj["documents"]:
                ProjectDocument.objects.create(
                    project=p,
                    name=doc["name"],
                    doctype=doc["doctype"],
                    status="processed",
                    size_bytes=doc["size_bytes"],
                    meta=doc["meta"],
                )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"  [{user.username}] created: {created}, skipped: {skipped}"
        ))

    def handle(self, *args, **opts):
        usernames = list(opts["user"])
        if opts["all_demo"]:
            usernames += ["demo_buyer", "demo_seller", "demo_operator", "Kosta"]
        if not usernames:
            usernames = ["demo_buyer"]
        usernames = list(dict.fromkeys(usernames))  # dedupe, preserve order

        for username in usernames:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"User '{username}' not found — skipping"))
                continue
            self._seed_user(user, opts["reset"])

        self.stdout.write(self.style.SUCCESS("\nDone."))
