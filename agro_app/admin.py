from django.contrib import admin
from .models import (
    UserRole,
    User,
    UserMeta,
    Supplier,
    Product,
    Batch,
    ProductPurchase,
    ProductPurchaseItem,
    SupplierPayment,
    InventoryTransaction,
    Customer,
    CustomerCart,
    CustomerPurchase,
    CustomerPurchaseItem,
    CustomerPayment,
    Category,
    OnlineOrder,
    OnlineOrderItem,
)


class BatchAdmin(admin.ModelAdmin):
    list_display = ['batch_id', 'batch_name', 'product', 'supplier', 'expiry_date', 'available_quantity', 'is_active']
    list_filter = ['is_active', 'supplier', 'expiry_date']
    search_fields = ['batch_name', 'product__product_name', 'supplier__supplier_name']
    ordering = ['expiry_date']
    date_hierarchy = 'expiry_date'


class ProductAdmin(admin.ModelAdmin):
    list_display = ['product_id', 'product_name', 'seller_user', 'category', 'sale_price', 'created_at']
    list_filter = ['seller_user', 'category']
    search_fields = ['product_name', 'barcode']


class SupplierAdmin(admin.ModelAdmin):
    list_display = ['supplier_id', 'supplier_name', 'seller_user', 'mobile_number', 'created_at']
    list_filter = ['seller_user', 'payment_type']
    search_fields = ['supplier_name', 'business_name']


class InventoryTransactionAdmin(admin.ModelAdmin):
    list_display = ['transaction_id', 'product', 'batch', 'quantity', 'transaction_type', 'date']
    list_filter = ['transaction_type', 'date']
    search_fields = ['product__product_name', 'batch__batch_name']


class OnlineOrderItemInline(admin.TabularInline):
    model = OnlineOrderItem
    extra = 0


class OnlineOrderAdmin(admin.ModelAdmin):
    list_display = ['order_id', 'status', 'order_no', 'seller_user', 'customer_name', 'customer_phone', 'total_amount', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['order_no', 'customer_name', 'customer_phone', 'data_hub_row_id']
    ordering = ['-created_at']
    inlines = [OnlineOrderItemInline]
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        (None, {'fields': ('seller_user', 'source_bot', 'data_hub_table_id', 'data_hub_row_id')}),
        ('Customer', {'fields': ('customer_name', 'customer_phone', 'customer_email', 'customer_address')}),
        ('Order', {'fields': ('total_amount', 'status', 'order_no')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')}),
    )


# Register models
admin.site.register(UserRole)
admin.site.register(User)
admin.site.register(UserMeta)
admin.site.register(Supplier, SupplierAdmin)
admin.site.register(Product, ProductAdmin)
admin.site.register(Batch, BatchAdmin)
admin.site.register(ProductPurchase)
admin.site.register(ProductPurchaseItem)
admin.site.register(SupplierPayment)
admin.site.register(InventoryTransaction, InventoryTransactionAdmin)
admin.site.register(Customer)
admin.site.register(CustomerCart)
admin.site.register(CustomerPurchase)
admin.site.register(CustomerPurchaseItem)
admin.site.register(CustomerPayment)
admin.site.register(Category)
admin.site.register(OnlineOrder, OnlineOrderAdmin)
