from urllib.parse import urlsplit, urlunsplit

from django.db import migrations


def sanitize_webhook_logs(apps, schema_editor):
    webhook_log = apps.get_model("marketplace", "WebhookDeliveryLog")

    for record in webhook_log.objects.all().iterator():
        update_fields = []
        try:
            parsed = urlsplit(record.endpoint or "")
            hostname = parsed.hostname or ""
            if hostname:
                if ":" in hostname and not hostname.startswith("["):
                    hostname = f"[{hostname}]"
                port = f":{parsed.port}" if parsed.port else ""
                sanitized_endpoint = urlunsplit(
                    (
                        parsed.scheme,
                        f"{hostname}{port}",
                        parsed.path,
                        "",
                        "",
                    )
                )
                if sanitized_endpoint != record.endpoint:
                    record.endpoint = sanitized_endpoint
                    update_fields.append("endpoint")
        except (TypeError, ValueError):
            pass

        if record.response_body:
            record.response_body = ""
            update_fields.append("response_body")
        if record.error:
            record.error = "Webhook delivery failed."
            update_fields.append("error")
        if update_fields:
            record.save(update_fields=update_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0100_hash_legacy_2fa_backup_codes"),
    ]

    operations = [
        migrations.RunPython(
            sanitize_webhook_logs,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
