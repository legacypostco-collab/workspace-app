"""Комплекты тест-аккаунтов: на каждого тестера — 6 учёток.

Схема (на тестера tNN):
    tNN_buyer_full     — покупатель с богатыми данными (депозит, заказы, RFQ, рекламации)
    tNN_buyer_empty    — чистый покупатель (только депозит)
    tNN_seller_full    — продавец с каталогом/складом/рейтингом
    tNN_seller_empty   — чистый продавец
    tNN_operator_full  — оператор (staff)
    tNN_operator_empty — оператор (staff)

У всех 6 аккаунтов одного тестера — ОДИН пароль (удобно раздавать).

Usage:
    python manage.py seed_tester_sets --testers 10
    python manage.py seed_tester_sets --testers 10 --purge-old     # снести legacy buyer_N/seller_N
    python manage.py seed_tester_sets --testers 10 --reset          # пересоздать t* набор
    python manage.py seed_tester_sets --testers 10 --force-prod     # разрешить запуск при DEBUG=False

Печатает таблицу учёток в stdout + CSV (--out). Идемпотентно.
"""
from __future__ import annotations

import csv
import random
import secrets
import string
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

User = get_user_model()

ROLES = ("buyer", "seller", "operator")
VARIANTS = ("full", "empty")


def _gen_password(length: int = 10) -> str:
    alphabet = "".join(c for c in (string.ascii_letters + string.digits) if c not in "lIO0")
    return "".join(secrets.choice(alphabet) for _ in range(length))


class Command(BaseCommand):
    help = "Seed per-tester account sets (full+empty × buyer/seller/operator)."

    def add_arguments(self, parser):
        parser.add_argument("--testers", type=int, default=10)
        parser.add_argument("--prefix", default="t", help="t → t01.._buyer_full …")
        parser.add_argument("--purge-old", action="store_true",
                            help="Удалить legacy buyer_1..N / seller_1..N + их данные")
        parser.add_argument("--reset", action="store_true",
                            help="Удалить ранее созданные t* комплекты перед посевом")
        parser.add_argument("--force-prod", action="store_true",
                            help="Разрешить запуск при DEBUG=False (прод)")
        parser.add_argument("--fixed", action="store_true",
                            help="Детерминированные пароли TestNNCons26 (вместо случайных)")
        parser.add_argument("--out", default="tester_sets.csv")

    @transaction.atomic
    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["force_prod"]:
            raise CommandError("DEBUG=False. Для прода добавь --force-prod (осознанный посев тест-данных).")

        from assistant.models import Wallet
        from marketplace.models import (
            Brand, Category, Order, OrderClaim, OrderEvent, OrderItem,
            Part, RFQ, RFQItem, UserProfile,
        )

        now = timezone.now()
        prefix = opts["prefix"]
        N = opts["testers"]

        # ── 0. purge legacy buyer_N / seller_N ─────────────────
        if opts["purge_old"]:
            import re
            legacy = [u for u in User.objects.filter(username__regex=r"^(buyer|seller)_[0-9]+$")]
            ln = len(legacy)
            for u in legacy:
                Order.objects.filter(buyer=u).delete()
                RFQ.objects.filter(created_by=u).delete()
                Part.objects.filter(seller=u).delete()
                u.delete()
            self.stdout.write(self.style.WARNING(f"⚠ Удалено legacy-аккаунтов: {ln} (+ их заказы/RFQ/товары)"))

        # ── 0b. reset previous t* sets ─────────────────────────
        if opts["reset"]:
            old = User.objects.filter(username__regex=rf"^{prefix}[0-9]+_(buyer|seller|operator)_(full|empty)$")
            on = old.count()
            for u in old:
                Order.objects.filter(buyer=u).delete()
                RFQ.objects.filter(created_by=u).delete()
                Part.objects.filter(seller=u).delete()
            old.delete()
            self.stdout.write(self.style.WARNING(f"⚠ Сброшено прежних t*-аккаунтов: {on}"))

        # ── reference data (parts pool, brands, cats) ──────────
        # NB: без order_by("?") — на 916K товаров PostgreSQL делал бы полную
        # сортировку (минуты). Берём первые 500 id и сэмплируем в Python.
        _pool_ids = list(Part.objects.filter(is_active=True).values_list("id", flat=True)[:500])
        if _pool_ids:
            _take = random.sample(_pool_ids, min(200, len(_pool_ids)))
            parts_pool = list(Part.objects.filter(id__in=_take))
        else:
            parts_pool = []
        brands = list(Brand.objects.all()[:12]) or [
            Brand.objects.get_or_create(name=b, defaults={"slug": slugify(b), "region": "europe"})[0]
            for b in ("Caterpillar", "Komatsu", "Volvo", "Liebherr", "Hitachi")
        ]
        cats = list(Category.objects.all()[:12]) or [
            Category.objects.get_or_create(name=c, defaults={"slug": slugify(c)})[0]
            for c in ("Engine", "Hydraulic", "Filters", "Electrical", "Cooling")
        ]

        ORDER_STATUSES = ["reserve_paid", "confirmed", "in_production",
                          "ready_to_ship", "shipped", "delivered", "completed"]
        PAY = ["reserve_paid", "mid_paid", "paid"]
        SLA = ["on_track", "on_track", "at_risk", "breached"]
        RFQ_ST = ["new", "quoted", "needs_review"]
        CLAIM_TITLES = ["Несоответствие OEM-номера", "Повреждение при транспортировке",
                        "Неполная комплектация", "Просрочка поставки > 14 дней"]
        DELIV = ["Москва, ул. Промышленная 12", "Екатеринбург, Заводской пр. 45",
                 "Новосибирск, ул. Технопарковая 8", "Казань, ул. Машиностроителей 3"]

        def mkuser(username, role, full):
            u, _ = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@tester.consolidator.parts",
                          "date_joined": now - timedelta(days=random.randint(3, 120))},
            )
            u.is_staff = (role == "operator")
            u.save()
            tag = "FULL" if full else "EMPTY"
            prof_defaults = {"role": role, "company_name": f"{username} ({tag})"}
            if role == "seller" and full:
                prof_defaults.update({"external_score": Decimal("85"),
                                      "behavioral_score": Decimal("80")})
            profile, _ = UserProfile.objects.get_or_create(user=u, defaults=prof_defaults)
            if profile.role != role:
                profile.role = role
                profile.save(update_fields=["role"])
            # supplier_status для full-seller — «trusted», если поле есть
            if role == "seller" and full and hasattr(profile, "supplier_status"):
                profile.supplier_status = "trusted"
                profile.save(update_fields=["supplier_status"])
            return u

        def seed_buyer_full(u):
            w, _ = Wallet.objects.get_or_create(user=u)
            w.balance = Decimal("50000")
            w.save(update_fields=["balance"])
            for _ in range(random.randint(5, 7)):
                days = random.randint(2, 80)
                total = Decimal(str(random.randint(1500, 80000)))
                st = random.choice(ORDER_STATUSES)
                sla = random.choice(SLA)
                o = Order.objects.create(
                    customer_name=u.username, customer_email=u.email,
                    delivery_address=random.choice(DELIV), buyer=u, status=st,
                    total_amount=total, payment_status=random.choice(PAY), sla_status=sla,
                    sla_breaches_count=random.randint(1, 3) if sla == "breached" else 0,
                    invoice_number=f"INV-2026-{random.randint(1000,9999)}",
                    reserve_amount=total * Decimal("0.10"),
                    logistics_cost=Decimal(str(random.randint(100, 4000))),
                    ship_deadline=now + timedelta(days=random.randint(3, 30)),
                )
                Order.objects.filter(id=o.id).update(created_at=now - timedelta(days=days))
                for _ in range(random.randint(1, 3)):
                    if parts_pool:
                        p = random.choice(parts_pool)
                        OrderItem.objects.create(order=o, part=p, quantity=random.randint(1, 8),
                                                 unit_price=p.price or Decimal("100"))
                OrderEvent.objects.create(order=o, event_type="order_created", source="system",
                                          meta={"comment": "Заказ создан (тест-данные)"})
                if sla == "breached" and random.random() > 0.5:
                    OrderClaim.objects.create(order=o, title=random.choice(CLAIM_TITLES),
                                              description="Тест-рекламация.", status="open", opened_by=u)
            for _ in range(random.randint(2, 4)):
                rfq = RFQ.objects.create(created_by=u, customer_name=u.username,
                                         customer_email=u.email, status=random.choice(RFQ_ST),
                                         notes="Тестовый запрос")
                RFQ.objects.filter(id=rfq.id).update(created_at=now - timedelta(days=random.randint(0, 40)))
                for _ in range(random.randint(1, 3)):
                    p = random.choice(parts_pool) if parts_pool else None
                    RFQItem.objects.create(rfq=rfq,
                                           query=(p.oem_number if p else "OEM-TEST-123"),
                                           quantity=random.randint(1, 15),
                                           matched_part=p)

        def seed_buyer_empty(u):
            w, _ = Wallet.objects.get_or_create(user=u)
            w.balance = Decimal("10000")
            w.save(update_fields=["balance"])

        def seed_seller_full(u):
            for idx in range(random.randint(5, 7)):
                brand = random.choice(brands)
                cat = random.choice(cats)
                oem = f"{random.choice('ABCDEFGH')}{random.randint(100,999)}-{random.randint(1000,9999)}"
                Part.objects.get_or_create(
                    oem_number=oem,
                    defaults={"title": f"Test Part {idx+1} ({brand.name})",
                              "slug": slugify(f"{u.username}-{oem}"),
                              "brand": brand, "category": cat, "seller": u,
                              "price": Decimal(str(random.randint(50, 6000))),
                              "currency": "USD", "condition": "oem",
                              "stock_quantity": random.randint(1, 40), "is_active": True})

        rows = []
        for i in range(1, N + 1):
            tp = f"{prefix}{i:02d}"
            pw = f"Test{i:02d}Cons26" if opts["fixed"] else _gen_password()
            accts = []
            for role in ROLES:
                for variant in VARIANTS:
                    username = f"{tp}_{role}_{variant}"
                    full = (variant == "full")
                    u = mkuser(username, role, full)
                    u.set_password(pw)
                    u.save(update_fields=["password"])
                    if full and role == "buyer":
                        seed_buyer_full(u)
                    elif not full and role == "buyer":
                        seed_buyer_empty(u)
                    elif full and role == "seller":
                        seed_seller_full(u)
                    accts.append(username)
            rows.append({"tester": tp, "password": pw, "accounts": accts})

        # CSV
        out_path = Path(opts["out"])
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["tester", "password", "username", "role", "variant"])
            for r in rows:
                for a in r["accounts"]:
                    _, role, variant = a.rsplit("_", 2)
                    w.writerow([r["tester"], r["password"], a, role, variant])

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Создано {len(rows)} комплектов × 6 = {len(rows)*6} аккаунтов. CSV → {out_path.resolve()}\n"))
        self.stdout.write("─" * 78)
        self.stdout.write(f"{'tester':7s} {'password':12s} accounts")
        self.stdout.write("─" * 78)
        for r in rows:
            self.stdout.write(f"{r['tester']:7s} {r['password']:12s} {tp_join(r['accounts'])}")
        self.stdout.write("─" * 78)
        self.stdout.write(self.style.WARNING("⚠ Один пароль на 6 аккаунтов тестера. Раздай таблицу/CSV."))


def tp_join(accts):
    return ", ".join(a.split("_", 1)[1] for a in accts)
