from django.db import migrations


def normalize_liebherr_brand(apps, schema_editor):
    Brand = apps.get_model("marketplace", "Brand")
    Part = apps.get_model("marketplace", "Part")

    old = Brand.objects.filter(name="Liebherr (CE / Mining)").first()
    new = Brand.objects.filter(name="Liebherr").first()

    if old and new and old.id != new.id:
        Part.objects.filter(brand_id=old.id).update(brand_id=new.id)
        old.delete()
        brand = new
    elif old and not new:
        old.name = "Liebherr"
        old.slug = "liebherr"
        old.save(update_fields=["name", "slug"])
        brand = old
    elif new:
        if new.slug != "liebherr":
            new.slug = "liebherr"
            new.save(update_fields=["slug"])
        brand = new
    else:
        brand = Brand.objects.create(
            name="Liebherr",
            slug="liebherr",
            region="global",
            is_component_manufacturer=False,
        )

    Part.objects.filter(brand_id=brand.id).update(image_url="/static/marketplace/liebherr-logo.svg")


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0004_rfq_rfqitem"),
    ]

    operations = [
        migrations.RunPython(normalize_liebherr_brand, migrations.RunPython.noop),
    ]
