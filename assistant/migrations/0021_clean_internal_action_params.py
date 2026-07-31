from django.db import migrations


def clean_internal_action_params(apps, schema_editor):
    message_model = apps.get_model("assistant", "Message")
    pending = []
    for message in (
        message_model.objects.filter(role="action")
        .only("id", "actions")
        .iterator(chunk_size=500)
    ):
        changed = False
        cleaned_actions = []
        for action in message.actions or []:
            if not isinstance(action, dict):
                cleaned_actions.append(action)
                continue
            cleaned = dict(action)
            params = cleaned.get("params")
            if isinstance(params, dict):
                filtered = {
                    key: value
                    for key, value in params.items()
                    if not str(key).startswith("_")
                }
                changed = changed or filtered != params
                cleaned["params"] = filtered
            cleaned_actions.append(cleaned)
        if changed:
            message.actions = cleaned_actions
            pending.append(message)
        if len(pending) >= 500:
            message_model.objects.bulk_update(pending, ["actions"], batch_size=500)
            pending = []
    if pending:
        message_model.objects.bulk_update(pending, ["actions"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0020_wallet_operations"),
    ]

    operations = [
        migrations.RunPython(clean_internal_action_params, migrations.RunPython.noop),
    ]
