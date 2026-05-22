"""Seed in-flight orders showing diverse logistics routes for the operator
logistics dashboard.

Берёт реальные заказы (или создаёт лёгкие fake-orders на основе demo_buyer)
и проставляет им разные:
  • status   — transit_abroad | customs | transit_rf | issuing
  • shipping_mode — sea / air / auto / rail
  • logistics_provider — COSCO / DHL / Maersk / FESCO / RZD-Logistics / Atasu
  • logistics_meta.origin       — CN / DE / UAE / KZ / TR / IT
  • logistics_meta.customs.country — RU / KZ / BY / AM
  • logistics_meta.tracking_number
  • sla_status — on_track / at_risk / breached

Цель: на /chat/ → 🚚 Логистика оператор видит реальные маршруты:
«CN→RU 🚢 COSCO», «DE→RU 🚚 DHL», «UAE→RU ✈️ Emirates», и т.д.

Usage:
    python manage.py seed_logistics_routes        # idempotent top-up
    python manage.py seed_logistics_routes --reset  # сначала откатить
"""
from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from assistant.management._seed_guard import ensure_dev_only

User = get_user_model()


# (status, shipping_mode, provider, origin, dest, tracking, sla, days_ago)
# days_ago = когда заказ был «создан» → влияет на «дней на этапе» в логистике
ROUTES = [
    ("transit_abroad", "sea",  "COSCO Shipping",      "CN",  "RU", "COSU-7841-22091", "on_track", 12),
    ("transit_abroad", "sea",  "Maersk Line",          "TR",  "RU", "MAEU-3392-44178", "at_risk",  18),
    ("transit_abroad", "air",  "Emirates SkyCargo",    "UAE", "RU", "EK-176-AWB-9921", "on_track", 4),
    ("transit_abroad", "auto", "DHL Eurasia",          "DE",  "RU", "DHL-EU-5598-22",   "at_risk",  9),
    ("customs",        "sea",  "FESCO",                "CN",  "RU", "FESCO-VLD-2241",   "on_track", 22),
    ("customs",        "auto", "Trasko",               "PL",  "BY", "TRSK-WAW-1107",    "breached", 31),
    ("transit_rf",     "rail", "RZD-Logistics",        "CN",  "RU", "RZD-9281-MSK",     "on_track", 25),
    ("transit_rf",     "auto", "Atasu Logistics",      "KZ",  "RU", "ATS-Almaty-7733",  "on_track", 15),
    ("issuing",        "auto", "СДЭК",                  "CN",  "RU", "CDEK-MSK-19281",   "on_track", 27),
    ("issuing",        "air",  "Cathay Cargo",          "HK",  "RU", "CX-AWB-160-9933",  "at_risk",  20),
    ("transit_abroad", "sea",  "MSC Mediterranean",    "IT",  "AM", "MSCU-IT-4471",     "on_track", 7),
    ("customs",        "rail", "Kazakhstan Temir Zholy","KZ", "RU", "KTZ-3399-NUR",     "on_track", 24),
]

MODE_TO_TARIFF = {
    # rough days for each mode (визуально только)
    "sea":  35,
    "air":  6,
    "auto": 12,
    "rail": 22,
}


class Command(BaseCommand):
    help = "Seed in-flight orders with diverse logistics routes for operator dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Удалить все ранее засеянные [ROUTE] заказы перед посевом.")

    def _h(self, msg): self.stdout.write(self.style.NOTICE(f"\n── {msg}"))
    def _ok(self, msg): self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))
    def _warn(self, msg): self.stdout.write(self.style.WARNING(f"  ⚠ {msg}"))

    @transaction.atomic
    def handle(self, *args, **opts):
        ensure_dev_only(self)
        from marketplace.models import Order, OrderEvent, OrderItem, Part

        if opts["reset"]:
            self._h("RESET — удаляем ранее засеянные [ROUTE] заказы")
            removed = Order.objects.filter(customer_name__startswith="[ROUTE]").delete()[0]
            self._ok(f"Removed {removed} prior route orders")

        try:
            buyer = User.objects.get(username="demo_buyer")
        except User.DoesNotExist:
            self._warn("demo_buyer не найден — запусти `manage.py seed_chat_demo` сначала.")
            return

        # Найдём любой части для item'ов (нужен только для не-пустого Order)
        part = Part.objects.filter(availability_status="active").first()
        if not part:
            self._warn("Нет ни одной part в каталоге — пропуск.")
            return

        self._h(f"Logistics routes — посев {len(ROUTES)} активных маршрутов")
        now = timezone.now()

        for idx, (status, mode, provider, origin, dest, tracking, sla, days_ago) in enumerate(ROUTES):
            customer = f"[ROUTE] {origin}→{dest} {mode}"
            # Idempotency: skip если такой уже есть
            existing = Order.objects.filter(customer_name=customer).first()
            if existing:
                self._ok(f"order #{existing.id} {origin}→{dest} {mode} — already seeded, skip")
                continue

            base_amount = Decimal(random.choice([4500, 8200, 12_500, 24_900, 38_000, 67_400]))
            created = now - timedelta(days=days_ago)

            order = Order.objects.create(
                customer_name=customer,
                customer_email=buyer.email or "demo@consolidator.parts",
                customer_phone="+7 (000) 000-00-00",
                delivery_address=f"{dest} · г.Москва · ул.Промышленная, 17",
                buyer=buyer,
                status=status,
                payment_status="paid",  # все в пути → деньги уже в эскроу
                sla_status=sla,
                shipping_mode=mode,
                logistics_provider=provider,
                logistics_meta={
                    "origin": origin,
                    "origin_country": origin,
                    "tracking_number": tracking,
                    "customs": {"country": dest, "hs_code": "8413.50" if mode == "sea" else "8708.99"},
                    "estimated_days": MODE_TO_TARIFF.get(mode, 20),
                },
                logistics_cost=Decimal(str(round(float(base_amount) * 0.08, 2))),
                logistics_currency="USD",
                total_amount=base_amount,
                reserve_amount=base_amount * Decimal("0.10"),
            )
            # Backdate created_at (auto_now_add есть, обходим через update)
            Order.objects.filter(id=order.id).update(created_at=created)
            # Один OrderItem на заказ — для целостности
            OrderItem.objects.create(order=order, part=part, quantity=1,
                                      unit_price=base_amount)
            # ── Заполняем timeline событий чтобы трекинг показывал
            # осмысленную историю (от создания до текущего этапа). Время
            # каждого события "размазываем" по days_ago, чтобы в UI был
            # реалистичный хронологический порядок.
            stage_age = max(1, days_ago // 2)
            # Хронология: чем дальше в прошлое — тем раньше событие
            # Полный путь: created → reserve → confirmed → in_production →
            # ready_to_ship → shipped → transit_abroad → customs → transit_rf → issuing
            STATUS_SEQUENCE = [
                "reserve_paid", "confirmed", "in_production",
                "ready_to_ship", "transit_abroad", "customs",
                "transit_rf", "issuing", "delivered", "completed",
            ]
            try:
                stop_idx = STATUS_SEQUENCE.index(status)
            except ValueError:
                stop_idx = 0
            # Распределяем шаги равномерно между created (days_ago) и сейчас (stage_age назад)
            step_days = max(0.5, (days_ago - stage_age) / max(1, stop_idx + 1))
            events_seq = [
                ("order_created",        {"by": "buyer"},                                    days_ago),
                ("invoice_opened",       {"amount": float(base_amount * Decimal("0.10"))},  days_ago - 0.3),
                ("reserve_paid",         {"amount": float(base_amount * Decimal("0.10"))},  days_ago - 0.5),
            ]
            for i, st in enumerate(STATUS_SEQUENCE[:stop_idx + 1]):
                prev = STATUS_SEQUENCE[i - 1] if i > 0 else "pending"
                events_seq.append(("status_changed", {"from": prev, "to": st},
                                    days_ago - 0.5 - step_days * (i + 1)))
            # Финальная оплата перед отгрузкой
            if stop_idx >= STATUS_SEQUENCE.index("transit_abroad"):
                events_seq.append(("final_payment_paid",
                                    {"amount": float(base_amount * Decimal("0.90"))},
                                    days_ago - 0.5 - step_days * (STATUS_SEQUENCE.index("ready_to_ship") + 1.2)))
                events_seq.append(("tracking_updated",
                                    {"tracking_number": tracking, "carrier": provider},
                                    days_ago - 0.5 - step_days * (STATUS_SEQUENCE.index("transit_abroad") + 0.5)))
            # Документы (когда на таможне+)
            if stop_idx >= STATUS_SEQUENCE.index("customs"):
                events_seq.append(("document_uploaded",
                                    {"doc_type": "invoice", "title": "Коммерческий инвойс"},
                                    days_ago - 0.5 - step_days * (STATUS_SEQUENCE.index("customs") - 0.3)))
                events_seq.append(("document_uploaded",
                                    {"doc_type": "packing_list", "title": "Упаковочный лист"},
                                    days_ago - 0.5 - step_days * (STATUS_SEQUENCE.index("customs") - 0.2)))

            for event_type, meta, age in events_seq:
                ev = OrderEvent.objects.create(
                    order=order, event_type=event_type, actor=None, source="seed",
                    meta=meta,
                )
                # Backdate
                ts = now - timedelta(days=max(0.1, float(age)))
                OrderEvent.objects.filter(id=ev.id).update(created_at=ts)

            self._ok(f"order #{order.id} {origin:3s}→{dest:3s} {mode:4s} · {provider:24s} · "
                     f"sla={sla:9s} · ${base_amount:>7,.0f}")

        # Сводка
        from marketplace.models import Order as O2
        live = O2.objects.filter(status__in=("transit_abroad", "customs", "transit_rf", "issuing"))
        self._h("Готово")
        self.stdout.write(self.style.SUCCESS(
            f"\n  Активных маршрутов: {live.count()}\n"
            f"  По статусам: " + " · ".join(
                f"{s}={live.filter(status=s).count()}"
                for s in ("transit_abroad", "customs", "transit_rf", "issuing")
            ) + "\n"
            "  Войди как demo_operator → 🚚 Логистика — увидишь все маршруты.\n"
        ))
