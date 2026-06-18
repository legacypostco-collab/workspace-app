"""Data-hygiene: деактивировать активные позиции со ОТРИЦАТЕЛЬНОЙ ценой.

QA-проверка целостности нашла активную позицию с price=-0.03 (битый импорт).
Отрицательная цена не должна попадать в матчинг/котировки. Деактивируем строго
отрицательные (price < 0) — нулевые («цена по запросу») не трогаем. Идемпотентно.
"""
from decimal import Decimal

from django.db import migrations


def deactivate_negative(apps, schema_editor):
    Part = apps.get_model("marketplace", "Part")
    Part.objects.filter(is_active=True, price__lt=Decimal("0")).update(is_active=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("marketplace", "0089_fix_incoherent_claims")]
    operations = [migrations.RunPython(deactivate_negative, noop)]
