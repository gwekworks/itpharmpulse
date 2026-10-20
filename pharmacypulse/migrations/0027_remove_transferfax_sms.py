from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('pharmacypulse', '0026_transfer_contact_override'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='transferfax',
            name='pharmacy_phone',
        ),
        migrations.RemoveField(
            model_name='transferfax',
            name='sms_failure',
        ),
        migrations.RemoveField(
            model_name='transferfax',
            name='sms_status',
        ),
        migrations.RemoveField(
            model_name='transferfax',
            name='sms_to',
        ),
    ]
