from django.db import migrations


def remove_unused_qr_tokens(apps, schema_editor):
    Order = apps.get_model("marketplace", "Order")
    OrderEvent = apps.get_model("marketplace", "OrderEvent")

    for order in Order.objects.iterator(chunk_size=500):
        meta = dict(order.logistics_meta or {})
        if "qr_token" in meta:
            meta.pop("qr_token", None)
            order.logistics_meta = meta
            order.save(update_fields=["logistics_meta"])

    for event in OrderEvent.objects.iterator(chunk_size=500):
        meta = dict(event.meta or {})
        if meta.get("kind") == "qr" and "token" in meta:
            meta.pop("token", None)
            event.meta = meta
            event.save(update_fields=["meta"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0102_remove_auth_secrets_from_chat_history"),
    ]

    operations = [
        migrations.RunPython(
            remove_unused_qr_tokens,
            migrations.RunPython.noop,
        ),
    ]
