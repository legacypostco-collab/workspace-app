import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0106_twofactorauth_last_totp_counter"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="activityevent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("order", "Заказ"),
                    ("rfq", "RFQ"),
                    ("pricelist", "Загрузка прайса"),
                    ("topup_confirmed", "Пополнение подтверждено"),
                    ("topup_rejected", "Пополнение отклонено"),
                    ("withdrawal_approved", "Вывод одобрен"),
                    ("withdrawal_completed", "Вывод выполнен"),
                    ("withdrawal_rejected", "Вывод отклонён"),
                ],
                db_index=True,
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="referralcode",
            name="clicks",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="referralcode",
            name="last_clicked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="ReferralAcceptance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("referrer_role", models.CharField(blank=True, max_length=32)),
                ("accepted_at", models.DateTimeField(auto_now_add=True)),
                (
                    "code",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="acceptances",
                        to="marketplace.referralcode",
                    ),
                ),
                (
                    "referred",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="referral_acceptance",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "referrer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="accepted_referrals",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-accepted_at"],
                "indexes": [
                    models.Index(
                        fields=["referrer", "-accepted_at"],
                        name="refaccept_referrer_idx",
                    ),
                ],
            },
        ),
    ]
