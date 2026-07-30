from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0017_shared_support_conversations"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="support_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Не является обращением"),
                    ("request", "Обращение"),
                    ("complaint", "Жалоба"),
                    ("kam", "Разговор с менеджером"),
                ],
                db_index=True,
                default="",
                max_length=20,
            ),
        ),
    ]
