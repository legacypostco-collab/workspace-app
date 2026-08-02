from django.db import migrations, models


def participant_code(user_id, role):
    multiplier, offset = ((1973, 2847) if role == "seller" else (3251, 6173))
    if user_id <= 9000:
        return str(1000 + ((user_id * multiplier + offset) % 9000))
    return str(10000 + user_id)


def fill_public_party_codes(apps, schema_editor):
    UserProfile = apps.get_model("marketplace", "UserProfile")
    for profile in UserProfile.objects.all().only("id", "user_id").iterator():
        UserProfile.objects.filter(pk=profile.pk).update(
            partner_public_code=participant_code(int(profile.user_id), "seller"),
            customer_public_code=participant_code(int(profile.user_id), "buyer"),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0107_referral_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="partner_public_code",
            field=models.CharField(
                blank=True,
                editable=False,
                help_text="Постоянный обезличенный номер партнёра. Не используется для проверки доступа.",
                max_length=16,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="customer_public_code",
            field=models.CharField(
                blank=True,
                editable=False,
                help_text="Постоянный обезличенный номер заказчика. Не используется для проверки доступа.",
                max_length=16,
                null=True,
                unique=True,
            ),
        ),
        migrations.RunPython(fill_public_party_codes, migrations.RunPython.noop),
    ]
