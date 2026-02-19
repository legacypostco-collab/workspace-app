from django.db import migrations


def rebalance_regions(apps, schema_editor):
    Brand = apps.get_model("marketplace", "Brand")

    move_to_europe = [
        "Atlas Copco",
        "BOMAG",
        "Bobcat",
        "Bosch",
        "Caterpillar",
        "Cummins",
        "Deutz",
        "Epiroc",
        "Hitachi Construction",
        "Ingersoll Rand",
        "JCB",
        "John Deere (Construction & Forestry)",
        "Komatsu",
        "Liebherr",
        "Volvo Construction Equipment",
    ]

    Brand.objects.filter(name__in=move_to_europe).update(region="europe")
    Brand.objects.filter(name="Sany").update(region="global")


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0005_normalize_liebherr_brand"),
    ]

    operations = [
        migrations.RunPython(rebalance_regions, migrations.RunPython.noop),
    ]
