"""Create disposable local accounts for the stateful browser user story."""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from assistant.management._seed_guard import (
    add_seed_password_argument,
    ensure_dev_only,
    require_seed_password,
)


User = get_user_model()
ACCOUNT_ROLES = {
    "itu_us_buyer_a": "buyer",
    "itu_us_buyer_b": "buyer",
    "itu_us_seller_a": "seller",
    "itu_us_seller_b": "seller",
    "itu_us_multi": "buyer",
    "itu_us_operator": "operator",
}
BUYER_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
BUYER_BACKUP_CODES = [f"ITU-STORY-{index:02d}-2026" for index in range(1, 13)]


class Command(BaseCommand):
    help = "Seed disposable accounts for tests/e2e/test_full_user_story.py."

    def add_arguments(self, parser):
        add_seed_password_argument(parser)
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete previous user-story accounts and their related data.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        ensure_dev_only(self)
        password = require_seed_password(options)

        from assistant.models import Wallet
        from assistant.security import encode_backup_codes
        from marketplace.models import (
            CompanyVerification,
            TwoFactorAuth,
            UserProfile,
            UserRole,
        )

        if options["reset"]:
            User.objects.filter(username__in=ACCOUNT_ROLES).delete()

        users = {}
        for username, role in ACCOUNT_ROLES.items():
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@example.test"},
            )
            user.email = f"{username}@example.test"
            user.is_active = True
            user.is_staff = role == "operator"
            user.set_password(password)
            user.save()

            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = role
            profile.operator_role = ""
            profile.company_name = f"ITU User Story {username}"
            profile.language = "ru"
            profile.notif_email_enabled = False
            if role == "seller":
                profile.external_score = Decimal("90.00")
                profile.behavioral_score = Decimal("90.00")
                profile.can_manage_assortment = True
                profile.can_manage_pricing = True
                profile.can_manage_orders = True
            profile.save()
            users[username] = user

        operator = users["itu_us_operator"]
        for username in (
            "itu_us_buyer_a",
            "itu_us_buyer_b",
            "itu_us_seller_a",
            "itu_us_seller_b",
            "itu_us_multi",
        ):
            user = users[username]
            CompanyVerification.objects.update_or_create(
                user=user,
                defaults={
                    "status": "verified",
                    "legal_name": f"ITU User Story {username}",
                    "country": "AE",
                    "risk_indicator": "green",
                    "auto_decision": "sandbox_candidate",
                    "reviewed_at": timezone.now(),
                    "reviewed_by": operator,
                },
            )

        UserRole.objects.update_or_create(
            user=users["itu_us_multi"],
            role="seller",
            operator_role="",
            defaults={"is_enabled": True},
        )

        for username in ("itu_us_buyer_a", "itu_us_buyer_b"):
            wallet, _ = Wallet.objects.get_or_create(user=users[username])
            wallet.balance = Decimal("10000000.00")
            wallet.currency = "USD"
            wallet.save(update_fields=["balance", "currency", "updated_at"])

        buyer = users["itu_us_buyer_a"]
        TwoFactorAuth.objects.update_or_create(
            user=buyer,
            defaults={
                "secret": BUYER_TOTP_SECRET,
                "enabled": True,
                "enabled_at": timezone.now(),
                "last_totp_counter": None,
                "backup_codes": encode_backup_codes(
                    buyer,
                    BUYER_BACKUP_CODES,
                ),
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "User-story accounts are ready. "
                f"Created or updated: {len(users)}."
            )
        )
        self.stdout.write(
            "E2E_BUYER_BACKUP_CODES=" + ",".join(BUYER_BACKUP_CODES)
        )
