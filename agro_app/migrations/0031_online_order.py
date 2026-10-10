from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("agro_app", "0030_live_chat_session_visitor_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="OnlineOrder",
            fields=[
                ("order_id", models.AutoField(primary_key=True, serialize=False)),
                ("data_hub_table_id", models.CharField(blank=True, default="", max_length=64)),
                ("data_hub_row_id", models.CharField(blank=True, default="", max_length=64)),
                ("customer_name", models.CharField(blank=True, default="", max_length=150)),
                ("customer_phone", models.CharField(blank=True, default="", max_length=30)),
                ("customer_email", models.EmailField(blank=True, default="", max_length=254)),
                ("customer_address", models.TextField(blank=True, default="")),
                ("total_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")],
                        default="pending",
                        max_length=20,
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
                        related_name="online_orders",
                        to="agro_app.user",
                    ),
                ),
                (
                    "source_bot",
                    models.ForeignKey(
                        blank=True,
                        db_column="source_bot_id",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="online_orders",
                        to="agro_app.bot",
                    ),
                ),
            ],
            options={
                "db_table": "online_order",
            },
        ),
        migrations.CreateModel(
            name="OnlineOrderItem",
            fields=[
                ("item_id", models.AutoField(primary_key=True, serialize=False)),
                ("quantity", models.PositiveIntegerField(default=1)),
                ("unit_price", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("line_total", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                (
                    "order",
                    models.ForeignKey(
                        db_column="order_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="agro_app.onlineorder",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        db_column="product_id",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="online_order_items",
                        to="agro_app.product",
                    ),
                ),
            ],
            options={
                "db_table": "online_order_item",
            },
        ),
        migrations.AddIndex(
            model_name="onlineorder",
            index=models.Index(fields=["seller_user", "created_at"], name="online_order_seller__a1c3f1_idx"),
        ),
        migrations.AddIndex(
            model_name="onlineorder",
            index=models.Index(fields=["status", "created_at"], name="online_order_status__e1d5c2_idx"),
        ),
        migrations.AddIndex(
            model_name="onlineorder",
            index=models.Index(fields=["data_hub_table_id", "data_hub_row_id"], name="online_order_data_hu__7a9e10_idx"),
        ),
        migrations.AddIndex(
            model_name="onlineorderitem",
            index=models.Index(fields=["order"], name="online_order_item_order__3b7c21_idx"),
        ),
        migrations.AddIndex(
            model_name="onlineorderitem",
            index=models.Index(fields=["product"], name="online_order_item_product__6f11c8_idx"),
        ),
    ]

