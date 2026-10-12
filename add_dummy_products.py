import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'agro_main.settings')
django.setup()

from agro_app.models import Product

# Create 5 dummy products
dummy_products = [
    {
        "product_name": "Wheat",
        "batch": "WHEAT-2025-001"
    },
    {
        "product_name": "Rice",
        "batch": "RICE-2025-001"
    },
    {
        "product_name": "Corn",
        "batch": "CORN-2025-001"
    },
    {
        "product_name": "Soybean",
        "batch": "SOYBEAN-2025-001"
    },
    {
        "product_name": "Tomato",
        "batch": "TOMATO-2025-001"
    }
]

for product_data in dummy_products:
    product, created = Product.objects.get_or_create(
        product_name=product_data["product_name"],
        defaults={"batch": product_data["batch"]}
    )
    if created:
        print(f"✓ Created product: {product.product_name} (Batch: {product.batch})")
    else:
        print(f"✗ Product already exists: {product.product_name}")

print("\nAll dummy products added successfully!")
