from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0104_encrypt_two_factor_secrets"),
    ]

    operations = [
        migrations.AlterField(
            model_name="drawing",
            name="file_url",
            field=models.URLField(blank=True, max_length=500),
        ),
    ]
