from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0015_alter_knowledgechunk_embedding"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="contextual_actions",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="message",
            name="suggestions",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
