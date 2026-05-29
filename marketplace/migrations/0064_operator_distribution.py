"""Operator-supplier distribution (PIVOT 2026-05-27).

UserProfile.assigned_operator — оператор ВЭД, ведущий поставщика (1:1).
Order.parent_order + is_sub_order — разбиение заказа по операторам.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0063_missing_demand'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='assigned_operator',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='managed_suppliers',
                to=settings.AUTH_USER_MODEL,
                help_text='Оператор ВЭД, ведущий этого поставщика (1:1)',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='parent_order',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='sub_orders',
                to='marketplace.order',
                help_text='Оригинальный заказ если это sub-order (видим покупателю)',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='is_sub_order',
            field=models.BooleanField(
                default=False,
                help_text='True если этот Order — часть разбитого по операторам заказа',
            ),
        ),
    ]
