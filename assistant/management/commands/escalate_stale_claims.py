"""Эскалация «застрявших» рекламаций — алерт супервайзеру.

Если OrderClaim открыт > N дней (default 7) и не resolved/closed, и его ещё
не эскалировали, отправляем alert в admin-chat «Алерты оператора».

Идемпотентность: после эскалации ставим `escalated_at = now()`. При следующем
запуске уже эскалированные пропускаются.

Можно настраивать TTL через --days (для разных policies SLA).

Запуск:
    python manage.py escalate_stale_claims          # порог 7 дней
    python manage.py escalate_stale_claims --days=3 # порог 3 дня (для критичных)
    python manage.py escalate_stale_claims --dry-run

CRON (production):
    0 9 * * * cd /app && python manage.py escalate_stale_claims
    # каждый рабочий день в 9:00 утра
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from assistant.order_events import notify_operator_alert
from marketplace.models import OrderClaim

# Статусы которые считаем «незакрытыми» (требуют действий оператора).
OPEN_STATUSES = ("open", "in_review", "approved",
                 "corrective_actions", "financial_settlement")

# Per-category SLA: разные виды claim требуют разной скорости реакции.
# Можно переопределить через settings.CLAIM_SLA_DAYS (dict) — для prod-тюнинга.
# `default` применяется к видам не указанным явно.
DEFAULT_SLA_DAYS = {
    "missing":    2,  # «не пришла» — критично, проверка трекинга
    "defect":     3,  # брак — нужен техосмотр у seller'a
    "damage":     3,  # повреждение при доставке — претензия к логисту
    "late":       5,  # просрочка — переговоры о компенсации
    "wrong_part": 7,  # «не та деталь» — длинный процесс уточнения
    "other":      7,
    "default":    7,
}


class Command(BaseCommand):
    help = ("Send escalation alerts for OrderClaims that have been open "
            "for more than N days without resolution.")

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None,
                            help="Override: единый порог для всех видов "
                                 "(по умолчанию — per-category SLA).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Только показать что будет эскалировано.")

    def _sla_for_kind(self, kind: str, override_days: int | None) -> int:
        """Возвращает SLA в днях для конкретного claim.kind.

        Если --days передан, используется он (одинаково для всех видов).
        Иначе берётся из settings.CLAIM_SLA_DAYS (override) или DEFAULT_SLA_DAYS.
        """
        if override_days is not None:
            return max(1, override_days)
        from django.conf import settings
        sla = getattr(settings, "CLAIM_SLA_DAYS", None) or {}
        return int(sla.get(kind) or DEFAULT_SLA_DAYS.get(kind) or DEFAULT_SLA_DAYS["default"])

    def handle(self, *args, **opts):
        override_days = opts.get("days")  # None = per-category
        dry = opts["dry_run"]
        now = timezone.now()

        # Берём ВСЕ не-эскалированные открытые — фильтр по age делаем в Python
        # (потому что порог зависит от kind, нельзя одним запросом).
        candidates_all = OrderClaim.objects.filter(
            status__in=OPEN_STATUSES,
            escalated_at__isnull=True,
        ).select_related("order", "order__buyer", "opened_by")

        candidates = []
        for c in candidates_all:
            sla_days = self._sla_for_kind(c.kind, override_days)
            age_days = (now - c.created_at).days
            if age_days >= sla_days:
                candidates.append((c, sla_days, age_days))

        total = len(candidates)
        if not total:
            mode = (f">{override_days}д (override)" if override_days
                    else "(per-category SLA)")
            self.stdout.write(self.style.SUCCESS(
                f"✓ Нет застрявших рекламаций {mode} · OK"
            ))
            return

        self.stdout.write(self.style.WARNING(
            f"⚠ Найдено {total} рекламаций превысивших SLA"
            + (" (DRY-RUN)" if dry else "")
        ))

        escalated_now = []
        for claim, sla_days, age_days in candidates:
            order = claim.order
            line = (f"  #{claim.id} · {claim.get_kind_display():18} · "
                    f"ORD-{order.id if order else '—'} · "
                    f"{claim.get_status_display()} · "
                    f"{age_days}д (SLA={sla_days}д) · "
                    f"«{(claim.title or '')[:50]}»")
            self.stdout.write(line)

            if dry:
                continue

            try:
                # Алерт супервайзеру в admin-chat + push в Telegram (если есть)
                notify_operator_alert(
                    claim=claim,
                    event="claim_escalated",
                    text=(
                        f"🚨 ЭСКАЛАЦИЯ · Рекламация #{claim.id} ({claim.get_kind_display()})\n"
                        f"Открыта {age_days}д · SLA для этого вида = {sla_days}д.\n"
                        f"ORD-{order.id if order else '—'} · "
                        f"статус: {claim.get_status_display()}\n"
                        f"Юзер: @{claim.opened_by.username if claim.opened_by else '—'}\n"
                        f"Краткое описание: {(claim.title or '')[:120]}"
                    ),
                )
                claim.escalated_at = timezone.now()
                claim.save(update_fields=["escalated_at"])
                escalated_now.append(claim.id)

                # Bonus: telegram-нотификация всем on-call операторам.
                try:
                    from assistant.notif_settings import send_telegram_to_operators
                    send_telegram_to_operators(
                        f"🚨 ЭСКАЛАЦИЯ · Claim #{claim.id}\n"
                        f"{claim.get_kind_display()} · {age_days}д (SLA {sla_days}д)\n"
                        f"ORD-{order.id if order else '—'} · "
                        f"@{claim.opened_by.username if claim.opened_by else '—'}\n"
                        f"{(claim.title or '')[:120]}"
                    )
                except Exception:
                    pass  # tg-канал необязателен, не падаем
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f"    ✗ Эскалация #{claim.id} провалена: {e}"
                ))

        if dry:
            self.stdout.write(self.style.NOTICE(
                f"\n[DRY-RUN] Было бы эскалировано {total} штук — без фактической записи."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n✓ Эскалировано {len(escalated_now)}/{total}: {escalated_now}"
            ))
