# Generated migration to fix missing foreign key columns in customer_purchase_item table

from django.db import migrations


def add_customer_purchase_item_columns_if_missing(apps, schema_editor):
    """
    Safely add customer_purchase_id and product_id columns to customer_purchase_item table if they don't exist.
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
        
        # Get the actual data type of product_id in product table
        cursor.execute("""
            SELECT COLUMN_TYPE 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'product' 
            AND COLUMN_NAME = 'product_id'
        """)
        product_col_info = cursor.fetchone()
        product_column_type = product_col_info[0] if product_col_info else 'INT'
        
        # Check if customer_purchase_id column exists in customer_purchase_item
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_purchase_item' 
            AND COLUMN_NAME = 'customer_purchase_id'
        """)
        purchase_col_exists = cursor.fetchone()[0] > 0
        
        # Check if product_id column exists in customer_purchase_item
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'customer_purchase_item' 
            AND COLUMN_NAME = 'product_id'
        """)
        product_col_exists = cursor.fetchone()[0] > 0
        
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
                ALTER TABLE customer_purchase_item 
                ADD COLUMN customer_purchase_id {purchase_column_type} NOT NULL DEFAULT {default_purchase_id},
                ADD CONSTRAINT customer_purchase_item_purchase_fk 
                FOREIGN KEY (customer_purchase_id) REFERENCES customer_purchase(purchase_id) 
                ON DELETE CASCADE
            """)
            
            # Remove default after adding column
            cursor.execute("""
                ALTER TABLE customer_purchase_item 
                ALTER COLUMN customer_purchase_id DROP DEFAULT
            """)
        
        # Add product_id if missing
        if not product_col_exists:
            # Get first product ID as default (or use 1 if no products exist)
            cursor.execute("""
                SELECT product_id FROM product LIMIT 1
            """)
            result = cursor.fetchone()
            default_product_id = result[0] if result else 1
            
            # Use the exact column type from the referenced table
            cursor.execute(f"""
                ALTER TABLE customer_purchase_item 
                ADD COLUMN product_id {product_column_type} NOT NULL DEFAULT {default_product_id},
                ADD CONSTRAINT customer_purchase_item_product_fk 
                FOREIGN KEY (product_id) REFERENCES product(product_id) 
                ON DELETE CASCADE
            """)
            
            # Remove default after adding column
            cursor.execute("""
                ALTER TABLE customer_purchase_item 
                ALTER COLUMN product_id DROP DEFAULT
            """)


def reverse_add_customer_purchase_item_columns(apps, schema_editor):
    """Reverse migration - remove the columns"""
    with schema_editor.connection.cursor() as cursor:
        try:
            cursor.execute("""
                ALTER TABLE customer_purchase_item 
                DROP FOREIGN KEY customer_purchase_item_purchase_fk,
                DROP FOREIGN KEY customer_purchase_item_product_fk,
                DROP COLUMN customer_purchase_id,
                DROP COLUMN product_id
            """)
        except Exception:
            # If columns don't exist, that's fine
            pass


class Migration(migrations.Migration):

    dependencies = [
        ('agro_app', '0016_fix_customer_purchase_columns'),
    ]

    operations = [
        migrations.RunPython(
            add_customer_purchase_item_columns_if_missing,
            reverse_add_customer_purchase_item_columns,
        ),
    ]

