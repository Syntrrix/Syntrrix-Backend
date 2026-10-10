# Generated manually for customer-facing order reference at catalogue checkout.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agro_app", "0031_online_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="onlineorder",
            name="order_no",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Public reference generated at checkout; stored on the Data Hub row and copied here on approval.",
                max_length=40,
                null=True,
                unique=True,
            ),
        ),
    ]
