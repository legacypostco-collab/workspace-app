from django.db import migrations


def move_sany_to_china(apps, schema_editor):
    Brand = apps.get_model("marketplace", "Brand")
    Brand.objects.filter(name="Sany").update(region="china")


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0006_rebalance_brand_regions"),
    ]

    operations = [
        migrations.RunPython(move_sany_to_china, migrations.RunPython.noop),
    ]
