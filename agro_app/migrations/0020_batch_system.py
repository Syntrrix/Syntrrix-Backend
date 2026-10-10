# Generated migration for Batch system integration

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('agro_app', '0019_category'),
    ]

    operations = [
        # Create Batch model
        migrations.CreateModel(
            name='Batch',
            fields=[
                ('batch_id', models.AutoField(primary_key=True, serialize=False)),
                ('batch_name', models.CharField(max_length=100)),
                ('expiry_date', models.DateField(blank=True, null=True)),
                ('manufacturing_date', models.DateField(blank=True, null=True)),
                ('purchase_price', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('selling_price', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('mrp', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('initial_quantity', models.PositiveIntegerField(default=0)),
                ('available_quantity', models.PositiveIntegerField(default=0)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('product', models.ForeignKey(db_column='product_id', on_delete=django.db.models.deletion.CASCADE, related_name='batches', to='agro_app.product')),
                ('supplier', models.ForeignKey(db_column='supplier_id', on_delete=django.db.models.deletion.CASCADE, related_name='batches', to='agro_app.supplier')),
            ],
            options={
                'db_table': 'batch',
                'ordering': ['expiry_date'],
            },
        ),
        # Add indexes for Batch
        migrations.AddIndex(
            model_name='batch',
            index=models.Index(fields=['product', 'expiry_date'], name='batch_product_d086fb_idx'),
        ),
        migrations.AddIndex(
            model_name='batch',
            index=models.Index(fields=['supplier', 'created_at'], name='batch_supplie_4df378_idx'),
        ),
        migrations.AddIndex(
            model_name='batch',
            index=models.Index(fields=['product', 'supplier'], name='batch_product_1d31b8_idx'),
        ),
        
        # Add batch foreign key to ProductPurchaseItem
        migrations.AddField(
            model_name='productpurchaseitem',
            name='batch',
            field=models.ForeignKey(blank=True, db_column='batch_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='purchase_items', to='agro_app.batch'),
        ),
        # Add expiry_date to ProductPurchaseItem
        migrations.AddField(
            model_name='productpurchaseitem',
            name='expiry_date',
            field=models.DateField(blank=True, null=True),
        ),
        # Add index for batch in ProductPurchaseItem
        migrations.AddIndex(
            model_name='productpurchaseitem',
            index=models.Index(fields=['batch'], name='product_pur_batch_i_9ac4b3_idx'),
        ),
        
        # Add batch foreign key to InventoryTransaction
        migrations.AddField(
            model_name='inventorytransaction',
            name='batch',
            field=models.ForeignKey(blank=True, db_column='batch_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='inventory_transactions', to='agro_app.batch'),
        ),
        # Add indexes for InventoryTransaction
        migrations.AddIndex(
            model_name='inventorytransaction',
            index=models.Index(fields=['product', 'batch'], name='inventory_t_product_a878ed_idx'),
        ),
        migrations.AddIndex(
            model_name='inventorytransaction',
            index=models.Index(fields=['transaction_type', 'date'], name='inventory_t_transac_c1873e_idx'),
        ),
        
        # Add batch foreign key to CustomerCart
        migrations.AddField(
            model_name='customercart',
            name='batch',
            field=models.ForeignKey(blank=True, db_column='batch_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cart_items', to='agro_app.batch'),
        ),
        # Update unique_together for CustomerCart to include batch
        migrations.AlterUniqueTogether(
            name='customercart',
            unique_together={('customer', 'product', 'batch')},
        ),
        # Add index for batch in CustomerCart
        migrations.AddIndex(
            model_name='customercart',
            index=models.Index(fields=['customer', 'product', 'batch'], name='customer_ca_custome_a1954e_idx'),
        ),
        
        # Add batch foreign key to CustomerPurchaseItem
        migrations.AddField(
            model_name='customerpurchaseitem',
            name='batch',
            field=models.ForeignKey(blank=True, db_column='batch_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='customer_purchase_items', to='agro_app.batch'),
        ),
        # Add index for batch in CustomerPurchaseItem
        migrations.AddIndex(
            model_name='customerpurchaseitem',
            index=models.Index(fields=['batch'], name='customer_pu_batch_i_e12a0d_idx'),
        ),
    ]
