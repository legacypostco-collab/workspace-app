from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("assistant", "0016_message_contextual_actions_message_suggestions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="conversation",
            name="role",
            field=models.CharField(
                choices=[
                    ("buyer", "Покупатель"),
                    ("seller", "Поставщик"),
                    ("operator_logist", "Логист"),
                    ("operator_customs", "Таможенный брокер"),
                    ("operator_payment", "Платёжный агент"),
                    ("operator_manager", "Менеджер по продажам"),
                    ("operator", "Оператор"),
                    ("admin", "Администратор"),
                ],
                default="buyer",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="conversation",
            name="assigned_operator",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_support_conversations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="conversation",
            name="support_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Не является обращением"),
                    ("open", "Открыто"),
                    ("waiting_user", "Ожидает пользователя"),
                    ("waiting_operator", "Ожидает оператора"),
                    ("closed", "Закрыто"),
                ],
                db_index=True,
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="message",
            name="sender",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assistant_messages",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="ConversationParticipant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[
                    ("buyer", "Покупатель"),
                    ("seller", "Поставщик"),
                    ("operator_logist", "Логист"),
                    ("operator_customs", "Таможенный брокер"),
                    ("operator_payment", "Платёжный агент"),
                    ("operator_manager", "Менеджер по продажам"),
                    ("operator", "Оператор"),
                    ("admin", "Администратор"),
                ], max_length=30)),
                ("joined_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="participant_links", to="assistant.conversation")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assistant_conversation_links", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="conversationparticipant",
            constraint=models.UniqueConstraint(fields=("conversation", "user", "role"), name="uniq_conversation_participant_role"),
        ),
        migrations.AddIndex(
            model_name="conversationparticipant",
            index=models.Index(fields=["user", "role", "conversation"], name="assistant_c_user_id_d2569e_idx"),
        ),
    ]
