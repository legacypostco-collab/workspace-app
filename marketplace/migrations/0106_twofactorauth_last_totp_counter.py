from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0105_alter_drawing_file_url_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="twofactorauth",
            name="last_totp_counter",
            field=models.BigIntegerField(
                blank=True,
                help_text=(
                    "Last accepted TOTP time-step; prevents replay within "
                    "valid_window."
                ),
                null=True,
            ),
        ),
    ]
