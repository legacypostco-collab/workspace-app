"""Создать/обновить аккаунты команды + тестовые аккаунты.

Идемпотентно (get_or_create по username) — безопасно запускать повторно.
По умолчанию НИЧЕГО не удаляет (только добавляет/обновляет).

Структура (по реальным ролям):
  • KAM (operator_manager):  личный + 3 тест-покупателя (привязаны как клиенты CRM)
  • Seller:                  личный + 3 тест-покупателя
  • Разработка (admin):      личный admin + 3 тест-покупателя
  • 7 безымянных:            client01..client07 (покупатели)
Все «с данными»: UserProfile(role) + company_name + пополненный Wallet.

Запуск:
  python manage.py seed_team_accounts                 # создать/обновить
  TEAM_PASSWORD=… python manage.py seed_team_accounts # свой пароль
  python manage.py seed_team_accounts --wallet 100000 # сумма на кошельках
"""
from __future__ import annotations

import os
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

DEFAULT_PASSWORD = "Consolidator2026"

# (display_name, username_base, role, operator_role, is_admin)
TEAM = [
    # KAM — operator_manager
    ("Константин К",     "konstantin_k",   "operator", "manager", False),
    ("Аркадий П",        "arkadiy_p",      "operator", "manager", False),
    ("Дмитрий Б",        "dmitriy_b",      "operator", "manager", False),
    # Продавцы
    ("Денис М",          "denis_m",        "seller",   "",        False),
    ("Владислав Д",      "vladislav_d",    "seller",   "",        False),
    ("Евгений А",        "evgeniy_a",      "seller",   "",        False),
    ("Даниил П",         "daniil_p",       "seller",   "",        False),
    # Разработка — admin (is_staff + is_superuser)
    ("Кирилл (разраб.)", "kirill",         "buyer",    "",        True),
    ("Никита М",         "nikita_m",       "buyer",    "",        True),
    ("Александр Зенит",  "aleksandr_zenit","buyer",    "",        True),
    ("Али",              "ali",            "buyer",    "",        True),
    ("Альбина",          "albina",         "buyer",    "",        True),
]
NUM_TEST_PER_PERSON = 3
NUM_ANON = 7


class Command(BaseCommand):
    help = "Создать/обновить аккаунты команды + тестовые аккаунты (идемпотентно)."

    def add_arguments(self, parser):
        parser.add_argument("--wallet", type=int, default=50000,
                            help="Сумма пополнения кошелька (USD), 0 — не пополнять.")
        parser.add_argument("--password", type=str, default="",
                            help="Пароль для всех аккаунтов (или env TEAM_PASSWORD).")

    def handle(self, *args, **opts):
        from marketplace.models import UserProfile, Customer
        from assistant.models import Wallet, WalletTx

        password = (opts.get("password") or os.getenv("TEAM_PASSWORD")
                    or DEFAULT_PASSWORD).strip()
        wallet_amount = Decimal(str(opts.get("wallet") or 0))
        created_rows, updated_rows = [], []

        def ensure_user(username, *, display, role, operator_role="",
                        is_admin=False, company=""):
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@consolidatorparts.com"},
            )
            user.set_password(password)
            user.first_name = display[:30]
            user.is_staff = bool(is_admin)
            user.is_superuser = bool(is_admin)
            if not user.email:
                user.email = f"{username}@consolidatorparts.com"
            user.save()
            prof, _ = UserProfile.objects.get_or_create(user=user)
            prof.role = role
            prof.operator_role = operator_role or ""
            if company:
                prof.company_name = company
            prof.save()
            if wallet_amount > 0:
                w = Wallet.for_user(user)
                if (w.balance or Decimal("0")) < wallet_amount:
                    w.balance = wallet_amount
                    w.save(update_fields=["balance", "updated_at"])
                    WalletTx.objects.get_or_create(
                        wallet=w, kind="topup", amount=wallet_amount,
                        defaults={"description": "Тестовое пополнение (seed)",
                                  "balance_after": wallet_amount},
                    )
            (created_rows if created else updated_rows).append(
                (username, role + (f"/{operator_role}" if operator_role else "")
                 + ("+admin" if is_admin else "")))
            return user

        with transaction.atomic():
            for display, base, role, op_role, is_admin in TEAM:
                # Личный аккаунт
                personal = ensure_user(
                    base, display=display, role=role, operator_role=op_role,
                    is_admin=is_admin, company=display)
                # 3 тест-покупателя
                test_buyers = []
                for i in range(1, NUM_TEST_PER_PERSON + 1):
                    tb = ensure_user(
                        f"{base}_t{i}", display=f"{display} · тест {i}",
                        role="buyer", company=f"{display} — тест {i}")
                    test_buyers.append(tb)
                # Для KAM — привязать тест-покупателей как клиентов CRM (данные)
                if role == "operator" and op_role == "manager":
                    for i, tb in enumerate(test_buyers, 1):
                        Customer.objects.get_or_create(
                            owner=personal, inn=f"TEST{personal.id:04d}{i}",
                            defaults={"name": f"{display} — клиент {i}",
                                      "user": tb, "note": "seed: тестовый клиент"},
                        )

            # 7 безымянных покупателей
            for n in range(1, NUM_ANON + 1):
                ensure_user(f"client{n:02d}", display=f"Клиент {n:02d}",
                            role="buyer", company=f"Тест-клиент {n:02d}")

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово. Пароль для всех: {password}"))
        self.stdout.write(f"Создано: {len(created_rows)} · обновлено: {len(updated_rows)}")
        self.stdout.write("\n— Создано —")
        for u, r in created_rows:
            self.stdout.write(f"  {u:22s} {r}")
        if updated_rows:
            self.stdout.write("\n— Обновлено (уже были) —")
            for u, r in updated_rows:
                self.stdout.write(f"  {u:22s} {r}")
