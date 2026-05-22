"""Cron-команда: проверяет SLA-нарушения и шлёт alerts оператору.

Запускается раз в 5 минут (Celery beat / cron):
    python manage.py sla_check

Что проверяется:
  • SEMI RFQ > 15 минут без approve → notify_operator_alert(sla_semi_overdue)
  • MANUAL RFQ > 48 часов без compose_kp → notify_operator_alert(sla_manual_overdue)
  • Order.sla_status = 'breached' и без свежей нотификации → alert
  • Открытые OrderClaim в статусе 'open' > 1 час → alert

Идемпотентность: пишем флаг в RFQ.notes / Order.notes («SLA_ESCALATED:
<event>:<timestamp>»), чтобы не алертить повторно.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from assistant.order_events import notify_operator_alert

_ESCALATED_MARKER = "SLA_ESCALATED:"


class Command(BaseCommand):
    help = "Проверка SLA нарушений и эскалация оператору."

    def handle(self, *args, **opts):
        from marketplace.models import RFQ, Order, OrderClaim
        now = timezone.now()
        sent = 0

        # 1. SEMI RFQ просрочены 15 мин
        semi = RFQ.objects.filter(
            mode="semi", status="new",
            created_at__lt=now - timedelta(minutes=15),
            created_at__gt=now - timedelta(hours=24),  # не алертим давние
        )
        for rfq in semi:
            if _ESCALATED_MARKER + "sla_semi" in (rfq.notes or ""):
                continue
            notify_operator_alert(rfq=rfq, event="sla_semi_overdue")
            rfq.notes = (rfq.notes or "")[:4500] + (
                f" | {_ESCALATED_MARKER}sla_semi:{now.isoformat()}"
            )
            rfq.save(update_fields=["notes"])
            sent += 1
            self.stdout.write(f"  • SEMI RFQ #{rfq.id} → operator alert")

        # 2. MANUAL RFQ > 48ч
        manual = RFQ.objects.filter(
            mode__in=("manual", "manual_oem"), status="new",
            created_at__lt=now - timedelta(hours=48),
        )
        for rfq in manual:
            if _ESCALATED_MARKER + "sla_manual" in (rfq.notes or ""):
                continue
            notify_operator_alert(rfq=rfq, event="sla_manual_overdue")
            rfq.notes = (rfq.notes or "")[:4500] + (
                f" | {_ESCALATED_MARKER}sla_manual:{now.isoformat()}"
            )
            rfq.save(update_fields=["notes"])
            sent += 1
            self.stdout.write(f"  • MANUAL RFQ #{rfq.id} → operator alert")

        # 3. Order.sla_status = 'breached'
        # Маркер эскалации храним в logistics_meta['_sla_escalated'] —
        # у Order нет text-поля notes.
        breached_orders = Order.objects.filter(sla_status="breached")
        for o in breached_orders:
            meta = dict(o.logistics_meta or {})
            if meta.get("_sla_escalated"):
                continue
            notify_operator_alert(order=o, event="sla_breach")
            meta["_sla_escalated"] = now.isoformat()
            o.logistics_meta = meta
            o.save(update_fields=["logistics_meta"])
            sent += 1
            self.stdout.write(f"  • ORD-{o.id} SLA breach → operator alert")

        # 4. Open claims > 1 час
        open_claims = OrderClaim.objects.filter(
            status="open",
            created_at__lt=now - timedelta(hours=1),
        )
        for c in open_claims:
            # Маркер в description (TextField) — нет отдельного meta поля
            if _ESCALATED_MARKER + "claim" in (c.description or ""):
                continue
            notify_operator_alert(claim=c, order=c.order, event="claim_opened")
            c.description = (c.description or "")[:4500] + (
                f"\n[{_ESCALATED_MARKER}claim:{now.isoformat()}]"
            )
            c.save(update_fields=["description"])
            sent += 1
            self.stdout.write(f"  • Claim #{c.id} → operator alert")

        self.stdout.write(self.style.SUCCESS(
            f"\nSLA check done: {sent} новых эскалаций отправлено."
        ))
