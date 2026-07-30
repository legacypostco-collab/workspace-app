import hashlib
import hmac

from django.conf import settings
from django.db import migrations


PREFIX = "hmac_sha256$"


def hash_legacy_backup_codes(apps, schema_editor):
    two_factor_auth = apps.get_model("marketplace", "TwoFactorAuth")
    key = settings.SECRET_KEY.encode("utf-8")

    for record in two_factor_auth.objects.exclude(backup_codes="").iterator():
        codes = [
            code.strip()
            for code in (record.backup_codes or "").split(",")
            if code.strip()
        ]
        normalized = []
        changed = False
        for code in codes:
            if code.startswith(PREFIX):
                normalized.append(code)
                continue
            payload = f"{record.user_id}:{code}".encode("utf-8")
            normalized.append(
                PREFIX + hmac.new(key, payload, hashlib.sha256).hexdigest()
            )
            changed = True
        if changed:
            record.backup_codes = ",".join(normalized)
            record.save(update_fields=["backup_codes"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0099_newslettersubscriber"),
    ]

    operations = [
        migrations.RunPython(
            hash_legacy_backup_codes,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
