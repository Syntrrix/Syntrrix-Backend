import os
import django
from datetime import date

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'agro_main.settings')
django.setup()

from agro_app.models import ProductPurchase, SupplierPayment

# Get some product purchases to create payments for
purchases = ProductPurchase.objects.all()[:3]

if not purchases:
    print("No product purchases found. Please create some purchases first.")
else:
    dummy_payments = []
    for i, purchase in enumerate(purchases, 1):
        dummy_payments.append({
            "invoice_number": purchase.invoice_number,
            "product_purchase": purchase,
            "amount": float(purchase.purchase_price),
            "payment_mode": "online" if i % 2 == 0 else "cash",
            "payment_date": date.today(),
        })

    for payment_data in dummy_payments:
        payment, created = SupplierPayment.objects.get_or_create(
            invoice_number=payment_data["invoice_number"],
            product_purchase=payment_data["product_purchase"],
            defaults={
                "amount": payment_data["amount"],
                "payment_mode": payment_data["payment_mode"],
                "payment_date": payment_data["payment_date"],
            }
        )
        if created:
            print(f"✓ Created payment: Invoice {payment.invoice_number} - Amount: {payment.amount}")
        else:
            print(f"✗ Payment already exists: Invoice {payment.invoice_number}")

    print(f"\nDummy payments added successfully!")
    print(f"Total payments created: {len(dummy_payments)}")
