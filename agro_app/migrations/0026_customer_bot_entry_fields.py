from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("agro_app", "0025_bot"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="created_via_bot",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customer",
            name="source_bot",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="captured_customers",
                to="agro_app.bot",
                db_column="source_bot_id",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="bot_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]


