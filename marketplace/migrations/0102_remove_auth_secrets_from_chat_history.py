import re

from django.db import migrations


API_TOKEN_RE = re.compile(r"ck_live_[A-Za-z0-9_-]{20,}")
INVITE_TOKEN_RE = re.compile(
    r"(?P<prefix>[?&](?:invite_customer|join_team)=)[^&#\s\"']+"
)
SENSITIVE_KEYS = {
    "password",
    "password1",
    "password2",
    "current_password",
    "new_password",
    "otp_code",
    "secret",
    "token",
    "api_token",
    "backup_code",
    "manual_entry",
    "join_team",
    "invite_customer",
}
SENSITIVE_CODE_ACTIONS = {
    "verify_2fa",
    "disable_2fa",
    "accept_referral",
    "accept_customer_invite",
}


def sanitize_text(value):
    if not isinstance(value, str):
        return value
    if value.startswith("otpauth://"):
        return "[redacted]"
    value = API_TOKEN_RE.sub("[redacted]", value)
    return INVITE_TOKEN_RE.sub(r"\g<prefix>[redacted]", value)


def sanitize_payload(value, action_name=""):
    if isinstance(value, list):
        return [sanitize_payload(item, action_name) for item in value]
    if not isinstance(value, dict):
        return sanitize_text(value)

    current_action = str(value.get("action") or action_name)
    cleaned = {}
    for key, item in value.items():
        normalized = str(key).strip().lower()
        if normalized in SENSITIVE_KEYS:
            cleaned[key] = "[redacted]"
        elif normalized == "code" and current_action in SENSITIVE_CODE_ACTIONS:
            cleaned[key] = "[redacted]"
        else:
            cleaned[key] = sanitize_payload(item, current_action)

    title = str(cleaned.get("title") or "").lower()
    if "backup" in title or "резервн" in title:
        cleaned["items"] = []
    if str(cleaned.get("label") or "").lower() in {
        "полный токен",
        "полное значение",
    }:
        cleaned["value"] = "[redacted]"
    return cleaned


def remove_auth_secrets(apps, schema_editor):
    Message = apps.get_model("assistant", "Message")
    ActionExecution = apps.get_model("assistant", "ActionExecution")

    for message in Message.objects.iterator(chunk_size=500):
        content = sanitize_text(message.content)
        cards = sanitize_payload(message.cards)
        actions = sanitize_payload(message.actions)
        changed = []
        if content != message.content:
            message.content = content
            changed.append("content")
        if cards != message.cards:
            message.cards = cards
            changed.append("cards")
        if actions != message.actions:
            message.actions = actions
            changed.append("actions")
        if changed:
            message.save(update_fields=changed)

    for execution in ActionExecution.objects.iterator(chunk_size=500):
        response = sanitize_payload(execution.response)
        if response != execution.response:
            execution.response = response
            execution.save(update_fields=["response"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0101_sanitize_webhook_delivery_logs"),
        ("assistant", "0019_action_execution"),
    ]

    operations = [
        migrations.RunPython(
            remove_auth_secrets,
            migrations.RunPython.noop,
        ),
    ]
