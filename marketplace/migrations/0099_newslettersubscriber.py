from django.db import migrations, models


def invalidate_plaintext_magic_links(apps, schema_editor):
    apps.get_model("marketplace", "MagicLinkToken").objects.all().delete()


def invalidate_plaintext_invites(apps, schema_editor):
    apps.get_model("marketplace", "TeamMember").objects.exclude(
        invite_token=""
    ).update(invite_token="")
    apps.get_model("marketplace", "Customer").objects.exclude(
        invite_token=""
    ).update(invite_token="")


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0098_notification_email_delivery"),
    ]

    operations = [
        migrations.CreateModel(
            name="NewsletterSubscriber",
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
                ("email", models.EmailField(max_length=254, unique=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Подписчик рассылки",
                "verbose_name_plural": "Подписчики рассылки",
                "ordering": ["-created_at"],
            },
        ),
        migrations.RunPython(
            invalidate_plaintext_magic_links,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            invalidate_plaintext_invites,
            migrations.RunPython.noop,
        ),
    ]
