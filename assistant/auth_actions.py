"""Real authentication: magic-link, TOTP 2FA, API tokens.

Дополняет стандартный username/password Django способами:

  • request_magic_link  — passwordless вход через email-ссылку (HTTP-only)
  • setup_2fa           — генерация TOTP secret + QR + backup codes
  • verify_2fa          — подтверждение OTP, активация 2FA
  • disable_2fa         — выключение с проверкой OTP
  • create_api_token    — генерация API-токена для интеграций
  • list_api_tokens     — список активных токенов
  • revoke_api_token    — отзыв токена

OAuth (Google/Yandex) — scaffolding в `auth_views.py`. Реальный flow
требует клиент-ID/secret в env.
"""
from __future__ import annotations

import hashlib
import base64
import logging
import secrets

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .actions import ActionResult, register
from .security import confirmation_is_true

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_token(prefix: str = "ck_live_") -> tuple[str, str]:
    """Возвращает (full_token, prefix_for_ui). Полный токен виден ОДИН раз."""
    raw = secrets.token_urlsafe(32)
    full = prefix + raw
    return full, full[:12]


def _local_qr_placeholder() -> str:
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240' viewBox='0 0 240 240'>"
        "<rect width='240' height='240' fill='white'/>"
        "<rect x='18' y='18' width='58' height='58' fill='none' stroke='#111' stroke-width='10'/>"
        "<rect x='164' y='18' width='58' height='58' fill='none' stroke='#111' stroke-width='10'/>"
        "<rect x='18' y='164' width='58' height='58' fill='none' stroke='#111' stroke-width='10'/>"
        "<path d='M105 38h20v20h-20zM135 38h12v12h-12zM104 86h18v18h-18zM140 88h22v22h-22zM96 128h16v16H96zM122 122h20v20h-20zM152 126h14v14h-14zM184 112h22v22h-22zM94 166h26v26H94zM134 166h16v16h-16zM164 164h40v40h-40z' fill='#111'/>"
        "<text x='120' y='116' text-anchor='middle' font-family='Arial,sans-serif' font-size='11' fill='#444'>manual key</text>"
        "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# ══════════════════════════════════════════════════════════
# 1. TOTP 2FA — enable / verify / disable
# ══════════════════════════════════════════════════════════

@register("setup_2fa")
def setup_2fa(params, user, role):
    """Сгенерировать TOTP secret + показать QR-URL для сканирования."""
    from marketplace.models import TwoFactorAuth
    try:
        import pyotp
    except ImportError:
        return ActionResult(text=_("⚠️ pyotp не установлен. pip install pyotp."))

    twofa, _created = TwoFactorAuth.objects.get_or_create(user=user)
    if twofa.enabled:
        return ActionResult(
            text=_("🔐 2FA уже включён. Выключить можно через disable_2fa."),
            contextual_actions=[
                {"action": "disable_2fa", "label": _("🔓 Выключить 2FA")},
            ],
        )

    # Новый secret каждый раз когда юзер запросил setup
    secret = pyotp.random_base32()
    twofa.secret = secret
    # Plain values are returned once; only keyed digests are stored.
    backup = [secrets.token_hex(4) for _u1 in range(8)]
    from .security import encode_backup_codes

    twofa.backup_codes = encode_backup_codes(user, backup)
    twofa.last_totp_counter = None
    twofa.save(update_fields=["secret", "backup_codes", "last_totp_counter"])

    issuer = "Consolidator"
    label = user.email or user.username
    otpauth_url = pyotp.totp.TOTP(secret).provisioning_uri(name=label, issuer_name=issuer)

    return ActionResult(
        text=_(
            "🔐 Setup 2FA · отсканируйте QR в Google Authenticator / Authy / 1Password.\n"
            "После добавления — введите 6-значный код из приложения через verify_2fa."
        ),
        cards=[
            {"type": "qr", "data": {
                "title": _("🔐 TOTP setup"),
                "image_url": _local_qr_placeholder(),
                "qr_url": _local_qr_placeholder(),
                "payload": otpauth_url,
                "subtitle": _("Issuer: %(issuer)s · Account: %(label)s")
                            % {"issuer": issuer, "label": label},
                "manual_entry": secret,
            }},
            {"type": "list", "data": {
                "title": _("🔑 Backup-коды (одноразовые)"),
                "items": [{"title": code, "subtitle": _("сохраните в надёжное место")}
                          for code in backup],
            }},
        ],
        actions=[
            {"action": "verify_2fa", "label": _("✓ Ввести код из приложения")},
        ],
        storage_text=_(
            "🔐 Настройка 2FA начата. QR-код, секрет и резервные коды "
            "были показаны один раз и не сохраняются в истории чата."
        ),
        storage_cards=[],
    )


@register("verify_2fa")
def verify_2fa(params, user, role):
    """Подтвердить OTP-код и активировать 2FA."""
    from marketplace.models import TwoFactorAuth
    try:
        import pyotp
    except ImportError:
        return ActionResult(text=_("⚠️ pyotp не установлен."))

    twofa = TwoFactorAuth.objects.filter(user=user).first()
    if not twofa or not twofa.secret:
        return ActionResult(
            text=_("Сначала пройдите setup_2fa."),
            actions=[{"action": "setup_2fa", "label": _("🔐 Запустить setup")}],
        )

    code = (params.get("code") or "").strip()
    confirmed = confirmation_is_true(params.get("confirmed"))
    if not confirmed or not code:
        return ActionResult(
            text=_("🔐 Введите 6-значный код из вашего authenticator-приложения."),
            cards=[{"type": "form", "data": {
                "title": _("🔐 Подтверждение 2FA"),
                "submit_action": "verify_2fa",
                "fields": [
                    {"name": "code", "label": _("OTP-код (6 цифр)"), "required": True},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    from .security import matching_totp_counter

    with transaction.atomic():
        twofa = TwoFactorAuth.objects.select_for_update().get(pk=twofa.pk)
        counter = matching_totp_counter(twofa.secret, code)
        if counter is None:
            return ActionResult(text=_("❌ Код неверный или устарел. Попробуйте ещё раз."))
        if (
            twofa.last_totp_counter is not None
            and counter <= twofa.last_totp_counter
        ):
            return ActionResult(text=_("❌ Этот код уже использован. Дождитесь следующего."))
        twofa.enabled = True
        twofa.enabled_at = timezone.now()
        twofa.last_totp_counter = counter
        twofa.save(
            update_fields=["enabled", "enabled_at", "last_totp_counter"],
        )
    return ActionResult(
        text=_("✓ 2FA активирован! При входе или критичных платежах потребуется код из приложения."),
        contextual_actions=[
            {"action": "notif_prefs", "label": _("🔔 Настройки уведомлений")},
        ],
    )


@register("disable_2fa")
def disable_2fa(params, user, role):
    """Выключить 2FA — требует подтверждения через OTP."""
    from marketplace.models import TwoFactorAuth
    twofa = TwoFactorAuth.objects.filter(user=user).first()
    if not twofa or not twofa.enabled:
        return ActionResult(text=_("2FA не активирован."))

    code = (params.get("code") or "").strip()
    confirmed = confirmation_is_true(params.get("confirmed"))
    if not confirmed or not code:
        return ActionResult(
            text=_("🔓 Подтвердите выключение 2FA вашим OTP-кодом."),
            cards=[{"type": "form", "data": {
                "title": _("🔓 Выключить 2FA"),
                "submit_action": "disable_2fa",
                "fields": [
                    {"name": "code", "label": _("OTP-код для подтверждения"), "required": True},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    from .security import verify_user_2fa

    if not verify_user_2fa(user, code):
        return ActionResult(text=_("❌ Код неверный."))

    twofa.enabled = False
    twofa.secret = ""
    twofa.backup_codes = ""
    twofa.last_totp_counter = None
    twofa.save(
        update_fields=[
            "enabled",
            "secret",
            "backup_codes",
            "last_totp_counter",
        ],
    )
    return ActionResult(text=_("✓ 2FA выключен."))


# ══════════════════════════════════════════════════════════
# 2. API tokens
# ══════════════════════════════════════════════════════════

@register("create_api_token")
def create_api_token(params, user, role):
    """Сгенерировать API-токен. Полный токен виден один раз."""
    from marketplace.models import ApiToken
    if role != "admin":
        return ActionResult(text=_("Управление API-токенами доступно только администратору."))
    label = (params.get("label") or "").strip()
    permissions = (params.get("permissions") or "read").strip().lower()
    confirmed = confirmation_is_true(params.get("confirmed"))

    if not confirmed or not label:
        return ActionResult(
            text=_("🔑 Создать API-токен"),
            cards=[{"type": "form", "data": {
                "title": _("🔑 Новый API-токен"),
                "submit_action": "create_api_token",
                "fields": [
                    {"name": "label", "label": _("Название (например, 'CI deploy')"), "required": True},
                    {"name": "permissions", "label": _("Разрешения"),
                     "type": "select",
                     "options": [
                         {"value": "read",       "label": _("read · только чтение")},
                         {"value": "read,write", "label": _("read+write · стандарт")},
                         {"value": "read,write,admin", "label": _("admin · полный доступ")},
                     ],
                     "value": "read,write"},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    allowed_permissions = {"read", "read,write", "read,write,admin"}
    if permissions not in allowed_permissions:
        return ActionResult(
            text=_("Недопустимый набор разрешений для ключа API.")
        )

    full, prefix = _generate_token()
    token = ApiToken.objects.create(
        user=user, label=label[:80],
        prefix=prefix, hashed_token=_hash_token(full),
        permissions=permissions,
    )
    return ActionResult(
        text=(
            _("✓ Токен создан · ID #%(id)s\n\n"
              "⚠️ Сохраните токен — больше не увидите:\n"
              "`%(full)s`\n\n"
              "Использование: `Authorization: Bearer %(full)s` в HTTP-заголовке.")
            % {"id": token.id, "full": full}
        ),
        cards=[{"type": "draft", "data": {
            "title": _("🔑 API-токен · %(label)s") % {"label": label},
            "rows": [
                {"label": _("Префикс"), "value": prefix},
                {"label": "Permissions", "value": permissions, "primary": True},
                {"label": _("Полный токен"), "value": full, "primary": True},
            ],
            "warnings": [_("Токен показывается ОДИН раз. Скопируйте сейчас.")],
            "confirm_label": "—",
        }}],
        contextual_actions=[
            {"action": "list_api_tokens", "label": _("📋 Все токены")},
        ],
        storage_text=(
            _("✓ Ключ API создан · ID #%(id)s\n\n"
              "Полное значение было показано один раз и не сохраняется "
              "в истории чата.")
            % {"id": token.id}
        ),
        storage_cards=[{"type": "draft", "data": {
            "title": _("🔑 Ключ API · %(label)s") % {"label": label},
            "rows": [
                {"label": _("Префикс"), "value": prefix},
                {"label": _("Разрешения"), "value": permissions, "primary": True},
            ],
            "warnings": [_("Полное значение не хранится в истории.")],
            "confirm_label": "—",
        }}],
    )


@register("list_api_tokens")
def list_api_tokens(params, user, role):
    """Список активных и отозванных токенов."""
    from marketplace.models import ApiToken
    if role != "admin":
        return ActionResult(text=_("Управление API-токенами доступно только администратору."))
    tokens = list(ApiToken.objects.filter(user=user).order_by("-created_at")[:20])
    if not tokens:
        return ActionResult(
            text=_("🔑 У вас ещё нет API-токенов."),
            actions=[{"action": "create_api_token", "label": _("➕ Создать")}],
        )
    rows = []
    for t in tokens:
        flags = []
        if not t.is_active: flags.append("🚫 revoked")
        if t.last_used_at: flags.append(f"used {t.last_used_at:%d.%m %H:%M}")
        rows.append({
            "title": f"{t.label} · {t.prefix}",
            "subtitle": (
                f"{t.permissions} · created {t.created_at:%d.%m.%Y}"
                + (" · " + ", ".join(flags) if flags else "")
            ),
        })
    return ActionResult(
        text=_("🔑 У вас %(n)s API-токенов.") % {"n": len(tokens)},
        cards=[{"type": "list", "data": {"title": _("🔑 API-токены"), "items": rows}}],
        contextual_actions=[
            {"action": "create_api_token", "label": _("➕ Создать новый")},
        ],
    )


@register("revoke_api_token")
def revoke_api_token(params, user, role):
    from marketplace.models import ApiToken
    if role != "admin":
        return ActionResult(text=_("Управление API-токенами доступно только администратору."))
    try:
        token = ApiToken.objects.get(id=int(params.get("token_id") or 0), user=user)
    except (ApiToken.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Токен не найден."))
    if not token.is_active:
        return ActionResult(text=_("Токен %(prefix)s уже отозван.") % {"prefix": token.prefix})
    if not confirmation_is_true(params.get("confirmed")):
        return ActionResult(
            text=_("Отозвать токен %(label)s?") % {"label": token.label},
            cards=[{"type": "draft", "data": {
                "title": _("🚫 Отозвать токен · %(label)s") % {"label": token.label},
                "rows": [
                    {"label": _("Префикс"), "value": token.prefix, "primary": True},
                    {"label": "Permissions", "value": token.permissions},
                ],
                "warnings": [_("Все интеграции, использующие этот токен, перестанут работать.")],
                "confirm_action": "revoke_api_token",
                "confirm_label": _("🚫 Отозвать"),
                "confirm_params": {"token_id": token.id, "confirmed": True},
                "cancel_label": _("Отмена"),
            }}],
        )

    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])
    return ActionResult(
        text=_("✓ Токен %(prefix)s отозван.") % {"prefix": token.prefix},
        contextual_actions=[
            {"action": "list_api_tokens", "label": _("← Все токены")},
        ],
    )
