# MySQL: keep a real `order_no` column right after `status` (not mixed into other columns).

from django.db import migrations


def mysql_place_order_no_after_status(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor != "mysql":
        return
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'online_order'
              AND COLUMN_NAME = 'order_no'
            """
        )
        exists = cursor.fetchone()[0] > 0
        if exists:
            cursor.execute(
                "ALTER TABLE `online_order` MODIFY COLUMN `order_no` VARCHAR(40) NULL DEFAULT NULL AFTER `status`"
            )
            return
        cursor.execute(
            "ALTER TABLE `online_order` ADD COLUMN `order_no` VARCHAR(40) NULL DEFAULT NULL AFTER `status`"
        )
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.statistics
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'online_order'
              AND COLUMN_NAME = 'order_no'
              AND NON_UNIQUE = 0
              AND INDEX_NAME != 'PRIMARY'
            """
        )
        if cursor.fetchone()[0] == 0:
            try:
                cursor.execute(
                    "ALTER TABLE `online_order` ADD UNIQUE KEY `online_order_order_no_uniq` (`order_no`)"
                )
            except Exception:
                pass


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("agro_app", "0032_onlineorder_order_no"),
    ]

    operations = [
        migrations.RunPython(mysql_place_order_no_after_status, noop_reverse),
    ]
