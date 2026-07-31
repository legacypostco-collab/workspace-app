import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0019_action_execution"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="wallettx",
            name="kind",
            field=models.CharField(
                choices=[
                    ("topup", "Пополнение"),
                    ("debit", "Списание"),
                    ("refund", "Возврат"),
                    ("escrow_hold", "Эскроу-холд"),
                    ("escrow_release", "Эскроу → продавцу"),
                    ("escrow_refund", "Эскроу → возврат"),
                    ("transfer_out", "Внутренний перевод: списание"),
                    ("transfer_in", "Внутренний перевод: зачисление"),
                    ("withdrawal_hold", "Вывод: сумма зарезервирована"),
                    ("withdrawal_refund", "Вывод: возврат резерва"),
                ],
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="WalletTransfer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("currency", models.CharField(default="USD", max_length=10)),
                ("status", models.CharField(choices=[("pending", "Ожидает подтверждения"), ("completed", "Выполнен"), ("cancelled", "Отменён"), ("expired", "Истёк")], db_index=True, default="pending", max_length=16)),
                ("reference_code", models.CharField(db_index=True, max_length=24, unique=True)),
                ("note", models.CharField(blank=True, max_length=200)),
                ("expires_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="incoming_transfers", to="assistant.wallet")),
                ("sender", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="outgoing_transfers", to="assistant.wallet")),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["sender", "-created_at"], name="wallet_transfer_sender_idx"),
                    models.Index(fields=["recipient", "-created_at"], name="wallet_transfer_recipient_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="WalletWithdrawalRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("currency", models.CharField(default="USD", max_length=10)),
                ("status", models.CharField(choices=[("pending", "На проверке"), ("approved", "Одобрена"), ("completed", "Выплачена"), ("rejected", "Отклонена"), ("cancelled", "Отменена")], db_index=True, default="pending", max_length=16)),
                ("reference_code", models.CharField(db_index=True, max_length=24, unique=True)),
                ("bank_name", models.CharField(blank=True, max_length=200)),
                ("bank_account_last4", models.CharField(blank=True, max_length=4)),
                ("user_note", models.CharField(blank=True, max_length=300)),
                ("operator_note", models.CharField(blank=True, max_length=300)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_withdrawals", to=settings.AUTH_USER_MODEL)),
                ("wallet", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="withdrawal_requests", to="assistant.wallet")),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["status", "-created_at"], name="withdrawal_status_created_idx"),
                    models.Index(fields=["wallet", "-created_at"], name="withdrawal_wallet_created_idx"),
                ],
            },
        ),
    ]
