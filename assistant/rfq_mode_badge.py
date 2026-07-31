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
from django.utils.translation import gettext as _

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
    _emoji, label, _sla, _hint = meta
    return _(label)


def mode_badge_with_sla(mode: str | None) -> str:
    """Развёрнутый бейдж: «Ручная рассылка · 48 часов»."""
    meta = _MODE_META.get((mode or "").strip().lower())
    if not meta:
        return ""
    _emoji, label, sla, _hint = meta
    return f"{_(label)} · {_(sla)}"


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
    from django.utils.translation import gettext as _
    if m == "semi":
        total_sec = 15 * 60
        remaining = total_sec - elapsed
        if remaining < 0:
            return (_("SLA нарушен %(n)s мин назад") % {"n": int(-remaining // 60)}, "bad")
        tone = "warn" if remaining < total_sec * 0.5 else "ok"
        return (_("осталось %(n)s мин из 15") % {"n": int(remaining // 60)}, tone)
    if m in ("manual", "manual_oem"):
        total_sec = 48 * 3600
        remaining = total_sec - elapsed
        if remaining < 0:
            return (_("48 ч просрочены на %(h)s ч") % {"h": int(-remaining // 3600)}, "bad")
        tone = "warn" if remaining < total_sec * 0.5 else "ok"
        return (_("осталось %(h)s ч из 48") % {"h": int(remaining // 3600)}, tone)
    return ("", "info")


def mode_hint_for_seller(mode: str | None) -> str:
    """Подсказка-объяснение для продавца: что значит этот режим.

    Возвращает 1 строку (≤ 80 символов), чтобы вставить в карточку формы.
    """
    meta = _MODE_META.get((mode or "").strip().lower())
    if not meta:
        return ""
    emoji, label, sla, hint = meta
    mode_key = (mode or "").strip().lower()
    if mode_key in ("manual", "manual_oem"):
        return (f"{emoji} {_(label)} · {_(sla)} — {_('оператор выбрал именно вас.')} "
                f"{_('Ответ приоритетный.')}")
    if mode_key == "semi":
        return (f"{emoji} {_(label)} · {_(sla)} — {_('оператор ждёт вашу цену перед сборкой КП.')}")
    return f"{emoji} {_(label)} · {_(sla)} — {_(hint)}."
