"""Seed test data for KYB queue and Claims.

Creates a handful of KYB submissions in different states and a few
OrderClaim records covering all kinds/statuses — so the operator can
open `/chat/` → 🛡 KYB поставщиков and 🧾 Рекламации and see the UI
populated with realistic-looking entries.

Usage:
    python manage.py seed_kyb_claims          # idempotent, top-up to ~6 of each
    python manage.py seed_kyb_claims --reset  # wipe demo entries first
"""
from __future__ import annotations

import random
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from assistant.management._seed_guard import ensure_dev_only

User = get_user_model()


class Command(BaseCommand):
    help = "Seed KYB submissions + OrderClaim test data for operator UI."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Удалить ранее заведённые demo-KYB/claim'ы перед посевом.")

    # ── KYB ────────────────────────────────────────────────────────────
    KYB_FIXTURES = [
        # (username_base, role, status, legal_name, inn, ogrn, director,
        #  rejection_reason)
        ("kyb_seller_metalcraft",  "seller",  "pending",
         "ООО «Металлкрафт»",      "7708123456", "1027700001234", "Иванов И.И.", ""),
        ("kyb_seller_partsline",   "seller",  "pending",
         "ИП Петров С.А.",         "504812345678", "317502400123456", "Петров С.А.", ""),
        ("kyb_seller_volgaparts",  "seller",  "pending",
         "ООО «Волга-Запчасть»",    "3666098765", "1093668001234", "Сидорова О.К.", ""),
        ("kyb_seller_hydrolux",    "seller",  "verified",
         "ООО «Гидролюкс»",        "7707654321", "1027700009876", "Кузнецов А.Б.", ""),
        ("kyb_seller_oldcompany",  "seller",  "rejected",
         "ООО «СтройТехРесурс»",   "5031000099", "1025003755555", "Смирнов В.Г.",
         "Указанный ИНН не найден в ЕГРЮЛ; ОГРН недействителен. Пожалуйста, проверьте данные."),
        ("kyb_buyer_mining",       "buyer",   "pending",
         "ООО «РудаДобыча»",       "8602099887", "1058600003366", "Орлов Д.М.", ""),
    ]

    # ── Claims ──────────────────────────────────────────────────────────
    CLAIM_FIXTURES = [
        # (kind, status, title, description, resolution_kind, refund_amount, age_days, rejection_reason)
        ("defect",           "open",
         "Гидроцилиндр течёт по штоку",
         "При первой опрессовке после установки обнаружена утечка масла из-под сальника штока. Гарантийное обращение.",
         "none",            "0",        1,  ""),
        ("wrong_part",       "in_review",
         "Прислали втулку другого диаметра",
         "Заказано 14Y-22-37470 (Komatsu OEM), приехала маркировка 14Y-22-37460 — на 0.8мм больше. Не садится в посадочное место.",
         "repair",          "0",        3,  ""),
        ("missing",          "approved",
         "Не пришла часть позиций по упаковочному листу",
         "В упаковке отсутствует 3 шт. шайб 02/202057. По packing list они должны быть. Просим донабрать.",
         "reproduce",       "0",        5,  ""),
        ("damage",           "corrective_actions",
         "Повреждение коробки при доставке",
         "Угол коробки промят, внутри 1 из 4 фильтров с поломанной резинкой уплотнителя. Остальные ОК.",
         "partial_refund",  "120.00",   8,  ""),
        ("late",             "financial_settlement",
         "Просрочка отгрузки 9 дней — упустили окно ремонта",
         "Поставщик подтвердил ready_to_ship через 22 дня вместо 14. Из-за этого простой техники +5 дней. Требуем частичный возврат.",
         "partial_refund",  "450.00",   14, ""),
        ("late",             "rejected",
         "Запрос на компенсацию за задержку",
         "ETA сместилось на 4 дня из-за шторма в Чёрном море. Просили компенсацию.",
         "none",            "0",        21, "Форс-мажор подтверждён страховой. Компенсация по контракту не предусмотрена."),
        ("defect",           "closed",
         "Брак сальника — заменён",
         "Резинка не подошла по диаметру. Поставщик прислал замену за свой счёт.",
         "repair",          "0",        30, ""),
        ("other",            "open",
         "Не хватает сертификата соответствия",
         "В пакете документов отсутствует ТР ТС 010/2011. Просим выслать копию.",
         "none",            "0",        2,  ""),
    ]

    def _h(self, msg):
        self.stdout.write(self.style.NOTICE(f"\n── {msg}"))

    def _ok(self, msg):
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def _warn(self, msg):
        self.stdout.write(self.style.WARNING(f"  ⚠ {msg}"))

    @transaction.atomic
    def handle(self, *args, **opts):
        ensure_dev_only(self)
        from marketplace.models import CompanyVerification, Order, OrderClaim, UserProfile

        if opts["reset"]:
            self._h("RESET — удаляем demo KYB/claims")
            # Удаляем KYB по нашим тестовым username'ам
            kyb_usernames = [f[0] for f in self.KYB_FIXTURES]
            kyb_users = User.objects.filter(username__in=kyb_usernames)
            CompanyVerification.objects.filter(user__in=kyb_users).delete()
            # Не удаляем самих юзеров — кто-то мог их использовать. Только сам KYB.
            self._ok(f"KYB removed for {kyb_users.count()} demo users")
            # Удаляем claim'ы по нашему маркеру [SEED]
            removed = OrderClaim.objects.filter(title__startswith="[SEED]").delete()[0]
            self._ok(f"Claims removed: {removed}")

        # ── KYB ───────────────────────────────────────────────────────
        self._h("KYB поставщиков — посев тестовых заявок")
        now = timezone.now()
        for (username, role, status, legal_name, inn, ogrn, director, reason) in self.KYB_FIXTURES:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@demo.consolidator.parts",
                          "first_name": legal_name.split('«')[-1].rstrip('»') if '«' in legal_name else legal_name},
            )
            if created:
                user.set_password("demo12345")
                user.save()
            # Профиль
            profile, _ = UserProfile.objects.get_or_create(
                user=user,
                defaults={"role": role, "company_name": legal_name},
            )
            if profile.role != role:
                profile.role = role; profile.save(update_fields=["role"])
            if not profile.company_name:
                profile.company_name = legal_name; profile.save(update_fields=["company_name"])

            # KYB
            kyb, _ = CompanyVerification.objects.update_or_create(
                user=user,
                defaults={
                    "status": status,
                    "legal_name": legal_name,
                    "inn": inn,
                    "ogrn": ogrn,
                    "kpp": (inn[:4] + "01001") if len(inn) >= 10 else "",
                    "legal_address": "г. Москва, ул. Промышленная, д. 17, оф. 401",
                    "bank_name": "ПАО Сбербанк",
                    "bank_account": "40702810" + str(random.randint(100000000, 999999999)),
                    "bik": "044525225",
                    "director_name": director,
                    "rejection_reason": reason,
                    "submitted_at": now,
                    "reviewed_at": now if status in ("verified", "rejected") else None,
                },
            )
            self._ok(f"KYB {username:30s} status={status:10s} INN={inn}")

        # ── Claims ────────────────────────────────────────────────────
        self._h("Рекламации — посев тестовых OrderClaim'ов")
        # Берём существующие заказы (предпочтительно с buyer=demo_buyer и далеко продвинутые)
        order_pool = list(Order.objects.filter(
            status__in=("delivered", "completed", "issuing", "transit_rf"),
        ).order_by("-id")[:20])
        if not order_pool:
            order_pool = list(Order.objects.order_by("-id")[:10])
        if not order_pool:
            self._warn("Нет заказов в БД — пропускаю claims. Прогоните `manage.py e2e_test` сначала.")
            return

        opener = None
        try:
            opener = User.objects.get(username="demo_buyer")
        except User.DoesNotExist:
            opener = order_pool[0].buyer

        for i, (kind, status, title, descr, resolution_kind, refund_str, age_days, reason) in enumerate(self.CLAIM_FIXTURES):
            order = order_pool[i % len(order_pool)]
            created_at = now - timezone.timedelta(days=age_days)
            seeded_title = f"[SEED] {title}"
            existing = OrderClaim.objects.filter(order=order, title=seeded_title).first()
            if existing:
                self._ok(f"Claim #{existing.id} (already seeded) — skip")
                continue
            claim = OrderClaim.objects.create(
                order=order,
                kind=kind,
                title=seeded_title,
                description=descr,
                status=status,
                resolution_kind=resolution_kind,
                refund_amount=Decimal(refund_str),
                rejection_reason=reason,
                opened_by=opener,
                closed_at=(now if status == "closed" else None),
            )
            # Backdate
            OrderClaim.objects.filter(id=claim.id).update(created_at=created_at)
            self._ok(f"Claim #{claim.id} ORD-{order.id} kind={kind:10s} status={status:24s} {age_days}d ago")

        self._h("Готово")
        kyb_count = CompanyVerification.objects.filter(status="pending").count()
        kyb_total = CompanyVerification.objects.count()
        claim_open = OrderClaim.objects.filter(
            status__in=("open", "in_review", "corrective_actions", "financial_settlement"),
        ).count()
        self.stdout.write(self.style.SUCCESS(
            f"\n  KYB:    pending={kyb_count}  total={kyb_total}\n"
            f"  Claims: open/active={claim_open}  total={OrderClaim.objects.count()}\n"
            f"\nЗайди в /chat/ как demo_operator → 🛡 KYB поставщиков / 🧾 Рекламации.\n"
        ))
