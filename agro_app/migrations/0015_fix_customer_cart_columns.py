# Generated migration to fix missing foreign key columns in customer_cart table
# This migration safely adds customer_id and product_id columns matching the referenced table types

from django.db import migrations


def add_cart_columns_if_missing(apps, schema_editor):
    """
    Safely add customer_id and product_id columns to customer_cart table if they don't exist.
    Checks the actual data types of the referenced columns first.
    """
    db_alias = schema_editor.connection.alias
    
    with schema_editor.connection.cursor() as cursor:
        # Get the actual data type of customer_id in customer table
        cursor.execute("""
            SELECT DATA_TYPE, COLUMN_TYPE 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer' 
            AND COLUMN_NAME = 'customer_id'
        """)
        customer_col_info = cursor.fetchone()
        customer_data_type = customer_col_info[0] if customer_col_info else 'INT'
        customer_column_type = customer_col_info[1] if customer_col_info else 'INT'
        
        # Get the actual data type of product_id in product table
        cursor.execute("""
            SELECT DATA_TYPE, COLUMN_TYPE 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'product' 
            AND COLUMN_NAME = 'product_id'
        """)
        product_col_info = cursor.fetchone()
        product_data_type = product_col_info[0] if product_col_info else 'INT'
        product_column_type = product_col_info[1] if product_col_info else 'INT'
        
        # Check if customer_id column exists in customer_cart
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_cart' 
            AND COLUMN_NAME = 'customer_id'
        """)
        customer_col_exists = cursor.fetchone()[0] > 0
        
        # Check if product_id column exists in customer_cart
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_cart' 
            AND COLUMN_NAME = 'product_id'
        """)
        product_col_exists = cursor.fetchone()[0] > 0
        
        # Add customer_id if missing - use the exact same type as the referenced column
        if not customer_col_exists:
            # Get first customer ID as default (or use 1 if no customers exist)
            cursor.execute("""
                SELECT customer_id FROM customer LIMIT 1
            """)
            result = cursor.fetchone()
            default_customer_id = result[0] if result else 1
            
            # Use the exact column type from the referenced table
            cursor.execute(f"""
                ALTER TABLE customer_cart 
                ADD COLUMN customer_id {customer_column_type} NOT NULL DEFAULT {default_customer_id},
                ADD CONSTRAINT customer_cart_customer_fk 
                FOREIGN KEY (customer_id) REFERENCES customer(customer_id) 
                ON DELETE CASCADE
            """)
            
            # Remove default after adding column
            cursor.execute("""
                ALTER TABLE customer_cart 
                ALTER COLUMN customer_id DROP DEFAULT
            """)
        
        # Add product_id if missing - use the exact same type as the referenced column
        if not product_col_exists:
            # Get first product ID as default (or use 1 if no products exist)
            cursor.execute("""
                SELECT product_id FROM product LIMIT 1
            """)
            result = cursor.fetchone()
            default_product_id = result[0] if result else 1
            
            # Use the exact column type from the referenced table
            cursor.execute(f"""
                ALTER TABLE customer_cart 
                ADD COLUMN product_id {product_column_type} NOT NULL DEFAULT {default_product_id},
                ADD CONSTRAINT customer_cart_product_fk 
                FOREIGN KEY (product_id) REFERENCES product(product_id) 
                ON DELETE CASCADE
            """)
            
            # Remove default after adding column
            cursor.execute("""
                ALTER TABLE customer_cart 
                ALTER COLUMN product_id DROP DEFAULT
            """)
        
        # Add unique constraint if it doesn't exist
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.TABLE_CONSTRAINTS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_cart' 
            AND CONSTRAINT_NAME = 'customer_cart_customer_product_unique'
        """)
        unique_constraint_exists = cursor.fetchone()[0] > 0
        
        if not unique_constraint_exists:
            cursor.execute("""
                ALTER TABLE customer_cart 
                ADD CONSTRAINT customer_cart_customer_product_unique 
                UNIQUE (customer_id, product_id)
            """)


def reverse_add_cart_columns(apps, schema_editor):
    """Reverse migration - remove the columns"""
    with schema_editor.connection.cursor() as cursor:
        try:
            cursor.execute("""
                ALTER TABLE customer_cart 
                DROP FOREIGN KEY customer_cart_customer_fk,
                DROP FOREIGN KEY customer_cart_product_fk,
                DROP CONSTRAINT customer_cart_customer_product_unique,
                DROP COLUMN customer_id,
                DROP COLUMN product_id
            """)
        except Exception:
            # If columns don't exist, that's fine
            pass


class Migration(migrations.Migration):

    dependencies = [
        ('agro_app', '0014_alter_customer_seller_user'),
    ]

    operations = [
        migrations.RunPython(
            add_cart_columns_if_missing,
            reverse_add_cart_columns,
        ),
    ]

