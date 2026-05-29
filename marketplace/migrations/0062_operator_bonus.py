"""Operator bonus system: assigned_operator on Order + OperatorBonusLine model.

ТЗ: единая комиссия за закрытую сделку, FOB 0.4% / CIP 0.5% / DDP 0.7% от
стоимости товара, min $50 / max $5,000. Платится через 14 дней после release.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0061_claim_escalated_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='assigned_operator',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=models.SET_NULL,
                related_name='assigned_orders',
                to=settings.AUTH_USER_MODEL,
                help_text='Оператор, ведущий сделку (получает бонус по закрытию)',
            ),
        ),
        migrations.CreateModel(
            name='OperatorBonusLine',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('basis', models.CharField(choices=[('FOB','FOB'),('CIP','CIP'),('DDP','DDP')], max_length=10)),
                ('base_amount', models.DecimalField(decimal_places=2, max_digits=14, help_text='Стоимость товара (база начисления)')),
                ('rate_pct', models.DecimalField(decimal_places=2, max_digits=5, help_text='% применённый к base_amount')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14, help_text='Итоговая сумма бонуса (USD), с учётом min/max')),
                ('currency', models.CharField(default='USD', max_length=10)),
                ('status', models.CharField(
                    choices=[('pending','Холд (14 дней)'),('released','Зачислено'),('withheld','Удержано (вина оператора)'),('reduced','−50% (вина оператора)')],
                    default='pending', max_length=20)),
                ('note', models.CharField(blank=True, max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('release_at', models.DateTimeField(blank=True, null=True, help_text='Когда выйти из холда (created_at + 14 дней)')),
                ('released_at', models.DateTimeField(blank=True, null=True)),
                ('operator', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='bonus_lines', to=settings.AUTH_USER_MODEL)),
                ('order', models.OneToOneField(on_delete=models.deletion.CASCADE, related_name='operator_bonus', to='marketplace.order')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['operator', '-created_at'], name='opbon_op_created_idx'),
                    models.Index(fields=['status', 'release_at'], name='opbon_status_release_idx'),
                ],
            },
        ),
    ]
