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
import logging
import secrets

from django.utils import timezone

from .actions import ActionResult, register

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_token(prefix: str = "ck_live_") -> tuple[str, str]:
    """Возвращает (full_token, prefix_for_ui). Полный токен виден ОДИН раз."""
    raw = secrets.token_urlsafe(32)
    full = prefix + raw
    return full, full[:12] + "…"


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
        return ActionResult(text="⚠️ pyotp не установлен. pip install pyotp.")

    twofa, _ = TwoFactorAuth.objects.get_or_create(user=user)
    if twofa.enabled:
        return ActionResult(
            text="🔐 2FA уже включён. Выключить можно через disable_2fa.",
            contextual_actions=[
                {"action": "disable_2fa", "label": "🔓 Выключить 2FA"},
            ],
        )

    # Новый secret каждый раз когда юзер запросил setup
    secret = pyotp.random_base32()
    twofa.secret = secret
    # Backup codes — 8 одноразовых, разделённых запятой
    backup = [secrets.token_hex(4) for _ in range(8)]
    twofa.backup_codes = ",".join(backup)
    twofa.save(update_fields=["secret", "backup_codes"])

    issuer = "Consolidator"
    label = user.email or user.username
    otpauth_url = pyotp.totp.TOTP(secret).provisioning_uri(name=label, issuer_name=issuer)

    # QR-URL через qrserver.com (publicly hosted, без сторонних библиотек)
    from urllib.parse import quote
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?data={quote(otpauth_url)}&size=240x240"

    return ActionResult(
        text=(
            f"🔐 Setup 2FA · отсканируйте QR в Google Authenticator / Authy / 1Password.\n"
            f"После добавления — введите 6-значный код из приложения через verify_2fa."
        ),
        cards=[
            {"type": "qr", "data": {
                "title": "🔐 TOTP setup",
                "qr_url": qr_url,
                "subtitle": f"Issuer: {issuer} · Account: {label}",
                "manual_entry": secret,
            }},
            {"type": "list", "data": {
                "title": "🔑 Backup-коды (одноразовые)",
                "items": [{"title": code, "subtitle": "сохраните в надёжное место"}
                          for code in backup],
            }},
        ],
        actions=[
            {"action": "verify_2fa", "label": "✓ Ввести код из приложения"},
        ],
    )


@register("verify_2fa")
def verify_2fa(params, user, role):
    """Подтвердить OTP-код и активировать 2FA."""
    from marketplace.models import TwoFactorAuth
    try:
        import pyotp
    except ImportError:
        return ActionResult(text="⚠️ pyotp не установлен.")

    twofa = TwoFactorAuth.objects.filter(user=user).first()
    if not twofa or not twofa.secret:
        return ActionResult(
            text="Сначала пройдите setup_2fa.",
            actions=[{"action": "setup_2fa", "label": "🔐 Запустить setup"}],
        )

    code = (params.get("code") or "").strip()
    confirmed = bool(params.get("confirmed"))
    if not confirmed or not code:
        return ActionResult(
            text="🔐 Введите 6-значный код из вашего authenticator-приложения.",
            cards=[{"type": "form", "data": {
                "title": "🔐 Подтверждение 2FA",
                "submit_action": "verify_2fa",
                "fields": [
                    {"name": "code", "label": "OTP-код (6 цифр)", "required": True},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    totp = pyotp.TOTP(twofa.secret)
    if not totp.verify(code, valid_window=1):
        return ActionResult(text="❌ Код неверный или устарел. Попробуйте ещё раз.")

    twofa.enabled = True
    twofa.enabled_at = timezone.now()
    twofa.save(update_fields=["enabled", "enabled_at"])
    return ActionResult(
        text="✓ 2FA активирован! При входе или критичных платежах потребуется код из приложения.",
        contextual_actions=[
            {"action": "notif_prefs", "label": "🔔 Настройки уведомлений"},
        ],
    )


@register("disable_2fa")
def disable_2fa(params, user, role):
    """Выключить 2FA — требует подтверждения через OTP."""
    from marketplace.models import TwoFactorAuth
    try:
        import pyotp
    except ImportError:
        return ActionResult(text="⚠️ pyotp не установлен.")

    twofa = TwoFactorAuth.objects.filter(user=user).first()
    if not twofa or not twofa.enabled:
        return ActionResult(text="2FA не активирован.")

    code = (params.get("code") or "").strip()
    confirmed = bool(params.get("confirmed"))
    if not confirmed or not code:
        return ActionResult(
            text="🔓 Подтвердите выключение 2FA вашим OTP-кодом.",
            cards=[{"type": "form", "data": {
                "title": "🔓 Выключить 2FA",
                "submit_action": "disable_2fa",
                "fields": [
                    {"name": "code", "label": "OTP-код для подтверждения", "required": True},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    totp = pyotp.TOTP(twofa.secret)
    if not totp.verify(code, valid_window=1):
        return ActionResult(text="❌ Код неверный.")

    twofa.enabled = False
    twofa.secret = ""
    twofa.backup_codes = ""
    twofa.save(update_fields=["enabled", "secret", "backup_codes"])
    return ActionResult(text="✓ 2FA выключен.")


# ══════════════════════════════════════════════════════════
# 2. API tokens
# ══════════════════════════════════════════════════════════

@register("create_api_token")
def create_api_token(params, user, role):
    """Сгенерировать API-токен. Полный токен виден один раз."""
    from marketplace.models import ApiToken
    label = (params.get("label") or "").strip()
    permissions = (params.get("permissions") or "read").strip().lower()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not label:
        return ActionResult(
            text="🔑 Создать API-токен",
            cards=[{"type": "form", "data": {
                "title": "🔑 Новый API-токен",
                "submit_action": "create_api_token",
                "fields": [
                    {"name": "label", "label": "Название (например, 'CI deploy')", "required": True},
                    {"name": "permissions", "label": "Разрешения",
                     "type": "select",
                     "options": [
                         {"value": "read",       "label": "read · только чтение"},
                         {"value": "read,write", "label": "read+write · стандарт"},
                         {"value": "read,write,admin", "label": "admin · полный доступ"},
                     ],
                     "value": "read,write"},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    full, prefix = _generate_token()
    token = ApiToken.objects.create(
        user=user, label=label[:80],
        prefix=prefix, hashed_token=_hash_token(full),
        permissions=permissions,
    )
    return ActionResult(
        text=(
            f"✓ Токен создан · ID #{token.id}\n\n"
            f"⚠️ Сохраните токен — больше не увидите:\n"
            f"`{full}`\n\n"
            f"Использование: `Authorization: Bearer {full}` в HTTP-заголовке."
        ),
        cards=[{"type": "draft", "data": {
            "title": f"🔑 API-токен · {label}",
            "rows": [
                {"label": "Префикс", "value": prefix},
                {"label": "Permissions", "value": permissions, "primary": True},
                {"label": "Полный токен", "value": full, "primary": True},
            ],
            "warnings": ["Токен показывается ОДИН раз. Скопируйте сейчас."],
            "confirm_label": "—",
        }}],
        contextual_actions=[
            {"action": "list_api_tokens", "label": "📋 Все токены"},
        ],
    )


@register("list_api_tokens")
def list_api_tokens(params, user, role):
    """Список активных и отозванных токенов."""
    from marketplace.models import ApiToken
    tokens = list(ApiToken.objects.filter(user=user).order_by("-created_at")[:20])
    if not tokens:
        return ActionResult(
            text="🔑 У вас ещё нет API-токенов.",
            actions=[{"action": "create_api_token", "label": "➕ Создать"}],
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
        text=f"🔑 У вас {len(tokens)} API-токенов.",
        cards=[{"type": "list", "data": {"title": "🔑 API-токены", "items": rows}}],
        contextual_actions=[
            {"action": "create_api_token", "label": "➕ Создать новый"},
        ],
    )


@register("revoke_api_token")
def revoke_api_token(params, user, role):
    from marketplace.models import ApiToken
    try:
        token = ApiToken.objects.get(id=int(params.get("token_id") or 0), user=user)
    except (ApiToken.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Токен не найден.")
    if not token.is_active:
        return ActionResult(text=f"Токен {token.prefix} уже отозван.")
    if not bool(params.get("confirmed")):
        return ActionResult(
            text=f"Отозвать токен {token.label}?",
            cards=[{"type": "draft", "data": {
                "title": f"🚫 Отозвать токен · {token.label}",
                "rows": [
                    {"label": "Префикс", "value": token.prefix, "primary": True},
                    {"label": "Permissions", "value": token.permissions},
                ],
                "warnings": ["Все интеграции, использующие этот токен, перестанут работать."],
                "confirm_action": "revoke_api_token",
                "confirm_label": "🚫 Отозвать",
                "confirm_params": {"token_id": token.id, "confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])
    return ActionResult(
        text=f"✓ Токен {token.prefix} отозван.",
        contextual_actions=[
            {"action": "list_api_tokens", "label": "← Все токены"},
        ],
    )
