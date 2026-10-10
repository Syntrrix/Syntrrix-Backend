from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("agro_app", "0026_customer_bot_entry_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="LiveChatSession",
            fields=[
                ("session_id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "public_token",
                    models.CharField(
                        db_index=True,
                        default=uuid.uuid4,
                        max_length=64,
                        unique=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "seller_user",
                    models.ForeignKey(
                        db_column="seller_user_id",
                        limit_choices_to={"role_id": 3},
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="live_chat_sessions_as_seller",
                        to="agro_app.user",
                    ),
                ),
                (
                    "customer",
                    models.OneToOneField(
                        db_column="customer_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="live_chat_session",
                        to="agro_app.customer",
                    ),
                ),
                (
                    "source_bot",
                    models.ForeignKey(
                        blank=True,
                        db_column="source_bot_id",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="live_chat_sessions",
                        to="agro_app.bot",
                    ),
                ),
            ],
            options={
                "db_table": "live_chat_session",
            },
        ),
        migrations.CreateModel(
            name="LiveChatMessage",
            fields=[
                ("message_id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "sender_type",
                    models.CharField(
                        choices=[("seller", "Seller"), ("customer", "Customer")],
                        max_length=20,
                    ),
                ),
                ("message", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "session",
                    models.ForeignKey(
                        db_column="session_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="agro_app.livechatsession",
                    ),
                ),
            ],
            options={
                "db_table": "live_chat_message",
            },
        ),
        migrations.AddIndex(
            model_name="livechatsession",
            index=models.Index(fields=["seller_user", "created_at"], name="livechat_seller__created_idx"),
        ),
        migrations.AddIndex(
            model_name="livechatsession",
            index=models.Index(fields=["customer", "created_at"], name="livechat_customer__created_idx"),
        ),
        migrations.AddIndex(
            model_name="livechatmessage",
            index=models.Index(fields=["session", "created_at"], name="livechatmsg_session__created_idx"),
        ),
        migrations.AddIndex(
            model_name="livechatmessage",
            index=models.Index(fields=["sender_type", "created_at"], name="livechatmsg_sender__created_idx"),
        ),
    ]


