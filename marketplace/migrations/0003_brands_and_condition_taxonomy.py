from django.db import migrations, models
import django.db.models.deletion
from django.utils.text import slugify


def forwards_seed_and_map(apps, schema_editor):
    Brand = apps.get_model("marketplace", "Brand")
    Part = apps.get_model("marketplace", "Part")
    Category = apps.get_model("marketplace", "Category")

    groups = {
        "global": [
            "Caterpillar",
            "Komatsu",
            "Hitachi Construction",
            "Liebherr (CE / Mining)",
            "Sany",
            "John Deere (Construction & Forestry)",
            "Volvo Construction Equipment",
            "JCB",
            "Bobcat",
            "BOMAG",
            "Cummins",
            "Deutz",
            "Bosch",
            "Atlas Copco",
            "Epiroc",
            "Ingersoll Rand",
        ],
        "korea": [
            "Hyundai",
        ],
        "china": [
            "XCMG",
            "FAW",
            "LiuGong",
            "Shantui",
            "Shacman",
            "SDLG",
            "Weichai",
            "Sinotruk",
            "HOWO",
            "Zoomlion",
        ],
        "europe": [
            "TEREX",
            "New Holland",
            "Wirtgen",
            "Iveco",
            "HBM-Nobas",
        ],
        "components": [
            "Bosch Rexroth",
            "Perkins",
            "Dana",
            "Carraro",
            "Denso",
            "Lincoln",
            "Berco",
            "ITR",
            "ETP",
        ],
    }

    for region, names in groups.items():
        for name in names:
            Brand.objects.get_or_create(
                slug=slugify(name)[:180] or f"brand-{abs(hash(name))}",
                defaults={
                    "name": name,
                    "region": region,
                    "is_component_manufacturer": region == "components",
                },
            )

    category_name_to_brand_id = {}
    for category in Category.objects.all():
        brand = Brand.objects.filter(name__iexact=category.name).first()
        if brand:
            category_name_to_brand_id[category.id] = brand.id

    # Condition taxonomy migration.
    Part.objects.filter(condition="new").update(condition="oem")
    Part.objects.filter(condition="used").update(condition="aftermarket")
    Part.objects.filter(condition="refurbished").update(condition="reman")

    for category_id, brand_id in category_name_to_brand_id.items():
        Part.objects.filter(category_id=category_id, brand_id__isnull=True).update(brand_id=brand_id)


def backwards_unmap(apps, schema_editor):
    Part = apps.get_model("marketplace", "Part")
    Part.objects.filter(condition="oem").update(condition="new")
    Part.objects.filter(condition="aftermarket").update(condition="used")
    Part.objects.filter(condition="reman").update(condition="refurbished")


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0002_profile_and_ownership"),
    ]

    operations = [
        migrations.CreateModel(
            name="Brand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=140, unique=True)),
                ("slug", models.SlugField(max_length=180, unique=True)),
                (
                    "region",
                    models.CharField(
                        choices=[
                            ("global", "Global"),
                            ("korea", "Korea"),
                            ("china", "China"),
                            ("europe", "Europe"),
                            ("components", "Component Manufacturer"),
                        ],
                        default="global",
                        max_length=20,
                    ),
                ),
                ("is_component_manufacturer", models.BooleanField(default=False)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="part",
            name="brand",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="parts",
                to="marketplace.brand",
            ),
        ),
        migrations.AlterField(
            model_name="part",
            name="condition",
            field=models.CharField(
                choices=[("oem", "OEM"), ("aftermarket", "Aftermarket"), ("reman", "REMAN")],
                default="oem",
                max_length=20,
            ),
        ),
        migrations.RunPython(forwards_seed_and_map, backwards_unmap),
    ]
