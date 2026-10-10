import requests
import json

BASE_URL = "http://localhost:8000"  # Change if your server runs elsewhere

# Helper to print pretty JSON
def print_response(resp):
    print(f"\nURL: {resp.request.url}")
    print(f"Status: {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2))
    except Exception:
        print(resp.text)

"""
1. Login (get tokens)
POST /auth/login/
{
    "email": "user@example.com",
    "password": "string"
}
"""
def login(email, password):
    resp = requests.post(f"{BASE_URL}/api/auth/login/", json={"email": email, "password": password})
    print_response(resp)
    if resp.status_code == 200:
        return resp.json()["access"], resp.json()["refresh"]
    return None, None

"""
2. Register Buyer
POST /buyers/
{
    "full_name": "string",
    "password": "string",
    "email": "string",
    "phone": "string",
    "company_name": "string",
    "gst_number": "string",
    "address": "string"
}
"""
def register_buyer(data):
    resp = requests.post(f"{BASE_URL}/api/buyers/", json=data)
    print_response(resp)
    return resp

"""
3. Register Seller
POST /sellers/
{
    "full_name": "string",
    "password": "string",
    "email": "string",
    "phone": "string",
    "company_name": "string",
    "gst_number": "string",
    "address": "string",
    "pan_number": "string",
    "account_holder_name": "string",
    "account_number": "string",
    "ifsc_code": "string",
    "bank_name": "string"
}
"""
def register_seller(data):
    resp = requests.post(f"{BASE_URL}/api/sellers/", json=data)
    print_response(resp)
    return resp

"""
4. Add Supplier
POST /suppliers/
{
    "supplier_name": "string",
    "email_id": "string",
    "mobile_number": "string",
    "whatsapp_number": "string",
    "business_name": "string",
    "address": "string",
    "gst_number": "string",
    "payment_type": "cash|online|cheque|bank_transfer"
}
"""
def add_supplier(token, data):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(f"{BASE_URL}/api/suppliers/", json=data, headers=headers)
    print_response(resp)
    return resp

"""
5. Add Product
POST /products/
{
    "product_name": "string"
}
"""
def add_product(token, data):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(f"{BASE_URL}/api/products/", json=data, headers=headers)
    print_response(resp)
    return resp

"""
6. Add Purchase
POST /purchases/
{
    "supplier": 1,
    "invoice_number": "INV-001",
    "invoice_date": "YYYY-MM-DD",
    "invoice_file": "/path/to/file.pdf",
    "items": [
        {
            "product": 1,
            "batch_name": "BATCH-001",
            "purchase_price": 100.0,
            "selling_price": 120.0,
            "quantity": 10,
            "gst": 5.0
        }
        # ...more items
    ]
}
"""
def add_purchase(token, data):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(f"{BASE_URL}/api/purchases/", json=data, headers=headers)
    print_response(resp)
    return resp

"""
7. Add Payment
POST /payments/
{
    "product_purchase": 1,
    "amount": 100.0,
    "payment_date": "YYYY-MM-DD",
    "payment_mode": "cash|online|cheque|bank_transfer"
}
"""
def add_payment(token, data):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(f"{BASE_URL}/api/payments/", json=data, headers=headers)
    print_response(resp)
    return resp

"""
8. Stock In
POST /inventory/stock-in/
{
    "product": 1,
    "batch_name": "BATCH-001",  # optional
    "quantity": 10,
    "reason": "Purchase",
    "reference_id": "INV-001",
    "date": "YYYY-MM-DD"
}
"""
def stock_in(token, data):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(f"{BASE_URL}/api/inventory/stock-in/", json=data, headers=headers)
    print_response(resp)
    return resp

"""
8. Stock Out
POST /inventory/stock-out/
{
    "product": 1,
    "batch_name": "BATCH-001",  # optional
    "quantity": 2,
    "reason": "Damaged",
    "reference_id": "",
    "date": "YYYY-MM-DD"
}
"""
def stock_out(token, data):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(f"{BASE_URL}/api/inventory/stock-out/", json=data, headers=headers)
    print_response(resp)
    return resp

# 9. Get Inventory Summary
def get_inventory_summary(token, params=None):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{BASE_URL}/api/inventory/summary/", params=params, headers=headers)
    print_response(resp)
    return resp

# 10. List endpoints (buyers, sellers, suppliers, products, purchases, payments)
def list_endpoint(token, endpoint, params=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(f"{BASE_URL}/api/{endpoint}/", params=params, headers=headers)
    print_response(resp)
    return resp

# Example usage (fill with your test data)

def main():
    # 1. Register Buyer
    buyer_data = {
        "full_name": "Test Buyer",
        "password": "buyerpass123",
        "email": "buyer1@example.com",
        "phone": "9000000001",
        "company_name": "BuyerCo",
        "gst_number": "GSTBUYER001",
        "address": "123 Buyer Street",
        # status, created_at, updated_at are auto/optional
    }
    register_buyer(buyer_data)

    # 2. Register Seller
    seller_data = {
        "full_name": "Test Seller",
        "password": "sellerpass123",
        "email": "seller1@example.com",
        "phone": "9000000002",
        "company_name": "SellerCo",
        "gst_number": "GSTSELLER001",
        "address": "456 Seller Avenue",
        # The following are for UserMeta, but registration serializer expects them inline
        "pan_number": "PANSELLER0",
        "account_holder_name": "Test Seller",
        "account_number": "1234567890",
        "ifsc_code": "IFSC0001",
        "bank_name": "Test Bank",
        # status, created_at, updated_at are auto/optional
    }
    register_seller(seller_data)

    # 3. Login as seller
    access, refresh = login(seller_data["email"], seller_data["password"])
    if not access:
        print("Login failed. Aborting test.")
        return

    # 4. Add Supplier
    supplier_data = {
        "supplier_name": "Test Supplier",
        "email_id": "supplier1@example.com",
        "mobile_number": "9000000003",
        "whatsapp_number": "9000000003",
        "business_name": "SupplierCo",
        "address": "789 Supplier Road",
        "gst_number": "GSTSUPPLIER001",
        "payment_type": "online",
        # seller_user is set automatically from token
        # created_at, updated_at are auto
    }
    supplier_resp = add_supplier(access, supplier_data)
    supplier_id = supplier_resp.json().get("supplier_id", 1)

    # 5. Add Product
    product_data = {
        "product_name": "Wheat",
        "category": "Grain",
        "brand": "BrandA",
        "unit": "kg",
        "purchase_price": 100.0,
        "sale_price": 120.0,
        "mrp": 130.0,
        "gst": 5.0,
        "minimum_stock_alert": 10,
        "barcode": "WHEAT-001",
        "description": "High quality wheat"
        # created_at, updated_at are auto
    }
    product_resp = add_product(access, product_data)
    product_id = product_resp.json().get("product_id", 1)

    # 6. Add Purchase
    purchase_data = {
        "supplier": supplier_id,
        "invoice_number": "INV-001",
        "invoice_date": "2025-12-19",
        "invoice_file": "/path/to/invoice.pdf",
        "items": [
            {
                "product": product_id,
                "batch_name": "WHEAT-2025-001",
                "purchase_price": 100.0,
                "selling_price": 120.0,
                "quantity": 10,
                "gst": 5.0
            },
            {
                "product": product_id,
                "batch_name": "WHEAT-2025-002",
                "purchase_price": 110.0,
                "selling_price": 130.0,
                "quantity": 5,
                "gst": 5.0
            }
        ]
    }
    purchase_resp = add_purchase(access, purchase_data)
    purchase_id = purchase_resp.json().get("product_purchase_id", 1)

    # 7. Add Payment
    payment_data = {
        "product_purchase": purchase_id,
        "amount": 100.0,
        "payment_date": "2025-12-19",
        "payment_mode": "online"
        # invoice_number is auto from purchase
    }
    add_payment(access, payment_data)

    # 8. Stock In
    stock_in_data = {
        "product": product_id,
        "batch_name": "WHEAT-2025-001",
        "quantity": 10,
        "transaction_type": "in",
        "reason": "Purchase",
        "reference_id": "INV-001",
        "date": "2025-12-19"
    }
    stock_in(access, stock_in_data)

    # 9. Stock Out
    stock_out_data = {
        "product": product_id,
        "batch_name": "WHEAT-2025-001",
        "quantity": 2,
        "transaction_type": "out",
        "reason": "Damaged",
        "reference_id": "",
        "date": "2025-12-19"
    }
    stock_out(access, stock_out_data)

    # 10. Get Inventory Summary
    get_inventory_summary(access)

    # 11. List endpoints
    # list_endpoint(access, "buyers")
    list_endpoint(access, "sellers")
    list_endpoint(access, "suppliers")
    list_endpoint(access, "products")
    list_endpoint(access, "purchases")
    list_endpoint(access, "payments")

if __name__ == "__main__":
    main()
