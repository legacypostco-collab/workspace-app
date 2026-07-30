from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0097_userrole"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="email_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="notification",
            name="email_claimed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="notification",
            name="email_last_error",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="notification",
            name="email_sent_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
