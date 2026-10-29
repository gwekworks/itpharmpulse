# SMS fallback tracking for failed fax legs.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pharmacypulse', '0023_transferfax'),
    ]

    operations = [
        migrations.AddField(
            model_name='transferfax',
            name='pharmacy_phone',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='transferfax',
            name='sms_status',
            field=models.TextField(default='unsent'),
        ),
        migrations.AddField(
            model_name='transferfax',
            name='sms_to',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='transferfax',
            name='sms_failure',
            field=models.TextField(blank=True, default=''),
        ),
    ]
