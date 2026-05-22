"""Create N beta-test accounts with unique strong credentials.

Usage:
    python manage.py seed_beta_testers --count 40
    python manage.py seed_beta_testers --count 40 --reset
    python manage.py seed_beta_testers --count 5 --role seller

Печатает CSV-таблицу учёток в stdout — отправь её тестерам.
"""
from __future__ import annotations

import csv
import secrets
import string
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

User = get_user_model()

PASSWORD_LEN = 12


def _gen_password(length: int = PASSWORD_LEN) -> str:
    """Strong but readable password (no l/I/O/0)."""
    alphabet = "".join(
        c for c in (string.ascii_letters + string.digits)
        if c not in "lIO0"
    )
    return "".join(secrets.choice(alphabet) for _ in range(length))


class Command(BaseCommand):
    help = "Seed beta-tester accounts with unique strong passwords."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=40,
                            help="Сколько тестеров создать (default 40)")
        parser.add_argument("--prefix", default="beta",
                            help="Префикс username (default: beta → beta01..beta40)")
        parser.add_argument("--role", choices=["buyer", "seller", "operator", "mixed"],
                            default="mixed",
                            help="Все одной роли или 'mixed' (60%% buyer / 30%% seller / 10%% operator)")
        parser.add_argument("--reset", action="store_true",
                            help="Удалить ранее созданных beta-тестеров перед посевом")
        parser.add_argument("--out", default="beta_testers.csv",
                            help="Путь к CSV-файлу с credentials (default beta_testers.csv)")

    @transaction.atomic
    def handle(self, *args, **opts):
        from decimal import Decimal

        from assistant.models import Wallet
        from marketplace.models import UserProfile

        count = opts["count"]
        prefix = opts["prefix"]
        role_mode = opts["role"]
        out_path = Path(opts["out"])

        if opts["reset"]:
            qs = User.objects.filter(username__startswith=prefix)
            n = qs.count()
            qs.delete()
            self.stdout.write(self.style.WARNING(f"⚠ Удалено {n} прежних аккаунтов {prefix}*"))

        # Назначение ролей при --role=mixed
        def _role_for_index(i: int) -> str:
            if role_mode != "mixed":
                return role_mode
            # 60% buyer, 30% seller, 10% operator
            if i % 10 < 6:
                return "buyer"
            if i % 10 < 9:
                return "seller"
            return "operator"

        rows = []
        for i in range(1, count + 1):
            username = f"{prefix}{i:02d}"
            password = _gen_password()
            role = _role_for_index(i)
            email = f"{username}@beta.consolidator.parts"

            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": email, "first_name": f"Beta{i:02d}"},
            )
            user.set_password(password)
            user.is_staff = (role == "operator")  # для доступа в /admin/ если потребуется
            user.save()

            profile, _ = UserProfile.objects.get_or_create(
                user=user,
                defaults={"role": role, "company_name": f"Beta Test Co {i:02d}"},
            )
            if profile.role != role:
                profile.role = role
                profile.save(update_fields=["role"])

            # Стартовый депозит для buyer-ролей — чтобы могли сразу делать заказы
            if role == "buyer":
                wallet, _ = Wallet.objects.get_or_create(user=user)
                if wallet.balance < Decimal("10000"):
                    wallet.balance = Decimal("10000")
                    wallet.save(update_fields=["balance"])

            rows.append({"username": username, "password": password, "role": role,
                          "email": email, "created": created})

        # CSV в файл
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["username", "password", "role", "email"])
            w.writeheader()
            for r in rows:
                w.writerow({k: r[k] for k in ("username", "password", "role", "email")})

        # И в stdout
        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Создано {len(rows)} beta-тестеров. CSV → {out_path.resolve()}\n"
        ))
        self.stdout.write(self.style.NOTICE(
            "─" * 70 +
            f"\n{'username':10s} {'password':14s} {'role':10s} email\n" +
            "─" * 70
        ))
        for r in rows:
            badge = "(new)" if r["created"] else "(reset password)"
            self.stdout.write(
                f"{r['username']:10s} {r['password']:14s} {r['role']:10s} "
                f"{r['email']:36s} {badge}"
            )
        self.stdout.write("─" * 70 + "\n")
        self.stdout.write(self.style.WARNING(
            "⚠ Сохрани CSV перед раздачей. Пароли больше нигде не покажутся."
        ))
