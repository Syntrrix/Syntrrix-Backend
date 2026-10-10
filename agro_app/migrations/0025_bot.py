from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("agro_app", "0024_add_payment_proof_and_note_to_customer_payment"),
    ]

    operations = [
        migrations.CreateModel(
            name="Bot",
            fields=[
                ("bot_id", models.AutoField(primary_key=True, serialize=False)),
                ("name", models.CharField(default="Bot", max_length=150)),
                ("flow", models.JSONField(blank=True, default=dict)),
                ("design", models.JSONField(blank=True, default=dict)),
                ("is_published", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "seller_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bots",
                        to="agro_app.user",
                    ),
                ),
            ],
            options={
                "db_table": "bot",
            },
        ),
        migrations.AddIndex(
            model_name="bot",
            index=models.Index(fields=["seller_user", "is_published"], name="bot_seller__7f1d39_idx"),
        ),
    ]


