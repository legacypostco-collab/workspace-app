"""Тестовые данные для chat-first кабинетов.

Создаёт/обновляет:
- Кошельки (Wallet) для demo_buyer и demo_seller с историей транзакций
- Заказы (Order) во ВСЕХ этапах pipeline (pending → completed),
  чтобы и track_order, и seller_pipeline было что показать
- Несколько RFQ в разных статусах
- Project'ы (для sidebar в /chat/)

Идемпотентно: можно запускать сколько угодно, не дублирует.

Использование:
    python manage.py seed_chat_demo
    python manage.py seed_chat_demo --reset   # очистить созданные seed-объекты
"""
from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

User = get_user_model()


# Маркер, по которому отличаем seed-данные от настоящих.
SEED_TAG = "[chat-demo]"


PIPELINE_STAGES = [
    # (status, payment_status, days_ago_created, sla_status)
    ("pending",        "awaiting_reserve",  0,  "on_track"),
    ("reserve_paid",   "reserve_paid",      1,  "on_track"),
    ("confirmed",      "reserve_paid",      3,  "on_track"),
    ("in_production",  "reserve_paid",      6,  "on_track"),
    ("ready_to_ship",  "paid",              9,  "on_track"),
    ("transit_abroad", "paid",              12, "on_track"),
    ("customs",        "paid",              16, "at_risk"),
    ("transit_rf",     "paid",              22, "on_track"),
    ("issuing",        "paid",              26, "on_track"),
    ("delivered",      "paid",              28, "on_track"),
    ("completed",      "paid",              35, "on_track"),
]


class Command(BaseCommand):
    help = "Создаёт демо-данные для chat-first кабинетов"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Удалить ранее созданные seed-объекты")

    def handle(self, *args, **options):
        from marketplace.models import Order, OrderItem, OrderEvent, Part, RFQ, RFQItem
        from assistant.models import Wallet, WalletTx, Project

        if options["reset"]:
            n_orders = Order.objects.filter(logistics_meta__seed=SEED_TAG).delete()
            n_rfqs = RFQ.objects.filter(notes__contains=SEED_TAG).delete()
            n_proj = Project.objects.filter(description__contains=SEED_TAG).delete()
            n_tx = WalletTx.objects.filter(description__contains=SEED_TAG).delete()
            self.stdout.write(self.style.SUCCESS(
                f"Удалено: orders={n_orders}, rfqs={n_rfqs}, projects={n_proj}, txs={n_tx}"
            ))
            return

        # ── Demo users ──────────────────────────────────────────
        try:
            buyer = User.objects.get(username="demo_buyer")
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR("demo_buyer не найден — пропускаю"))
            return
        try:
            seller = User.objects.get(username="demo_seller")
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR("demo_seller не найден — пропускаю"))
            return

        # ── Каталог: гарантированно есть >= 6 товаров demo_seller ──
        seller_parts = list(
            Part.objects.filter(seller=seller, is_active=True).order_by("?")[:6]
        )
        if len(seller_parts) < 6:
            need = 6 - len(seller_parts)
            # Переназначим N случайных товаров (без seller'а или с другим
            # seller'ом, который не demo_buyer и не demo_seller) — на demo_seller
            candidates = list(
                Part.objects.filter(is_active=True)
                .exclude(seller=seller)
                .exclude(seller=buyer)
                .order_by("?")[:need]
            )
            for p in candidates:
                p.seller = seller
                p.save(update_fields=["seller"])
                seller_parts.append(p)
            if candidates:
                self.stdout.write(self.style.SUCCESS(
                    f"Переназначил {len(candidates)} товаров на demo_seller"
                ))

        if not seller_parts:
            self.stdout.write(self.style.ERROR("Нет товаров в каталоге, abort"))
            return

        random.shuffle(seller_parts)
        now = timezone.now()

        with transaction.atomic():
            # ── Кошельки ────────────────────────────────────────
            for u, seed_amount in [(buyer, Decimal("75000")), (seller, Decimal("12000"))]:
                w, created = Wallet.objects.get_or_create(user=u)
                if created or w.balance < seed_amount / 2:
                    w.balance = seed_amount
                    w.save(update_fields=["balance", "updated_at"])
                    WalletTx.objects.create(
                        wallet=w, kind="topup", amount=seed_amount,
                        description=f"Демо-депозит {SEED_TAG}",
                        balance_after=w.balance,
                    )

            # ── Проекты у buyer ────────────────────────────────
            project_specs = [
                ("EuroChem Kovdor",     "EUROCHEM",  "ЕвроХим — Ковдорский ГОК", "purple"),
                ("SUEK Borodino",       "SUEK-BOR",  "СУЭК — Бородинский разрез", "blue"),
                ("Polyus Olimpiada",    "POLYUS-OL", "Полюс — Олимпиада",         "orange"),
                ("Norilsk Q2 procurement", "NN-Q2",  "Норникель — закуп Q2",      "green"),
            ]
            for name, code, customer, dot in project_specs:
                Project.objects.get_or_create(
                    owner=buyer, name=name,
                    defaults={
                        "code": code,
                        "customer": customer,
                        "description": f"Демо-проект {SEED_TAG}",
                        "dot_color": dot,
                        "tags": ["demo"],
                    },
                )

            # ── Заказы по pipeline ─────────────────────────────
            created_orders = 0
            for status, payment_status, days_ago, sla in PIPELINE_STAGES:
                if Order.objects.filter(
                    buyer=buyer, status=status, logistics_meta__seed=SEED_TAG
                ).exists():
                    continue  # уже есть

                parts_for_order = random.sample(seller_parts, k=min(3, len(seller_parts)))
                total = Decimal("0")
                for p in parts_for_order:
                    if p.price:
                        total += Decimal(str(p.price))
                if total <= 0:
                    total = Decimal("1500")
                reserve_pct = Decimal("10.00")
                reserve_amount = (total * reserve_pct / Decimal("100")).quantize(Decimal("0.01"))

                created_at = now - timedelta(days=days_ago)
                order = Order(
                    customer_name=buyer.get_full_name() or "Demo Buyer",
                    customer_email=buyer.email or "demo_buyer@chat.local",
                    customer_phone="+7 999 000 0000",
                    delivery_address="Россия, Москва, склад demo",
                    buyer=buyer,
                    status=status,
                    payment_status=payment_status,
                    payment_scheme="simple",
                    reserve_percent=reserve_pct,
                    reserve_amount=reserve_amount,
                    sla_status=sla,
                    total_amount=total,
                    logistics_meta={"seed": SEED_TAG, "stage": status},
                )
                order.save()
                # Чтобы created_at был в прошлом, переписываем явно (auto_now_add иначе)
                Order.objects.filter(pk=order.pk).update(created_at=created_at)

                if payment_status in ("reserve_paid", "mid_paid", "customs_paid", "paid"):
                    order.reserve_paid_at = created_at + timedelta(hours=4)
                if payment_status == "paid":
                    order.final_paid_at = created_at + timedelta(days=8)
                order.save(update_fields=["reserve_paid_at", "final_paid_at"])

                for p in parts_for_order:
                    OrderItem.objects.create(
                        order=order, part=p,
                        quantity=random.choice([1, 2, 3, 5]),
                        unit_price=p.price or Decimal("500"),
                    )

                # Лог событий
                OrderEvent.objects.create(
                    order=order, event_type="order_created", source="buyer",
                    actor=buyer, meta={"items": len(parts_for_order)},
                )
                if payment_status != "awaiting_reserve":
                    OrderEvent.objects.create(
                        order=order, event_type="reserve_paid", source="buyer",
                        actor=buyer, meta={"amount": float(reserve_amount)},
                    )
                if payment_status == "paid":
                    OrderEvent.objects.create(
                        order=order, event_type="final_payment_paid", source="buyer",
                        actor=buyer, meta={"amount": float(total - reserve_amount)},
                    )

                # WalletTx — для оплаченных
                bw = Wallet.for_user(buyer)
                if payment_status != "awaiting_reserve":
                    WalletTx.objects.create(
                        wallet=bw, kind="debit", amount=reserve_amount,
                        description=f"Резерв 10% по заказу #{order.id} {SEED_TAG}",
                        order_id=order.id, balance_after=bw.balance,
                    )
                if payment_status == "paid":
                    final_amount = (total - reserve_amount).quantize(Decimal("0.01"))
                    WalletTx.objects.create(
                        wallet=bw, kind="debit", amount=final_amount,
                        description=f"Остаток 90% по заказу #{order.id} {SEED_TAG}",
                        order_id=order.id, balance_after=bw.balance,
                    )

                created_orders += 1

            # ── RFQ в разных статусах ──────────────────────────
            rfq_specs = [
                ("new",       "Гидроцилиндры — поиск аналогов",     2),
                ("processing","Тормозные колодки CAT — срочно",      4),
                ("matched",   "Запчасти для Komatsu PC450",          7),
                ("declined",  "Эксклюзивные фильтры Donaldson",     14),
                ("closed",    "Зимняя кампания — лента конвейерная", 28),
            ]
            for status, q, days_ago in rfq_specs:
                if RFQ.objects.filter(
                    created_by=buyer, notes__contains=SEED_TAG, status=status
                ).exists():
                    continue
                rfq = RFQ.objects.create(
                    created_by=buyer,
                    customer_name=buyer.get_full_name() or "Demo Buyer",
                    customer_email=buyer.email or "demo_buyer@chat.local",
                    company_name="Demo Company",
                    mode="semi", urgency="standard", status=status,
                    notes=f"{q} {SEED_TAG}",
                )
                # Старим created_at
                RFQ.objects.filter(pk=rfq.pk).update(
                    created_at=now - timedelta(days=days_ago)
                )
                # Несколько позиций
                for p in random.sample(seller_parts, k=min(2, len(seller_parts))):
                    RFQItem.objects.create(
                        rfq=rfq,
                        query=p.oem_number,
                        quantity=random.choice([1, 2, 5, 10]),
                        matched_part=p,
                        state="matched",
                    )

        # ── Отчёт ──────────────────────────────────────────────
        bw = Wallet.for_user(buyer)
        sw = Wallet.for_user(seller)
        self.stdout.write(self.style.SUCCESS(
            f"✓ Готово.\n"
            f"  • demo_buyer balance: ${bw.balance:,.2f}\n"
            f"  • demo_seller balance: ${sw.balance:,.2f}\n"
            f"  • Всего заказов у buyer: {Order.objects.filter(buyer=buyer).count()}\n"
            f"  • Заказов на pipeline (seed): "
            f"{Order.objects.filter(buyer=buyer, logistics_meta__seed=SEED_TAG).count()}\n"
            f"  • RFQ у buyer: {RFQ.objects.filter(created_by=buyer).count()}\n"
            f"  • Проектов у buyer: {Project.objects.filter(owner=buyer).count()}\n"
            f"\nКоманда идемпотентна — можно запускать снова.\n"
            f"Чтобы откатить: python manage.py seed_chat_demo --reset"
        ))
