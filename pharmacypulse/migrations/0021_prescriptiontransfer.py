# Generated for PrescriptionTransfer (USA 21 CFR 1306 patient-initiated requests).

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('pharmacypulse', '0020_review_visit_metadata'),
    ]

    operations = [
        migrations.CreateModel(
            name='PrescriptionTransfer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('from_pharmacy_name', models.TextField(blank=True, default='')),
                ('to_pharmacy_name', models.TextField(blank=True, default='')),
                ('patient_full_name', models.TextField(blank=True, default='')),
                ('patient_dob', models.DateField(blank=True, null=True)),
                ('patient_phone', models.CharField(blank=True, default='', max_length=32)),
                ('patient_address', models.TextField(blank=True, default='')),
                ('medication_name', models.TextField(blank=True, default='')),
                ('medication_strength', models.TextField(blank=True, default='')),
                ('directions', models.TextField(blank=True, default='')),
                ('rx_number', models.CharField(blank=True, default='', max_length=64)),
                ('refills_remaining', models.IntegerField(blank=True, null=True)),
                ('last_filled_date', models.DateField(blank=True, null=True)),
                ('prescriber_name', models.TextField(blank=True, default='')),
                ('prescriber_phone', models.CharField(blank=True, default='', max_length=32)),
                ('medication_type', models.CharField(choices=[('non_controlled', 'Non-controlled'), ('schedule_iii_v', 'Schedule III–V'), ('schedule_ii', 'Schedule II')], default='non_controlled', max_length=16)),
                ('consent_given', models.BooleanField(default=False)),
                ('patient_signature', models.TextField(blank=True, default='')),
                ('status', models.TextField(default='pending')),
                ('notes', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('from_pharmacy', models.ForeignKey(blank=True, db_column='from_pharmacy_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='outgoing_transfers', to='pharmacypulse.pharmacy')),
                ('to_pharmacy', models.ForeignKey(blank=True, db_column='to_pharmacy_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='incoming_transfers', to='pharmacypulse.pharmacy')),
                ('user', models.ForeignKey(blank=True, db_column='user_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='prescription_transfers', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'prescription_transfers',
                'ordering': ['-created_at'],
            },
        ),
    ]
