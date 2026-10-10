# Generated migration for Category model

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('agro_app', '0018_fix_customer_payment_columns'),
    ]

    operations = [
        migrations.CreateModel(
            name='Category',
            fields=[
                ('category_id', models.AutoField(primary_key=True, serialize=False)),
                ('category_name', models.CharField(max_length=100)),
                ('description', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('seller_user', models.ForeignKey(
                    db_column='seller_user_id',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='categories',
                    to='agro_app.user'
                )),
            ],
            options={
                'db_table': 'category',
                'unique_together': {('seller_user', 'category_name')},
            },
        ),
        migrations.AddIndex(
            model_name='category',
            index=models.Index(fields=['seller_user', 'category_name'], name='category_seller__b59c95_idx'),
        ),
    ]
