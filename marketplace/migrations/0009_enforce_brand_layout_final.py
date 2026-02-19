from django.db import migrations
from django.utils.text import slugify


def forwards(apps, schema_editor):
    Brand = apps.get_model("marketplace", "Brand")
    Part = apps.get_model("marketplace", "Part")

    renames = [
        ("John Deere (Construction & Forestry)", "John Deere"),
        ("Volvo Construction Equipment", "Volvo"),
        ("Wirtgen", "Wiergten"),
        ("Bosch Rexroth", "Bosh Rexroth"),
        ("Cummins", "Cummins (двигатели для спецтехники)"),
        ("Deutz", "Deutz (двигатели для спецтехники)"),
        ("Bosch", "Bosch (топливная/гидравлика/электрика под спецтехнику)"),
        ("Atlas Copco", "Atlas Copco (буровое / компрессоры CE)"),
    ]

    for old_name, new_name in renames:
        old = Brand.objects.filter(name=old_name).first()
        if not old:
            continue
        target = Brand.objects.filter(name=new_name).first()
        if target and target.id != old.id:
            Part.objects.filter(brand_id=old.id).update(brand_id=target.id)
            old.delete()
        else:
            old.name = new_name
            old.slug = slugify(new_name)[:180] or old.slug
            old.save(update_fields=["name", "slug"])

    europe = [
        "Caterpillar",
        "Komatsu",
        "Hitachi Construction",
        "Liebherr",
        "TEREX",
        "New Holland",
        "Wiergten",
        "Iveco",
        "HBM-Nobas",
        "John Deere",
        "Volvo",
        "JCB",
        "Bobcat",
        "BOMAG",
        "Cummins (двигатели для спецтехники)",
        "Deutz (двигатели для спецтехники)",
        "Bosch (топливная/гидравлика/электрика под спецтехнику)",
        "Atlas Copco (буровое / компрессоры CE)",
        "Epiroc",
        "Ingersoll Rand",
    ]
    china = [
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
        "Sany",
    ]
    components = [
        "Bosh Rexroth",
        "Perkins",
        "Dana",
        "Carraro",
        "Denso",
        "Lincoln",
        "Berco",
        "ITR",
        "ETP",
    ]

    Brand.objects.filter(name__in=europe).update(region="europe", is_component_manufacturer=False)
    Brand.objects.filter(name__in=china).update(region="china", is_component_manufacturer=False)
    Brand.objects.filter(name__in=components).update(region="components", is_component_manufacturer=True)

    # Hide brands not in your 3 visible sections from brand page.
    Brand.objects.exclude(name__in=(europe + china + components)).update(region="korea", is_component_manufacturer=False)


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0008_apply_user_brand_taxonomy"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
