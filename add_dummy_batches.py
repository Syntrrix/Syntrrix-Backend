import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'agro_main.settings')
django.setup()

from agro_app.models import Product, Batch

# Create dummy batches for products
dummy_batches = [
    # Wheat batches
    {"batch_name": "WHEAT-2025-001", "product_name": "Wheat"},
    {"batch_name": "WHEAT-2025-002", "product_name": "Wheat"},
    # Rice batches
    {"batch_name": "RICE-2025-001", "product_name": "Rice"},
    {"batch_name": "RICE-2025-002", "product_name": "Rice"},
    # Corn batch
    {"batch_name": "CORN-2025-001", "product_name": "Corn"},
]

for batch_data in dummy_batches:
    try:
        product = Product.objects.get(product_name=batch_data["product_name"])
        batch, created = Batch.objects.get_or_create(
            batch_name=batch_data["batch_name"],
            defaults={"product": product}
        )
        if created:
            print(f"✓ Created batch: {batch.batch_name} for product: {product.product_name}")
        else:
            print(f"✗ Batch already exists: {batch.batch_name}")
    except Product.DoesNotExist:
        print(f"✗ Product not found: {batch_data['product_name']}")

print("\nAll dummy batches added successfully!")
