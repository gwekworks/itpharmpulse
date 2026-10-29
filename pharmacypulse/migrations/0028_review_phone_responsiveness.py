from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pharmacypulse", "0027_remove_transferfax_sms"),
    ]

    operations = [
        migrations.AddField(
            model_name="review",
            name="phone_responsiveness",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
