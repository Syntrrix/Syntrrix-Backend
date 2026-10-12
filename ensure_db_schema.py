import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agro_main.settings")
django.setup()

from django.db import connection

def fix_schema():
    print("Running automatic DB schema check for AgroBridge...")
    with connection.cursor() as cursor:
        # 1. Ensure `plan` table exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS `plan` (
                `plan_id` int NOT NULL AUTO_INCREMENT,
                `name` varchar(150) NOT NULL UNIQUE,
                `features` json DEFAULT NULL,
                `description` longtext,
                `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                PRIMARY KEY (`plan_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # 2. Check if `plan_id` column exists in `agro_user`
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns 
            WHERE table_schema = DATABASE() 
            AND table_name = 'agro_user' 
            AND column_name = 'plan_id';
        """)
        has_plan_column = cursor.fetchone()[0] > 0

        if not has_plan_column:
            print("Adding missing 'plan_id' column to 'agro_user' table...")
            try:
                cursor.execute("""
                    ALTER TABLE `agro_user` 
                    ADD COLUMN `plan_id` int NULL,
                    ADD CONSTRAINT `fk_agro_user_plan` FOREIGN KEY (`plan_id`) REFERENCES `plan` (`plan_id`) ON DELETE SET NULL;
                """)
                print("Successfully added 'plan_id' column to 'agro_user'.")
            except Exception as ex:
                # Fallback without foreign key constraint if index/table issue occurs
                try:
                    cursor.execute("ALTER TABLE `agro_user` ADD COLUMN `plan_id` int NULL;")
                    print("Successfully added 'plan_id' column to 'agro_user' (no FK).")
                except Exception as ex2:
                    print(f"Column plan_id addition notice: {ex2}")

if __name__ == "__main__":
    try:
        fix_schema()
    except Exception as e:
        print(f"DB schema check warning: {e}")
