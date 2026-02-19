from django.db import migrations
from django.utils.text import slugify


def _ensure_brand(Brand, name: str, region: str, is_component: bool):
    slug = slugify(name)[:180] or f"brand-{abs(hash(name))}"
    brand = Brand.objects.filter(name=name).first()
    if brand:
        changed = False
        if brand.region != region:
            brand.region = region
            changed = True
        if brand.is_component_manufacturer != is_component:
            brand.is_component_manufacturer = is_component
            changed = True
        if brand.slug != slug:
            brand.slug = slug
            changed = True
        if changed:
            brand.save(update_fields=["region", "is_component_manufacturer", "slug"])
        return brand
    return Brand.objects.create(
        name=name,
        slug=slug,
        region=region,
        is_component_manufacturer=is_component,
    )


def forwards(apps, schema_editor):
    Brand = apps.get_model("marketplace", "Brand")

    renames = {
        "John Deere (Construction & Forestry)": "John Deere",
        "Volvo Construction Equipment": "Volvo",
        "Wirtgen": "Wiergten",
        "Bosch Rexroth": "Bosh Rexroth",
        "Cummins": "Cummins (двигатели для спецтехники)",
        "Deutz": "Deutz (двигатели для спецтехники)",
        "Bosch": "Bosch (топливная/гидравлика/электрика под спецтехнику)",
        "Atlas Copco": "Atlas Copco (буровое / компрессоры CE)",
    }

    for old_name, new_name in renames.items():
        old = Brand.objects.filter(name=old_name).first()
        if not old:
            continue
        target = Brand.objects.filter(name=new_name).first()
        if target and target.id != old.id:
            # Merge old into existing target.
            apps.get_model("marketplace", "Part").objects.filter(brand_id=old.id).update(brand_id=target.id)
            old.delete()
            continue
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

    for name in europe:
        _ensure_brand(Brand, name=name, region="europe", is_component=False)
    for name in china:
        _ensure_brand(Brand, name=name, region="china", is_component=False)
    for name in components:
        _ensure_brand(Brand, name=name, region="components", is_component=True)

    # Remove deprecated region sections from UI by moving leftovers to europe.
    Brand.objects.filter(region__in=["global", "korea"]).update(region="europe")


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0007_move_sany_to_china"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
