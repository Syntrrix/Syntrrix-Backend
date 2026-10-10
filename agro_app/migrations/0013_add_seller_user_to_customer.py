# Generated migration to fix missing seller_user_id column in customer table
# This migration adds the seller_user_id column if migration 0012 was partially applied
# NOTE: If migration 0012 was fully applied, this migration will be skipped automatically

from django.db import migrations, models
import django.db.models.deletion


def check_and_add_seller_user(apps, schema_editor):
    """Add seller_user field only if it doesn't already exist"""
    Customer = apps.get_model('agro_app', 'Customer')
    
    # Check if the column already exists
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            SELECT COUNT(*) 
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'customer' 
            AND COLUMN_NAME = 'seller_user_id'
        """)
        column_exists = cursor.fetchone()[0] > 0
    
    # If column doesn't exist, add it
    if not column_exists:
        field = models.ForeignKey(
            db_column='seller_user_id',
            limit_choices_to={'role_id': 3},
            on_delete=django.db.models.deletion.CASCADE,
            related_name='customers',
            to='agro_app.user',
            null=True,
            blank=True
        )
        field.set_attributes_from_name('seller_user')
        schema_editor.add_field(Customer, field)


def reverse_add_seller_user(apps, schema_editor):
    """Reverse migration - remove field if it was added by this migration"""
    Customer = apps.get_model('agro_app', 'Customer')
    
    # Only remove if we added it (check if it exists and was added by this migration)
    try:
        field = Customer._meta.get_field('seller_user')
        schema_editor.remove_field(Customer, field)
    except:
        # Field doesn't exist or wasn't added by this migration, skip
        pass


class Migration(migrations.Migration):

    dependencies = [
        ('agro_app', '0012_customer_customercart_customerpayment_and_more'),
    ]

    operations = [
        # Use RunPython to conditionally add the field
        migrations.RunPython(
            check_and_add_seller_user,
            reverse_code=reverse_add_seller_user,
        ),
    ]

