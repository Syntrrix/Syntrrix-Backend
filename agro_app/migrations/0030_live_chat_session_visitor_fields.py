from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agro_app", "0029_rename_bot_seller__7f1d39_idx_bot_seller__e1fb05_idx_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="livechatsession",
            name="customer",
            field=models.OneToOneField(
                blank=True,
                db_column="customer_id",
                null=True,
                on_delete=models.SET_NULL,
                related_name="live_chat_session",
                to="agro_app.customer",
            ),
        ),
        migrations.AddField(
            model_name="livechatsession",
            name="visitor_email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
        migrations.AddField(
            model_name="livechatsession",
            name="visitor_mobile_number",
            field=models.CharField(blank=True, default="", max_length=30),
        ),
        migrations.AddField(
            model_name="livechatsession",
            name="visitor_name",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.AddField(
            model_name="livechatsession",
            name="visitor_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

