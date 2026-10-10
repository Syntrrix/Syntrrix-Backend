from django.urls import path


from .views import (
    BuyerAPIView,
    LoginAPIView,
    SellerAPIView,
    SupplierAPIView,
    ProductAPIView,
    # BatchAPIView,
    SupplierPurchaseHistoryAPIView,
    SupplierPaymentAPIView,
    SupplierOutstandingAPIView,
    ProductDetailAPIView,
    InventoryStockInAPIView,
    InventoryStockOutAPIView,
    InventorySummaryAPIView,
)

urlpatterns = [
    path("auth/login/", LoginAPIView.as_view(), name="auth-login"),
    path("buyers/", BuyerAPIView.as_view(), name="buyers"),
    path("sellers/", SellerAPIView.as_view(), name="sellers"),
    path("suppliers/", SupplierAPIView.as_view(), name="suppliers"),
    path("products/", ProductAPIView.as_view(), name="products"),
    path("products/detail/", ProductDetailAPIView.as_view(), name="product-detail"),
    # path("batches/", BatchAPIView.as_view(), name="batches"),
    path("purchases/", SupplierPurchaseHistoryAPIView.as_view(), name="purchases"),
    path("payments/", SupplierPaymentAPIView.as_view(), name="payments"),
    path("outstanding/", SupplierOutstandingAPIView.as_view(), name="outstanding"),
    path("inventory/stock-in/", InventoryStockInAPIView.as_view(), name="inventory-stock-in"),
    path("inventory/stock-out/", InventoryStockOutAPIView.as_view(), name="inventory-stock-out"),
    path("inventory/summary/", InventorySummaryAPIView.as_view(), name="inventory-summary"),
]