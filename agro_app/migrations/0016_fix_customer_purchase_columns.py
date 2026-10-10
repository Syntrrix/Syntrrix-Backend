# Generated migration to fix missing customer_id column in customer_purchase table

from django.db import migrations


def add_customer_purchase_columns_if_missing(apps, schema_editor):
    """
    Safely add customer_id column to customer_purchase table if it doesn't exist.
    """
    db_alias = schema_editor.connection.alias
    
    with schema_editor.connection.cursor() as cursor:
        # Get the actual data type of customer_id in customer table
        cursor.execute("""
            SELECT COLUMN_TYPE 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer' 
            AND COLUMN_NAME = 'customer_id'
        """)
        customer_col_info = cursor.fetchone()
        customer_column_type = customer_col_info[0] if customer_col_info else 'INT'
        
        # Check if customer_id column exists in customer_purchase
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_purchase' 
            AND COLUMN_NAME = 'customer_id'
        """)
        customer_col_exists = cursor.fetchone()[0] > 0
        
        # Add customer_id if missing
        if not customer_col_exists:
            # Get first customer ID as default (or use 1 if no customers exist)
            cursor.execute("""
                SELECT customer_id FROM customer LIMIT 1
            """)
            result = cursor.fetchone()
            default_customer_id = result[0] if result else 1
            
            # Use the exact column type from the referenced table
            cursor.execute(f"""
                ALTER TABLE customer_purchase 
                ADD COLUMN customer_id {customer_column_type} NOT NULL DEFAULT {default_customer_id},
                ADD CONSTRAINT customer_purchase_customer_fk 
                FOREIGN KEY (customer_id) REFERENCES customer(customer_id) 
                ON DELETE CASCADE
            """)
            
            # Remove default after adding column
            cursor.execute("""
                ALTER TABLE customer_purchase 
                ALTER COLUMN customer_id DROP DEFAULT
            """)


def reverse_add_customer_purchase_columns(apps, schema_editor):
    """Reverse migration - remove the column"""
    with schema_editor.connection.cursor() as cursor:
        try:
            cursor.execute("""
                ALTER TABLE customer_purchase 
                DROP FOREIGN KEY customer_purchase_customer_fk,
                DROP COLUMN customer_id
            """)
        except Exception:
            # If column doesn't exist, that's fine
            pass


class Migration(migrations.Migration):

    dependencies = [
        ('agro_app', '0015_fix_customer_cart_columns'),
    ]

    operations = [
        migrations.RunPython(
            add_customer_purchase_columns_if_missing,
            reverse_add_customer_purchase_columns,
        ),
    ]

