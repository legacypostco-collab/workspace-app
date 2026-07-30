from django.db import migrations

import marketplace.encrypted_fields


def encrypt_existing_secrets(apps, schema_editor):
    TwoFactorAuth = apps.get_model("marketplace", "TwoFactorAuth")
    for two_factor in TwoFactorAuth.objects.exclude(secret="").iterator(
        chunk_size=500
    ):
        two_factor.secret = two_factor.secret
        two_factor.save(update_fields=["secret"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0103_remove_unused_qr_tokens"),
    ]

    operations = [
        migrations.AlterField(
            model_name="twofactorauth",
            name="secret",
            field=marketplace.encrypted_fields.EncryptedSecretField(
                blank=True,
                help_text="Encrypted Base32-encoded TOTP secret",
                max_length=255,
            ),
        ),
        migrations.RunPython(
            encrypt_existing_secrets,
            migrations.RunPython.noop,
        ),
    ]
