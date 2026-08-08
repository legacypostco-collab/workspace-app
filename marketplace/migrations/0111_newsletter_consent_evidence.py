from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0110_userconsent"),
    ]

    operations = [
        migrations.AddField(
            model_name="newslettersubscriber",
            name="consent_source",
            field=models.CharField(default="public_api", max_length=32),
        ),
        migrations.AddField(
            model_name="newslettersubscriber",
            name="consent_version",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="newslettersubscriber",
            name="consented_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
