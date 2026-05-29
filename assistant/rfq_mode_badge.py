"""Единый бейдж режима RFQ для всех ролей.

Используется в seller_inbox, respond_rfq_form, op_queue и любом другом месте,
где надо показать пользователю, в каком режиме обрабатывается RFQ:

    🤖 AUTO   — автоматический подбор, без оператора, ~1 сек
    👨‍💼 SEMI  — полуавтоматический, оператор подтверждает, SLA 15 мин
    🔍 MANUAL — ручной OEM-поиск, оператор адресно рассылает, до 48 ч

Раньше продавец не видел режим, и MANUAL-запрос (где оператор выбрал
именно его) выглядел как массовый AUTO — приоритет терялся.
"""
from __future__ import annotations

# (emoji, short_label, sla_hint, priority_for_seller)
# Без эмодзи и без англо-аббревиатур — понятные русские названия
_MODE_META: dict[str, tuple[str, str, str, str]] = {
    "auto":       ("", "Автоподбор",          "≈1 сек",       "без оператора"),
    "semi":       ("", "Нужно подтвердить",   "15 минут",     "оператор подтверждает КП"),
    "manual":     ("", "Ручная рассылка",     "48 часов",     "оператор рассылает поставщикам"),
    "manual_oem": ("", "Ручная рассылка",     "48 часов",     "оператор рассылает поставщикам"),
}


def mode_badge(mode: str | None) -> str:
    """Короткий бейдж: «Ручная рассылка». Безопасно для пустого/неизвестного режима."""
    meta = _MODE_META.get((mode or "").strip().lower())
    if not meta:
        return ""
    _emoji, label, _, _ = meta
    return label


def mode_badge_with_sla(mode: str | None) -> str:
    """Развёрнутый бейдж: «Ручная рассылка · 48 часов»."""
    meta = _MODE_META.get((mode or "").strip().lower())
    if not meta:
        return ""
    _emoji, label, sla, _ = meta
    return f"{label} · {sla}"


def sla_countdown_for_operator(mode: str, created_at) -> tuple[str, str]:
    """Обратный отсчёт SLA для оператора. Возвращает (subtitle, tone).

    Для SEMI: SLA 15 мин с момента создания RFQ.
    Для MANUAL: SLA 48 часов.
    Tone: 'ok' (>50% времени осталось), 'warn' (<50%), 'bad' (нарушен).

    Раньше показывали «13мин назад» — оператор должен в голове считать,
    сколько осталось. Теперь показываем «осталось 2 мин» / «нарушен 5 мин назад».
    """
    from django.utils import timezone
    if not created_at:
        return ("", "info")
    now = timezone.now()
    elapsed = (now - created_at).total_seconds()
    m = (mode or "").lower()
    # Без эмодзи внутри текста — иконка вынесена в title-префикс.
    if m == "semi":
        total_sec = 15 * 60
        remaining = total_sec - elapsed
        if remaining < 0:
            return (f"SLA нарушен {int(-remaining // 60)} мин назад", "bad")
        tone = "warn" if remaining < total_sec * 0.5 else "ok"
        return (f"осталось {int(remaining // 60)} мин из 15", tone)
    if m in ("manual", "manual_oem"):
        total_sec = 48 * 3600
        remaining = total_sec - elapsed
        if remaining < 0:
            return (f"48 ч просрочены на {int(-remaining // 3600)} ч", "bad")
        tone = "warn" if remaining < total_sec * 0.5 else "ok"
        return (f"осталось {int(remaining // 3600)} ч из 48", tone)
    return ("", "info")


def mode_hint_for_seller(mode: str | None) -> str:
    """Подсказка-объяснение для продавца: что значит этот режим.

    Возвращает 1 строку (≤ 80 символов), чтобы вставить в карточку формы.
    """
    meta = _MODE_META.get((mode or "").strip().lower())
    if not meta:
        return ""
    emoji, label, sla, hint = meta
    if label == "MANUAL":
        return (f"{emoji} {label} · {sla} — оператор выбрал именно вас. "
                f"Ответ приоритетный.")
    if label == "SEMI":
        return (f"{emoji} {label} · {sla} — оператор ждёт вашу цену "
                f"перед сборкой КП.")
    return f"{emoji} {label} · {sla} — {hint}."
