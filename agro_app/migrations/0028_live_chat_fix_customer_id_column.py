from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("agro_app", "0027_live_chat"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                SET @col_exists := (
                    SELECT COUNT(*)
                    FROM information_schema.columns
                    WHERE table_schema = DATABASE()
                      AND table_name = 'live_chat_session'
                      AND column_name = 'customer_id'
                );
                SET @sql := IF(@col_exists = 0,
                    'ALTER TABLE live_chat_session ADD COLUMN customer_id INT NULL',
                    'SELECT 1'
                );
                PREPARE stmt FROM @sql;
                EXECUTE stmt;
                DEALLOCATE PREPARE stmt;
            """,
            reverse_sql="SELECT 1;",
        ),
    ]

