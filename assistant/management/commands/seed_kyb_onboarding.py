"""Seed 3 test KYB applications walking the full ТЗ flow.

Покрывает все 3 пути из «ТЗ: Онбординг и проверка поставщика» §5:

  A. CleanCorp (ИНН 7708123456, RU) — чистая компания, зелёный риск.
     Авто-проверки без замечаний → попадает в очередь оператора, кандидат
     в «Песочницу». Оператор должен одобрить → start sandbox.

  B. Hydrolux FZE (Company № 2233445, UAE) — зарубежная компания,
     VAT не подтверждён через VIES (компания вне ЕС, нужна ручная
     сверка TRN UAE). Жёлтый риск → ручная проверка, оператор может
     запросить уточнения (op_kyb_clarify) или одобрить.

  C. OldFabric (ИНН 5031000099, RU) — ликвидация + санкции + массовый
     директор. Красный риск → автоотказ системой ДО оператора.

Usage:
    python manage.py seed_kyb_onboarding          # idempotent
    python manage.py seed_kyb_onboarding --reset  # cleanup first
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from assistant.management._seed_guard import (
    add_seed_password_argument,
    ensure_dev_only,
    require_seed_password,
)

User = get_user_model()


COMPANIES = [
    # ── A: чистая RU ──
    {
        "username": "supplier_cleancorp",
        "legal_name": "ООО «Чистая Корпорация»",
        "country": "RU",
        "inn": "7708123456",
        "ogrn": "1027700001234",
        "kpp": "770801001",
        "vat_number": "",
        "legal_address": "г. Москва, ул. Промышленная, д. 17, оф. 401",
        "warehouse_address": "г. Москва, ул. Промышленная, д. 17, склад №3",
        "website": "https://cleancorp-parts.ru",
        "phone": "+74951234567",
        "whatsapp": "+74951234567",
        "telegram": "@cleancorp_parts",
        "contact_email": "info@cleancorp-parts.ru",
        "categories": "Caterpillar, Komatsu — гидравлика, фильтры",
        "bank_name": "ПАО Сбербанк",
        "bank_account": "40702810400001234567",
        "bik": "044525225",
        "director_name": "Иванов Иван Иванович",
        "expected_outcome": "В очередь оператора (green) → одобрить → sandbox",
    },
    # ── B: зарубежная (UAE) ──
    {
        "username": "supplier_hydrolux",
        "legal_name": "Hydrolux Trading FZE",
        "country": "AE",
        "inn": "2233445",
        "ogrn": "",
        "kpp": "",
        "vat_number": "AE100123456700003",
        "legal_address": "RAKEZ Business Zone, Ras Al Khaimah, UAE",
        "warehouse_address": "RAKEZ Industrial Zone, Warehouse B-17, RAK, UAE",
        "website": "https://hydrolux-fze.ae",
        "phone": "+97172041234",
        "whatsapp": "+971501234567",
        "telegram": "@hydrolux_uae",
        "contact_email": "sales@hydrolux-fze.ae",
        "categories": "Liebherr, JCB — гидроцилиндры, насосы (OEM)",
        "bank_name": "Emirates NBD",
        "bank_account": "AE070331234567890123456",
        "bik": "EBILAEAD",
        "director_name": "Ahmed Al-Mansoori",
        "expected_outcome": "В очередь оператора (yellow) → запросить уточнения / одобрить",
    },
    # ── C: проблемная RU ──
    {
        "username": "supplier_oldfabric",
        "legal_name": "ООО «Старый Завод»",
        "country": "RU",
        "inn": "5031000099",
        "ogrn": "1025003755555",
        "kpp": "503101001",
        "vat_number": "",
        "legal_address": "г. Электросталь, ул. Заводская, 3А",
        "warehouse_address": "г. Электросталь, ул. Заводская, 3А (тот же)",
        "website": "https://oldfabric.example",
        "phone": "+74961234567",
        "whatsapp": "",
        "telegram": "",
        "contact_email": "info@oldfabric.example",
        "categories": "Импорт и продажа запчастей",
        "bank_name": "Промсвязьбанк",
        "bank_account": "40702810400009999999",
        "bik": "044525555",
        "director_name": "Смирнов Виктор Геннадьевич",
        "expected_outcome": "АВТООТКАЗ (red: ликвидация + санкции + массовый директор)",
    },
]


class Command(BaseCommand):
    help = "Seed 3 KYB companies covering all 3 ТЗ paths (green/yellow/red)."

    def add_arguments(self, parser):
        add_seed_password_argument(parser)
        parser.add_argument("--reset", action="store_true",
                            help="Удалить ранее засеянные KYB-компании перед посевом.")

    def _h(self, msg): self.stdout.write(self.style.NOTICE(f"\n── {msg}"))
    def _ok(self, msg): self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))
    def _warn(self, msg): self.stdout.write(self.style.WARNING(f"  ⚠ {msg}"))

    @transaction.atomic
    def handle(self, *args, **opts):
        ensure_dev_only(self)
        password = require_seed_password(opts)
        from assistant.kyb_api_checks import evaluate_risk, run_all_checks
        from marketplace.models import CompanyVerification, UserProfile

        if opts["reset"]:
            self._h("RESET — удаляем ранее засеянные онбординг-компании")
            usernames = [c["username"] for c in COMPANIES]
            users = User.objects.filter(username__in=usernames)
            CompanyVerification.objects.filter(user__in=users).delete()
            self._ok(f"KYB сброшен для {users.count()} компаний")

        self._h(f"Seeding {len(COMPANIES)} test KYB applications")
        now = timezone.now()

        for c in COMPANIES:
            user, created = User.objects.get_or_create(
                username=c["username"],
                defaults={"email": c["contact_email"],
                          "first_name": c["legal_name"]},
            )
            if created:
                user.set_password(password)
                user.save()
            profile, _ = UserProfile.objects.get_or_create(
                user=user, defaults={"role": "seller", "company_name": c["legal_name"]},
            )
            if profile.role != "seller":
                profile.role = "seller"; profile.save(update_fields=["role"])
            if not profile.company_name:
                profile.company_name = c["legal_name"]
                profile.save(update_fields=["company_name"])

            kyb, _ = CompanyVerification.objects.update_or_create(
                user=user,
                defaults={k: v for k, v in c.items()
                          if k not in ("username", "expected_outcome")},
            )

            # ── Имитируем submit_for_review: прогон 5–7 API + risk eval ──
            # Команда доступна только при DEBUG=True и предназначена именно
            # для воспроизводимой проверки трех контрольных веток KYB.
            # Обычная работа приложения по-прежнему fail-closed: без живых
            # провайдеров все заявки уходят оператору на ручную проверку.
            with override_settings(KYB_ALLOW_TEST_FIXTURES=True):
                kyb.api_results = run_all_checks(kyb)
            decision, risk, reasons = evaluate_risk(kyb.api_results)
            kyb.risk_indicator = risk
            kyb.auto_decision = decision
            kyb.auto_checked_at = now
            kyb.submitted_at = now
            if decision == "auto_reject":
                kyb.status = "rejected"
                kyb.rejection_reason = "АВТООТКАЗ:\n• " + "\n• ".join(reasons[:5])
                kyb.reviewed_at = now
            else:
                kyb.status = "pending"
                kyb.rejection_reason = ""
            kyb.save()

            self._ok(
                f"{c['username']:25s} risk={risk:6s} decision={decision:18s} "
                f"status={kyb.status}\n     reasons={len(reasons)}, "
                f"expected={c['expected_outcome']}"
            )
            if reasons:
                for r in reasons[:3]:
                    self.stdout.write(f"        • {r[:100]}")

        # Сводка
        from marketplace.models import CompanyVerification as CV
        pending = CV.objects.filter(status="pending").count()
        rejected = CV.objects.filter(status="rejected").count()
        verified = CV.objects.filter(status="verified").count()
        self._h("Готово")
        self.stdout.write(self.style.SUCCESS(
            f"\n  Pending (на проверку оператором): {pending}\n"
            f"  Rejected (автоотказ):              {rejected}\n"
            f"  Verified (одобрено):               {verified}\n"
            f"\nЗайди как demo_operator → 🛡 KYB поставщиков:\n"
            f"  • ООО «Чистая Корпорация» — зелёный → одобрить → Песочница\n"
            f"  • Hydrolux Trading FZE — жёлтый → запросить уточнения / одобрить\n"
            f"  • ООО «Старый Завод» — уже отказан (виден в журнале)\n"
        ))
