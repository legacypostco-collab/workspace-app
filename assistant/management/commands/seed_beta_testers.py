"""Create N beta-test accounts with unique strong credentials.

Usage:
    python manage.py seed_beta_testers --count 40
    python manage.py seed_beta_testers --count 40 --reset
    python manage.py seed_beta_testers --count 5 --role seller

Сохраняет CSV-таблицу учетных записей в файл, доступный только владельцу.
"""
from __future__ import annotations

import csv
import os
import secrets
import string
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from assistant.management._seed_guard import ensure_dev_only

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
        ensure_dev_only(self)
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
            email = f"{username}@beta.invalid"

            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": email, "first_name": f"Beta{i:02d}"},
            )
            user.set_password(password)
            user.is_staff = False
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
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["username", "password", "role", "email"])
            w.writeheader()
            for r in rows:
                w.writerow({k: r[k] for k in ("username", "password", "role", "email")})

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Создано {len(rows)} beta-тестеров. CSV → {out_path.resolve()}\n"
        ))
        self.stdout.write(self.style.WARNING(
            "Пароли записаны только в CSV с правами доступа владельца файла."
        ))
