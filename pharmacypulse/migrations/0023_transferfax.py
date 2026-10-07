# Tracks outbound EffyMobile fax legs per prescription transfer.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('pharmacypulse', '0022_pharmacy_fax_number'),
    ]

    operations = [
        migrations.CreateModel(
            name='TransferFax',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('recipient_kind', models.CharField(default='to_pharmacy', max_length=16)),
                ('pharmacy_name', models.TextField(blank=True, default='')),
                ('to_number', models.CharField(blank=True, default='', max_length=32)),
                ('media_url', models.TextField(blank=True, default='')),
                ('effy_id', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('fax_id', models.CharField(blank=True, default='', max_length=64)),
                ('status', models.TextField(default='pending')),
                ('page_count', models.IntegerField(blank=True, null=True)),
                ('failure_reason', models.TextField(blank=True, default='')),
                ('preview_url', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('transfer', models.ForeignKey(db_column='transfer_id', on_delete=django.db.models.deletion.CASCADE, related_name='faxes', to='pharmacypulse.prescriptiontransfer')),
            ],
            options={
                'db_table': 'transfer_faxes',
                'ordering': ['-created_at'],
            },
        ),
    ]
