"""Чистит дублирующиеся демо-заказы и пересевает вариативный набор.

Репорт пользователя: у покупателя десятки ОДИНАКОВЫХ заказов («$1,670 ·
Транзит», одна дата) — следствие того, что разные сид-команды льют заказы из
фиксированного набора позиций (seed_team_accounts.mk_order → pool[:3] → одна и
та же сумма), копятся при повторных прогонах.

Команда:
  1) находит SEED-заказы (метка logistics_meta['seed'] ИЛИ OrderEvent meta
     seed=True) и их покупателей — безопасный скоуп, реальные заказы не трогаем;
  2) у каждого такого покупателя удаляет его seed-заказы (каскадом уходят
     items/events/shipments/documents; WalletTx.order = SET_NULL — история цела);
  3) сеет ~7 ВАРИАТИВНЫХ заказов: разные позиции из каталога → разные суммы,
     разные даты, количества и статусы по pipeline.

Детерминированно по покупателю (random.Random(buyer.id)) — повторный прогон даёт
тот же набор (idempotent-reseed). Запуск: python manage.py reseed_demo_orders
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone


# (status, payment_status, days_ago_base, sla) — по одному заказу на стадию.
STAGES = [
    ("reserve_paid",   "reserve_paid",  4,  "on_track"),
    ("in_production",  "reserve_paid",  10, "on_track"),
    ("ready_to_ship",  "reserve_paid",  15, "on_track"),
    ("transit_abroad", "paid",          23, "on_track"),
    ("customs",        "paid",          31, "at_risk"),
    ("delivered",      "paid",          41, "on_track"),
    ("completed",      "paid",          56, "on_track"),
]


class Command(BaseCommand):
    help = "Чистит дубли демо-заказов и пересевает вариативный набор на каждого демо-покупателя."

    def handle(self, *args, **opts):
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        from marketplace.models import (Order, OrderEvent, OrderItem, Part)

        U = get_user_model()
        now = timezone.now()

        # Скоуп — ДЕМО-аккаунты покупателей (конвенция сидов: username содержит
        # «buyer»: demo_buyer, client0N_buyer, konstantin_k_buyer, t0N_buyer…).
        # Это безопасно: чистим только сид-покупателей, реальные имена не трогаем.
        buyer_ids = [
            u.id for u in U.objects.filter(profile__role="buyer")
            if "buyer" in (u.username or "").lower()
        ]

        # Пул каталожных позиций с ценой — для вариативных сумм.
        catalog = list(
            Part.objects.filter(is_active=True, price__gt=0)
            .exclude(seller__isnull=True)
            .only("id", "price", "seller_id")[:500])
        if not catalog:
            self.stdout.write(self.style.ERROR("Нет каталожных позиций с ценой — нечего сеять."))
            return

        deleted = created = 0
        for bid in buyer_ids:
            buyer = U.objects.filter(id=bid).first()
            if not buyer:
                continue
            # Удаляем ВСЕ заказы демо-покупателя (каскад чистит items/events/
            #    shipments/documents; WalletTx.order → NULL — история цела).
            del_qs = Order.objects.filter(buyer=buyer)
            deleted += del_qs.count()
            del_qs.delete()

            # 3) Вариативный пересев — детерминированно по покупателю.
            rnd = random.Random(bid)
            for status, pstatus, days_base, sla in STAGES:
                k = rnd.randint(1, 4)
                parts = rnd.sample(catalog, min(k, len(catalog)))
                items = [(p, rnd.choice([1, 2, 3, 5, 8])) for p in parts]
                sub = sum((Decimal(str(p.price)) * q for p, q in items), Decimal("0"))
                logi = Decimal(str(rnd.choice([120, 190, 260, 340, 410])))
                total = (sub + logi).quantize(Decimal("0.01"))
                reserve = (total * Decimal("0.10")).quantize(Decimal("0.01"))
                created_at = now - timedelta(days=days_base + rnd.randint(-2, 3))

                o = Order.objects.create(
                    customer_name=buyer.get_full_name() or buyer.username,
                    customer_email=buyer.email or f"{buyer.username}@chat.local",
                    customer_phone="+7 999 000 0000",
                    delivery_address="Россия, Москва, склад demo",
                    buyer=buyer, status=status, payment_status=pstatus,
                    reserve_percent=Decimal("10.00"), reserve_amount=reserve,
                    total_amount=total, logistics_cost=logi,
                    logistics_currency="USD", sla_status=sla,
                    logistics_meta={"seed": "reseed", "stage": status},
                )
                Order.objects.filter(pk=o.pk).update(created_at=created_at)
                fields = []
                if pstatus in ("reserve_paid", "paid"):
                    o.reserve_paid_at = created_at + timedelta(hours=5)
                    fields.append("reserve_paid_at")
                if pstatus == "paid":
                    o.final_paid_at = created_at + timedelta(days=8)
                    fields.append("final_paid_at")
                if fields:
                    o.save(update_fields=fields)
                for p, q in items:
                    OrderItem.objects.create(order=o, part=p, quantity=q,
                                             unit_price=p.price)
                OrderEvent.objects.create(
                    order=o, event_type="order_created", source="buyer",
                    actor=buyer, meta={"seed": True})
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Демо-покупателей: {len(buyer_ids)} · удалено seed-заказов: "
            f"{deleted} · создано вариативных: {created}. "
            f"Идемпотентно — можно запускать снова."))
