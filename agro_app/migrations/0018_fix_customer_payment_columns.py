# Generated migration to fix missing customer_purchase_id column in customer_payment table

from django.db import migrations


def add_customer_payment_columns_if_missing(apps, schema_editor):
    """
    Safely add customer_purchase_id column to customer_payment table if it doesn't exist.
    """
    db_alias = schema_editor.connection.alias
    
    with schema_editor.connection.cursor() as cursor:
        # Get the actual data type of purchase_id in customer_purchase table
        cursor.execute("""
            SELECT COLUMN_TYPE 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_purchase' 
            AND COLUMN_NAME = 'purchase_id'
        """)
        purchase_col_info = cursor.fetchone()
        purchase_column_type = purchase_col_info[0] if purchase_col_info else 'INT'
        
        # Check if customer_purchase_id column exists in customer_payment
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_payment' 
            AND COLUMN_NAME = 'customer_purchase_id'
        """)
        purchase_col_exists = cursor.fetchone()[0] > 0
        
        # Add customer_purchase_id if missing
        if not purchase_col_exists:
            # Get first purchase ID as default (or use 1 if no purchases exist)
            cursor.execute("""
                SELECT purchase_id FROM customer_purchase LIMIT 1
            """)
            result = cursor.fetchone()
            default_purchase_id = result[0] if result else 1
            
            # Use the exact column type from the referenced table
            cursor.execute(f"""
                ALTER TABLE customer_payment 
                ADD COLUMN customer_purchase_id {purchase_column_type} NOT NULL DEFAULT {default_purchase_id},
                ADD CONSTRAINT customer_payment_purchase_fk 
                FOREIGN KEY (customer_purchase_id) REFERENCES customer_purchase(purchase_id) 
                ON DELETE CASCADE
            """)
            
            # Remove default after adding column
            cursor.execute("""
                ALTER TABLE customer_payment 
                ALTER COLUMN customer_purchase_id DROP DEFAULT
            """)


def reverse_add_customer_payment_columns(apps, schema_editor):
    """Reverse migration - remove the column"""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_payment' 
            AND COLUMN_NAME = 'customer_purchase_id'
        """)
        col_exists = cursor.fetchone()[0] > 0
        
        if col_exists:
            # Drop foreign key constraint first
            cursor.execute("""
                ALTER TABLE customer_payment 
                DROP FOREIGN KEY customer_payment_purchase_fk
            """)
            # Then drop the column
            cursor.execute("""
                ALTER TABLE customer_payment 
                DROP COLUMN customer_purchase_id
            """)


class Migration(migrations.Migration):

    dependencies = [
        ('agro_app', '0017_fix_customer_purchase_item_columns'),
    ]

    operations = [
        migrations.RunPython(
            add_customer_payment_columns_if_missing,
            reverse_add_customer_payment_columns
        ),
    ]

