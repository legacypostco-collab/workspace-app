import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0018_conversation_support_kind"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ActionExecution",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("operation_id", models.UUIDField()),
                ("action", models.CharField(max_length=100)),
                ("response", models.JSONField(blank=True, default=dict)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="assistant_action_executions",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "indexes": [models.Index(fields=["user", "-created_at"], name="assistant_a_user_id_05ae2b_idx")],
                "constraints": [models.UniqueConstraint(fields=("user", "operation_id"), name="uniq_action_operation_per_user")],
            },
        ),
    ]
