# Adds fax_number to Pharmacy + PharmacyPublishable for transfer checks.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pharmacypulse', '0021_prescriptiontransfer'),
    ]

    operations = [
        migrations.AddField(
            model_name='pharmacy',
            name='fax_number',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='pharmacypublishable',
            name='fax_number',
            field=models.TextField(blank=True, null=True),
        ),
    ]
