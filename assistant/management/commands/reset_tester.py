"""Сбрасывает все данные конкретного beta-тестера (заказы/RFQ/чаты/claim'ы),
оставляя сам аккаунт + пароль + начальный wallet-баланс.

Используется когда тестер «сломал» состояние и хочет начать заново.

Usage:
    python manage.py reset_tester beta07
    python manage.py reset_tester beta07 --keep-wallet
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = "Reset all transactional data for a specific tester (keeps account)."

    def add_arguments(self, parser):
        parser.add_argument("username", help="Username тестера (например beta07)")
        parser.add_argument("--keep-wallet", action="store_true",
                            help="Не пересоздавать Wallet (по умолчанию: balance → 10000)")

    @transaction.atomic
    def handle(self, *args, **opts):
        from assistant.models import Conversation, Wallet, WalletTx
        from marketplace.models import (
            RFQ,
            CompanyVerification,
            CompetitorOffer,
            Notification,
            Order,
            Quote,
        )

        username = opts["username"]
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"User '{username}' не найден")

        self.stdout.write(self.style.NOTICE(f"\n── Reset для {username} (id={user.id})"))

        counts = {}

        # 1. Чаты + сообщения (cascade)
        n = Conversation.objects.filter(user=user).delete()[0]
        counts["conversations+messages"] = n

        # 2. Заказы → cascade удалит OrderItem/OrderEvent/OrderClaim/OrderDocument
        n = Order.objects.filter(buyer=user).delete()[0]
        counts["orders+items+events+claims+docs"] = n

        # 3. RFQ → cascade удалит RFQItem/Quote/QuoteItem
        n = RFQ.objects.filter(created_by=user).delete()[0]
        counts["rfqs+items+quotes"] = n

        # 4. Quote'ы где user — seller (на чужие RFQ)
        n = Quote.objects.filter(seller=user).delete()[0]
        counts["quotes_as_seller"] = n

        # 5. Notifications
        n = Notification.objects.filter(user=user).delete()[0]
        counts["notifications"] = n

        # 6. Wallet — обнулить и восстановить начальный баланс (только для buyer)
        if not opts["keep_wallet"]:
            WalletTx.objects.filter(wallet__user=user).delete()
            wallet, _ = Wallet.objects.get_or_create(user=user)
            try:
                role = getattr(user.profile, "role", None)
            except Exception:
                role = None
            if role == "buyer":
                wallet.balance = Decimal("10000")
                wallet.save(update_fields=["balance"])
                counts["wallet_restored"] = "$10,000"
            else:
                wallet.balance = Decimal("0")
                wallet.save(update_fields=["balance"])
                counts["wallet_zeroed"] = "$0"

        # 7. KYB — сбросить в "none" (тестер может пройти заново)
        n = CompanyVerification.objects.filter(user=user).delete()[0]
        counts["kyb_reset"] = n

        # 8. Competitor offers
        n = CompetitorOffer.objects.filter(uploaded_by=user).delete()[0]
        counts["competitor_offers"] = n

        self.stdout.write(self.style.SUCCESS("\n  Reset summary:"))
        for k, v in counts.items():
            self.stdout.write(f"    {k:40s} {v}")
        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Готово. {username} может войти и начать с нуля."
        ))
