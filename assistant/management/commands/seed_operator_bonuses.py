"""Заполнить OperatorBonusLine для существующих закрытых заказов (demo).

Назначает demo_operator на все заказы без assigned_operator и создаёт
бонусные строки по правилам 0.4 / 0.5 / 0.7 % с разными статусами:
  • Старые сделки (>14 дней) → status=released, зачислены в Wallet
  • Свежие (<14 дней)         → status=pending, ждут release
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from assistant.models import Wallet, WalletTx
from assistant.operator_bonus import compute_bonus_amount
from marketplace.models import Order, OperatorBonusLine

User = get_user_model()


class Command(BaseCommand):
    help = "Seed demo operator bonus lines"

    def handle(self, *args, **options):
        op = User.objects.filter(username="demo_operator").first()
        if not op:
            self.stdout.write(self.style.ERROR("demo_operator not found — создайте сначала"))
            return

        # Назначим всем delivered/completed заказам без оператора demo_operator
        unassigned = Order.objects.filter(
            assigned_operator__isnull=True,
        )
        unassigned.update(assigned_operator=op)
        self.stdout.write(f"Assigned demo_operator to {unassigned.count()} orders")

        now = timezone.now()
        created = 0
        released_sum = Decimal("0")
        pending_sum = Decimal("0")

        # Demo: берём все заказы с оплаченным резервом (есть commitment) — не только delivered.
        # Pending для in-progress, released для delivered/completed.
        active_payment = ("reserve_paid", "mid_paid", "customs_paid", "paid")
        candidates = Order.objects.filter(
            payment_status__in=active_payment,
            assigned_operator=op,
        ).exclude(id__in=OperatorBonusLine.objects.values_list("order_id", flat=True))

        for o in candidates:
            basis = random.choice(["FOB", "CIP", "DDP"])  # для разнообразия в демо
            base = float(o.total_amount or 0)
            if base <= 0:
                continue
            rate, amount = compute_bonus_amount(base, basis)
            age_days = (now - o.created_at).days if o.created_at else 0
            # Released только если заказ доставлен И прошло 14 дней с создания
            is_closed = o.status in ("delivered", "completed") and o.payment_status == "paid"
            if is_closed and age_days > 14:
                status = "released"
                released_at = o.created_at + timedelta(days=14)
                released_sum += amount
            else:
                status = "pending"
                released_at = None
                pending_sum += amount
            OperatorBonusLine.objects.create(
                operator=op,
                order=o,
                basis=basis,
                base_amount=Decimal(str(base)),
                rate_pct=rate,
                amount=amount,
                status=status,
                release_at=(o.created_at + timedelta(days=14)) if o.created_at else None,
                released_at=released_at,
                note=f"DEMO seed bonus ({basis} {rate}%)",
            )
            created += 1

        # Зачислим released-суммы в Wallet оператора + транзакции
        if released_sum > 0:
            wallet = Wallet.for_user(op)
            wallet.balance = (wallet.balance or Decimal("0")) + released_sum
            wallet.save(update_fields=["balance"])
            WalletTx.objects.create(
                wallet=wallet,
                amount=released_sum,
                kind="escrow_release",
                description=f"DEMO: суммарное зачисление бонусов за закрытые сделки",
                balance_after=wallet.balance,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Создано {created} бонусных строк · released: ${released_sum:,.2f} · pending: ${pending_sum:,.2f}"
        ))
