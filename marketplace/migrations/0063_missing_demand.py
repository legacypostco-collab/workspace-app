"""MissingDemand — аналитика спроса без предложения (PIVOT 2026-05-26)."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0062_operator_bonus'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MissingDemand',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('oem', models.CharField(db_index=True, max_length=128)),
                ('day', models.DateField(db_index=True)),
                ('count', models.PositiveIntegerField(default=1)),
                ('rfq_id', models.IntegerField(blank=True, null=True, help_text='ID первого RFQ где это спросили')),
                ('last_rfq_id', models.IntegerField(blank=True, null=True, help_text='ID последнего RFQ где это спросили')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('buyer', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='missing_demand_records', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-day', '-count'],
                'unique_together': {('oem', 'day')},
                'indexes': [
                    models.Index(fields=['-day', '-count'], name='missdem_day_count_idx'),
                    models.Index(fields=['oem', '-day'], name='missdem_oem_day_idx'),
                ],
            },
        ),
    ]
