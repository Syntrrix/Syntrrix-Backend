# Generated manually

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("agro_app", "0039_livechatsession_is_manual_pipeline_lead"),
    ]

    operations = [
        migrations.CreateModel(
            name="WhatsAppConfig",
            fields=[
                ("whatsapp_config_id", models.AutoField(primary_key=True, serialize=False)),
                ("phone_number_id", models.CharField(blank=True, default="", max_length=100)),
                ("whatsapp_business_account_id", models.CharField(blank=True, default="", max_length=100)),
                ("access_token", models.TextField(blank=True, default="")),
                ("webhook_verify_token", models.CharField(blank=True, default="", max_length=100)),
                ("is_connected", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "seller_user",
                    models.OneToOneField(
                        db_column="seller_user_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="whatsapp_config",
                        to="agro_app.user",
                    ),
                ),
            ],
            options={
                "db_table": "whatsapp_config",
            },
        ),
        migrations.CreateModel(
            name="WhatsAppChatSession",
            fields=[
                ("whatsapp_session_id", models.AutoField(primary_key=True, serialize=False)),
                ("customer_phone", models.CharField(db_index=True, max_length=30)),
                ("customer_name", models.CharField(blank=True, default="", max_length=150)),
                ("current_node_id", models.CharField(blank=True, max_length=100, null=True)),
                ("collected_data", models.JSONField(blank=True, default=dict)),
                ("is_submitted", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "live_chat_session",
                    models.ForeignKey(
                        blank=True,
                        db_column="live_chat_session_id",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="whatsapp_chat_session",
                        to="agro_app.livechatsession",
                    ),
                ),
                (
                    "seller_user",
                    models.ForeignKey(
                        db_column="seller_user_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="whatsapp_chat_sessions",
                        to="agro_app.user",
                    ),
                ),
            ],
            options={
                "db_table": "whatsapp_chat_session",
            },
        ),
        migrations.AddIndex(
            model_name="whatsappchatsession",
            index=models.Index(fields=["customer_phone"], name="whatsapp_ch_custome_c0cfb4_idx"),
        ),
        migrations.AddIndex(
            model_name="whatsappchatsession",
            index=models.Index(fields=["seller_user", "customer_phone"], name="whatsapp_ch_seller__ee2761_idx"),
        ),
    ]
