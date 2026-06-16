"""Backfill ленты важных событий (ActivityEvent) из реальной истории.

Логирование ActivityEvent добавлено позже, поэтому старые заказы/RFQ/загрузки
прайса в ленту не попали — из-за чего дашборд (считает из реальных таблиц) и
лента (читает ActivityEvent) расходились. Эта команда создаёт недостающие
события из Order / RFQ / PricelistImport.

Идемпотентно: не дублирует уже залогированные (dedup по id сущности в meta).
IP у исторических — пустой («—», трекинга тогда не было), время — исходное.

    python manage.py backfill_activity_events
    python manage.py backfill_activity_events --orders-days 90   # ограничить заказы
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from marketplace.models import ActivityEvent, Order, RFQ, PricelistImport


class Command(BaseCommand):
    help = "Заполняет ActivityEvent из истории Order/RFQ/PricelistImport (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--orders-days", type=int, default=0,
                            help="Ограничить заказы последними N днями (0 = все).")

    def _seen_ids(self, key: str) -> set:
        """id сущностей, уже представленных в ленте (по meta[key])."""
        out = set()
        for meta in ActivityEvent.objects.values_list("meta", flat=True).iterator():
            v = (meta or {}).get(key)
            if v is not None:
                out.add(v)
        return out

    def _role_of(self, u) -> str:
        if not u:
            return ""
        prof = getattr(u, "userprofile", None) or getattr(u, "profile", None)
        return (getattr(prof, "role", "") or "") if prof else ""

    def _create(self, *, kind, actor, title, meta, when):
        ev = ActivityEvent.objects.create(
            kind=kind, actor=actor, actor_role=self._role_of(actor),
            ip="", title=title[:255], meta=meta,
        )
        # created_at — auto_now_add, перебиваем на исходное время через update.
        ActivityEvent.objects.filter(pk=ev.pk).update(created_at=when)

    def handle(self, *args, **opts):
        created = {"order": 0, "rfq": 0, "pricelist": 0}

        # ── Загрузки прайса ──
        seen = self._seen_ids("import_id")
        for imp in (PricelistImport.objects.select_related("seller")
                    .order_by("created_at").iterator()):
            if imp.id in seen:
                continue
            self._create(
                kind="pricelist", actor=imp.seller,
                title=f"Загрузка прайса #{imp.id} · {imp.total_rows} строк · "
                      f"{imp.filename or 'файл'}",
                meta={"import_id": imp.id, "n_rows": imp.total_rows,
                      "filename": imp.filename or "",
                      "seller": getattr(imp.seller, "username", ""),
                      "backfilled": True},
                when=imp.created_at)
            created["pricelist"] += 1

        # ── RFQ ──
        seen = self._seen_ids("rfq_id")
        for rfq in (RFQ.objects.select_related("created_by")
                    .order_by("created_at").iterator()):
            if rfq.id in seen:
                continue
            n_items = rfq.items.count()
            self._create(
                kind="rfq", actor=rfq.created_by,
                title=f"RFQ #{rfq.id} · {n_items} поз · {rfq.mode}",
                meta={"rfq_id": rfq.id, "n_items": n_items,
                      "mode": rfq.mode, "backfilled": True},
                when=rfq.created_at)
            created["rfq"] += 1

        # ── Заказы ──
        seen = self._seen_ids("order_id")
        oqs = Order.objects.select_related("buyer").order_by("created_at")
        if opts["orders_days"]:
            oqs = oqs.filter(created_at__gte=timezone.now() - timedelta(days=opts["orders_days"]))
        for o in oqs.iterator():
            if o.id in seen:
                continue
            n_items = o.items.count()
            self._create(
                kind="order", actor=o.buyer,
                title=f"Заказ #{o.id} · {n_items} поз · ${float(o.total_amount or 0):,.0f}",
                meta={"order_id": o.id, "n_items": n_items,
                      "amount": float(o.total_amount or 0), "currency": "USD",
                      "backfilled": True},
                when=o.created_at)
            created["order"] += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано событий: прайсы={created['pricelist']}, "
            f"RFQ={created['rfq']}, заказы={created['order']}."
        ))
