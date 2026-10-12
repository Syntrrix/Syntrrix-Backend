from django.contrib.auth.hashers import check_password, make_password
from django.db.models import Sum
from rest_framework import serializers

from .models import (
    User, UserMeta, UserRole, Plan, Supplier, Product, Batch, ProductPurchase, ProductPurchaseItem, SupplierPayment, InventoryTransaction,
    Customer, CustomerCart, CustomerPurchase, CustomerPurchaseItem, CustomerPayment, Category, Bot,
    LiveChatSession, LiveChatMessage, TeamMember, WhatsAppCampaign, WhatsAppCampaignRecipient,
)

class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = ["plan_id", "name", "features", "description", "created_at", "updated_at"]

# InventoryTransaction serializer for stock in/out
class InventoryTransactionSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    batch_name = serializers.CharField(required=False, allow_blank=True)
    batch_info = serializers.SerializerMethodField()

    class Meta:
        model = InventoryTransaction
        fields = [
            "transaction_id",
            "product",
            "product_name",
            "batch",
            "batch_name",
            "batch_info",
            "quantity",
            "transaction_type",
            "reason",
            "reference_id",
            "date",
            "expiry_date",
            "manufacturing_date",
            "created_at",
        ]
        extra_kwargs = {
            "transaction_id": {"read_only": True},
            "created_at": {"read_only": True},
            "product_name": {"read_only": True},
            "batch_info": {"read_only": True},
            "batch": {"required": False, "allow_null": True},
            "expiry_date": {"required": False, "allow_null": True},
            "manufacturing_date": {"required": False, "allow_null": True},
        }

    def get_batch_info(self, obj):
        if obj.batch:
            return {
                "batch_id": obj.batch.batch_id,
                "batch_name": obj.batch.batch_name,
                "expiry_date": obj.batch.expiry_date,
                "available_quantity": obj.batch.available_quantity,
                "purchase_price": str(obj.batch.purchase_price) if obj.batch.purchase_price else None,
                "selling_price": str(obj.batch.selling_price) if obj.batch.selling_price else None,
                "mrp": str(obj.batch.mrp) if obj.batch.mrp else None,
            }
        return None

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0.")
        return value

    def validate(self, attrs):
        # Ensure transaction_type is valid
        if attrs.get('transaction_type') not in [InventoryTransaction.TRANSACTION_TYPE_IN, InventoryTransaction.TRANSACTION_TYPE_OUT]:
            raise serializers.ValidationError("Invalid transaction type.")
        return attrs


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    default_error_messages = {
        "invalid_credentials": "Invalid email or password.",
        "inactive": "User account is inactive.",
    }

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        try:
            user = User.objects.select_related("role", "meta").get(email=email)
        except User.DoesNotExist as exc:
            raise serializers.ValidationError(
                self.error_messages["invalid_credentials"],
                code="invalid_credentials",
            ) from exc

        if not check_password(password, user.password):
            raise serializers.ValidationError(
                self.error_messages["invalid_credentials"],
                code="invalid_credentials",
            )

        if user.status != User.STATUS_ACTIVE:
            raise serializers.ValidationError(
                self.error_messages["inactive"],
                code="inactive",
            )

        attrs["user"] = user
        return attrs


class BaseRegistrationSerializer(serializers.ModelSerializer):
    role_id = None

    class Meta:
        model = User
        fields = [
            "full_name",
            "password",
            "email",
            "phone",
            "company_name",
            "gst_number",
            "address",
            "status",
        ]
        extra_kwargs = {
            "password": {"write_only": True},
            "status": {"required": False},
            "company_name": {"required": False, "allow_blank": True},
            "gst_number": {"required": False, "allow_blank": True},
            "address": {"required": False, "allow_blank": True},
        }

    def create(self, validated_data):
        validated_data["password"] = make_password(validated_data["password"])
        if not self.role_id:
            raise serializers.ValidationError("Role is not configured.")
        try:
            role = UserRole.objects.get(role_id=self.role_id)
        except UserRole.DoesNotExist as exc:
            raise serializers.ValidationError("Role not found.") from exc
        status_value = validated_data.get("status") or User.STATUS_PENDING
        validated_data["status"] = status_value
        return User.objects.create(role=role, **validated_data)


class BuyerRegistrationSerializer(BaseRegistrationSerializer):
    role_id = UserRole.ROLE_BUYER


class SellerRegistrationSerializer(BaseRegistrationSerializer):
    role_id = UserRole.ROLE_SELLER

    pan_number = serializers.CharField(max_length=10)
    account_holder_name = serializers.CharField(max_length=150)
    account_number = serializers.CharField(max_length=30)
    ifsc_code = serializers.CharField(max_length=11)
    bank_name = serializers.CharField(max_length=120)

    class Meta(BaseRegistrationSerializer.Meta):
        fields = BaseRegistrationSerializer.Meta.fields + [
            "pan_number",
            "account_holder_name",
            "account_number",
            "ifsc_code",
            "bank_name",
        ]

    def create(self, validated_data):
        meta_fields = {
            "pan_number": validated_data.pop("pan_number"),
            "account_holder_name": validated_data.pop("account_holder_name"),
            "account_number": validated_data.pop("account_number"),
            "ifsc_code": validated_data.pop("ifsc_code"),
            "bank_name": validated_data.pop("bank_name"),
        }
        user = super().create(validated_data)
        UserMeta.objects.create(user=user, **meta_fields)
        return user


class UserDetailSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    meta = serializers.SerializerMethodField()
    plan = serializers.SerializerMethodField()
    whatsapp_config = serializers.SerializerMethodField()
    whatsapp_configs = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "company_name",
            "gst_number",
            "address",
            "status",
            "role",
            "meta",
            "plan",
            "whatsapp_config",
            "whatsapp_configs",
            "created_at",
            "updated_at",
        ]

    def get_role(self, obj):
        return {
            "id": obj.role.role_id,
            "name": obj.role.name,
        }

    def get_meta(self, obj):
        if not hasattr(obj, "meta") or obj.meta is None:
            return None
        return {
            "pan_number": obj.meta.pan_number,
            "account_holder_name": obj.meta.account_holder_name,
            "account_number": obj.meta.account_number,
            "ifsc_code": obj.meta.ifsc_code,
            "bank_name": obj.meta.bank_name,
        }

    def get_plan(self, obj):
        try:
            if not getattr(obj, "plan", None):
                return None
            return {
                "plan_id": obj.plan.plan_id,
                "name": obj.plan.name,
                "features": obj.plan.features,
                "description": obj.plan.description,
            }
        except Exception:
            return None

    def get_whatsapp_configs(self, obj):
        try:
            from agro_app.models import WhatsAppConfig
            configs = WhatsAppConfig.objects.filter(seller_user=obj).order_by("-created_at")
            return [
                {
                    "config_id": c.whatsapp_config_id,
                    "channel_name": c.channel_name,
                    "phone_number": c.phone_number,
                    "phone_number_id": c.phone_number_id,
                    "whatsapp_business_account_id": c.whatsapp_business_account_id,
                    "access_token": c.access_token,
                    "app_id": getattr(c, "app_id", "") or "",
                    "is_connected": c.is_connected,
                }
                for c in configs
            ]
        except Exception:
            return []

    def get_whatsapp_config(self, obj):
        configs = self.get_whatsapp_configs(obj)
        return configs[0] if configs else None


class TeamMemberSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)
    status = serializers.CharField(source="user.status", read_only=True)
    role = serializers.SerializerMethodField()
    assigned_team_manager = serializers.SerializerMethodField()

    class Meta:
        model = TeamMember
        fields = [
            "team_member_id",
            "user_id",
            "member_type",
            "first_name",
            "last_name",
            "city",
            "team_name",
            "team_description",
            "full_name",
            "email",
            "phone",
            "status",
            "role",
            "assigned_team_manager",
            "created_at",
            "updated_at",
        ]

    def get_role(self, obj):
        return {
            "id": obj.user.role_id,
            "name": obj.user.role.name,
        }

    def get_assigned_team_manager(self, obj):
        if not obj.parent_team_manager:
            return None
        return {
            "id": obj.parent_team_manager_id,
            "name": obj.parent_team_manager.full_name,
        }


class TeamMemberCreateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=80)
    last_name = serializers.CharField(max_length=80, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=15)
    email = serializers.EmailField()
    user_type = serializers.ChoiceField(choices=["team_manager", "agent"])
    city = serializers.CharField(max_length=120, required=False, allow_blank=True)
    team_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    team_description = serializers.CharField(max_length=255, required=False, allow_blank=True)
    parent_team_manager_id = serializers.IntegerField(required=False, allow_null=True)
    password = serializers.CharField(write_only=True, min_length=6)

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("User with this email already exists.")
        return email

    def validate_phone(self, value):
        phone = value.strip()
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError("User with this phone already exists.")
        return phone

    def validate(self, attrs):
        seller_user = self.context.get("seller_user")
        if not seller_user:
            raise serializers.ValidationError("Seller context is required.")

        user_type = attrs.get("user_type")
        team_name = (attrs.get("team_name") or "").strip()
        team_description = (attrs.get("team_description") or "").strip()
        parent_team_manager_id = attrs.get("parent_team_manager_id")

        if user_type == "team_manager":
            if not team_name:
                raise serializers.ValidationError({"team_name": "Team name is required for Team Manager."})
            if not team_description:
                raise serializers.ValidationError({"team_description": "Team description is required for Team Manager."})
        else:
            if not parent_team_manager_id:
                raise serializers.ValidationError({"parent_team_manager_id": "Select Team is required for Agent."})
            exists = TeamMember.objects.filter(
                seller_user=seller_user,
                user_id=parent_team_manager_id,
                member_type=TeamMember.MEMBER_TYPE_TEAM_MANAGER,
            ).exists()
            if not exists:
                raise serializers.ValidationError({"parent_team_manager_id": "Selected team manager is invalid."})

        return attrs

    def create(self, validated_data):
        seller_user = self.context["seller_user"]
        first_name = validated_data["first_name"].strip()
        last_name = (validated_data.get("last_name") or "").strip()
        full_name = " ".join([p for p in [first_name, last_name] if p]).strip()
        user_type = validated_data["user_type"]
        # DB must have role rows 4/5 (seeded in migration 0035). If missing, create them
        # so create never 500s on fresh/partially migrated DBs.
        if user_type == "team_manager":
            role, _ = UserRole.objects.get_or_create(
                role_id=UserRole.ROLE_TEAM_MANAGER,
                defaults={
                    "name": "Team Manager",
                    "description": "Seller team manager users.",
                },
            )
        else:
            role, _ = UserRole.objects.get_or_create(
                role_id=UserRole.ROLE_AGENT,
                defaults={
                    "name": "Agent",
                    "description": "Seller team agent users.",
                },
            )

        user = User.objects.create(
            role=role,
            full_name=full_name,
            password=make_password(validated_data["password"]),
            email=validated_data["email"].strip().lower(),
            phone=validated_data["phone"].strip(),
            status=User.STATUS_ACTIVE,
        )

        return TeamMember.objects.create(
            seller_user=seller_user,
            user=user,
            member_type=user_type,
            first_name=first_name,
            last_name=last_name,
            city=(validated_data.get("city") or "").strip(),
            team_name=(validated_data.get("team_name") or "").strip(),
            team_description=(validated_data.get("team_description") or "").strip(),
            parent_team_manager_id=validated_data.get("parent_team_manager_id"),
        )


class TeamMemberUpdateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=80, required=False)
    last_name = serializers.CharField(max_length=80, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=15, required=False)
    email = serializers.EmailField(required=False)
    city = serializers.CharField(max_length=120, required=False, allow_blank=True)
    team_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    team_description = serializers.CharField(max_length=255, required=False, allow_blank=True)
    parent_team_manager_id = serializers.IntegerField(required=False, allow_null=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, min_length=6)

    def validate_email(self, value):
        email = value.strip().lower()
        member = self.instance
        if (
            member
            and User.objects.filter(email=email).exclude(id=member.user_id).exists()
        ):
            raise serializers.ValidationError("User with this email already exists.")
        return email

    def validate_phone(self, value):
        phone = value.strip()
        member = self.instance
        if (
            member
            and User.objects.filter(phone=phone).exclude(id=member.user_id).exists()
        ):
            raise serializers.ValidationError("User with this phone already exists.")
        return phone

    def validate(self, attrs):
        seller_user = self.context.get("seller_user")
        member = self.instance
        if not seller_user or not member:
            raise serializers.ValidationError("Seller context is required.")

        if member.member_type == TeamMember.MEMBER_TYPE_TEAM_MANAGER:
            team_name = (attrs.get("team_name", member.team_name) or "").strip()
            team_description = (attrs.get("team_description", member.team_description) or "").strip()
            if not team_name:
                raise serializers.ValidationError({"team_name": "Team name is required for Team Manager."})
            if not team_description:
                raise serializers.ValidationError({"team_description": "Team description is required for Team Manager."})
            attrs["parent_team_manager_id"] = None
        else:
            parent_team_manager_id = attrs.get("parent_team_manager_id", member.parent_team_manager_id)
            if not parent_team_manager_id:
                raise serializers.ValidationError({"parent_team_manager_id": "Select Team is required for Agent."})
            exists = TeamMember.objects.filter(
                seller_user=seller_user,
                user_id=parent_team_manager_id,
                member_type=TeamMember.MEMBER_TYPE_TEAM_MANAGER,
            ).exists()
            if not exists:
                raise serializers.ValidationError({"parent_team_manager_id": "Selected team manager is invalid."})
            attrs["parent_team_manager_id"] = parent_team_manager_id
            attrs["team_name"] = ""
            attrs["team_description"] = ""
        return attrs

    def update(self, instance, validated_data):
        user = instance.user
        first_name = validated_data.get("first_name", instance.first_name).strip()
        last_name = (validated_data.get("last_name", instance.last_name) or "").strip()
        full_name = " ".join([p for p in [first_name, last_name] if p]).strip()

        user_update_fields = ["full_name"]
        if "email" in validated_data:
            user.email = validated_data["email"].strip().lower()
            user_update_fields.append("email")
        if "phone" in validated_data:
            user.phone = validated_data["phone"].strip()
            user_update_fields.append("phone")
        if "password" in validated_data and validated_data["password"]:
            user.password = make_password(validated_data["password"])
            user_update_fields.append("password")
        user.full_name = full_name
        user.save(update_fields=user_update_fields)

        instance.first_name = first_name
        instance.last_name = last_name
        if "city" in validated_data:
            instance.city = (validated_data.get("city") or "").strip()
        if instance.member_type == TeamMember.MEMBER_TYPE_TEAM_MANAGER:
            instance.team_name = (validated_data.get("team_name", instance.team_name) or "").strip()
            instance.team_description = (validated_data.get("team_description", instance.team_description) or "").strip()
            instance.parent_team_manager = None
        else:
            instance.parent_team_manager_id = validated_data.get("parent_team_manager_id")
            instance.team_name = ""
            instance.team_description = ""
        instance.save()
        return instance


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            "supplier_id",
            "supplier_name",
            "email_id",
            "seller_user",
            "mobile_number",
            "whatsapp_number",
            "business_name",
            "address",
            "gst_number",
            "payment_type",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "supplier_id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "whatsapp_number": {"required": False, "allow_blank": True},
            "business_name": {"required": False, "allow_blank": True},
            "address": {"required": False, "allow_blank": True},
            "gst_number": {"required": False, "allow_blank": True},
        }


class BotSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bot
        fields = [
            "bot_id",
            "seller_user",
            "name",
            "flow",
            "design",
            "is_published",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "bot_id": {"read_only": True},
            "seller_user": {"read_only": True},
            "is_published": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
        }

    def validate_seller_user(self, value):
        """Validate that the seller_user is actually a seller."""
        if value.role.role_id != UserRole.ROLE_SELLER:
            raise serializers.ValidationError(
                "The provided user must have a seller role."
            )
        return value


class ProductSerializer(serializers.ModelSerializer):
    seller_user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role__role_id=UserRole.ROLE_SELLER),
        required=True
    )

    class Meta:
        model = Product
        fields = [
            "product_id",
            "product_name",
            "seller_user",
            "category",
            "brand",
            "unit",
            "purchase_price",
            "sale_price",
            "mrp",
            "gst",
            "minimum_stock_alert",
            "barcode",
            "description",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "product_id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "barcode": {"required": False},
            "seller_user": {"required": True},
        }

    def validate_seller_user(self, value):
        if value.role.role_id != UserRole.ROLE_SELLER:
            raise serializers.ValidationError("The provided user must have a seller role.")
        return value

    def create(self, validated_data):
        # Auto-generate barcode if not provided
        if not validated_data.get("barcode"):
            import uuid
            validated_data["barcode"] = str(uuid.uuid4())
        return super().create(validated_data)


class BatchSerializer(serializers.ModelSerializer):
    """
    Serializer for Batch model with comprehensive batch information.
    """
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.supplier_name', read_only=True)
    is_expired = serializers.BooleanField(read_only=True)
    days_until_expiry = serializers.IntegerField(read_only=True)
    stock_status = serializers.SerializerMethodField()

    class Meta:
        model = Batch
        fields = [
            "batch_id",
            "batch_name",
            "product",
            "product_name",
            "supplier",
            "supplier_name",
            "expiry_date",
            "manufacturing_date",
            "purchase_price",
            "selling_price",
            "mrp",
            "initial_quantity",
            "available_quantity",
            "is_active",
            "is_expired",
            "days_until_expiry",
            "stock_status",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "batch_id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "product_name": {"read_only": True},
            "supplier_name": {"read_only": True},
            "is_expired": {"read_only": True},
            "days_until_expiry": {"read_only": True},
            "available_quantity": {"required": False},
        }

    def get_stock_status(self, obj):
        """Calculate stock status for the batch."""
        if obj.available_quantity <= 0:
            return "out_of_stock"
        elif obj.available_quantity < 10:
            return "low_stock"
        return "in_stock"

    def validate(self, attrs):
        """Validate batch data."""
        expiry_date = attrs.get('expiry_date')
        manufacturing_date = attrs.get('manufacturing_date')
        
        if expiry_date and manufacturing_date:
            if expiry_date <= manufacturing_date:
                raise serializers.ValidationError({
                    "expiry_date": "Expiry date must be after manufacturing date."
                })
        
        return attrs

    def create(self, validated_data):
        """Create batch with initial_quantity set to available_quantity if not specified."""
        if 'available_quantity' not in validated_data:
            validated_data['available_quantity'] = validated_data.get('initial_quantity', 0)
        return super().create(validated_data)


class BatchListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for batch lists with minimal fields.
    """
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.supplier_name', read_only=True)
    days_until_expiry = serializers.IntegerField(read_only=True)

    class Meta:
        model = Batch
        fields = [
            "batch_id",
            "batch_name",
            "product",
            "product_name",
            "supplier_name",
            "expiry_date",
            "available_quantity",
            "selling_price",
            "days_until_expiry",
            "is_active",
        ]










# New serializer for line items
class ProductPurchaseItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    batch_name = serializers.CharField(required=False, allow_blank=True)
    batch_info = serializers.SerializerMethodField()

    class Meta:
        model = ProductPurchaseItem
        fields = [
            "item_id",
            "product",
            "product_name",
            "batch",
            "batch_name",
            "batch_info",
            "purchase_price",
            "selling_price",
            "mrp",
            "quantity",
            "gst",
        ]
        extra_kwargs = {
            "item_id": {"read_only": True},
            "batch": {"required": False, "allow_null": True},
            "mrp": {"required": False, "allow_null": True},
        }

    def get_batch_info(self, obj):
        if obj.batch:
            return {
                "batch_id": obj.batch.batch_id,
                "batch_name": obj.batch.batch_name,
                "expiry_date": obj.batch.expiry_date,
                "available_quantity": obj.batch.available_quantity,
            }
        return None

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0.")
        return value
    def validate_purchase_price(self, value):
        if value <= 0:
            raise serializers.ValidationError("Purchase price must be greater than 0.")
        return value
    def validate_selling_price(self, value):
        if value <= 0:
            raise serializers.ValidationError("Selling price must be greater than 0.")
        return value

class ProductPurchaseSerializer(serializers.ModelSerializer):
    invoice_file_url = serializers.SerializerMethodField()
    supplier_name = serializers.CharField(source='supplier.supplier_name', read_only=True)
    items = ProductPurchaseItemSerializer(many=True)

    class Meta:
        model = ProductPurchase
        fields = [
            "product_purchase_id",
            "supplier",
            "supplier_name",
            "invoice_number",
            "invoice_date",
            "invoice_file",
            "invoice_file_url",
            "created_at",
            "items",
        ]
        extra_kwargs = {
            "product_purchase_id": {"read_only": True},
            "created_at": {"read_only": True},
            "invoice_file": {"required": False, "allow_blank": True},
            "invoice_file_url": {"read_only": True},
        }

    def get_invoice_file_url(self, obj):
        """
        Build full URL for invoice file.
        The invoice_file is stored as media/invoices/filename in the database (without leading /).
        We prepend the host URL and / to make it a full accessible URL.
        """
        if obj.invoice_file:
            request = self.context.get('request')
            # invoice_file is stored as media/invoices/filename (without leading /)
            # Add leading / for URL construction
            invoice_path = obj.invoice_file if obj.invoice_file.startswith('/') else f"/{obj.invoice_file}"
            if request:
                scheme = request.scheme
                host = request.get_host()
                return f"{scheme}://{host}{invoice_path}"
            return invoice_path
        return None

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        purchase = ProductPurchase.objects.create(**validated_data)
        for item_data in items_data:
            ProductPurchaseItem.objects.create(product_purchase=purchase, **item_data)
        return purchase


class SupplierPaymentSerializer(serializers.ModelSerializer):
    supplier_name = serializers.SerializerMethodField()
    product_name = serializers.SerializerMethodField()
    batch_name = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    payment_proof_url = serializers.SerializerMethodField()

    class Meta:
        model = SupplierPayment
        fields = [
            "supplier_payment_id",
            "invoice_number",
            "product_purchase",
            "supplier_name",
            "product_name",
            "batch_name",
            "amount",
            "payment_mode",
            "payment_date",
            "payment_status",
            "payment_proof",
            "payment_proof_url",
            "note",
        ]
        extra_kwargs = {
            "supplier_payment_id": {"read_only": True},
            "payment_status": {"read_only": True},
            "payment_proof": {"required": False, "allow_blank": True, "allow_null": True},
            "payment_proof_url": {"read_only": True},
            "note": {"required": False, "allow_blank": True, "allow_null": True},
        }

    def get_supplier_name(self, obj):
        return obj.product_purchase.supplier.supplier_name if obj.product_purchase and obj.product_purchase.supplier else None

    def get_product_name(self, obj):
        # If there are multiple items, this will just show the first product name (customize as needed)
        if obj.product_purchase and hasattr(obj.product_purchase, 'items'):
            items = obj.product_purchase.items.all()
            if items:
                return items[0].product.product_name
        return None

    def get_batch_name(self, obj):
        if obj.product_purchase and hasattr(obj.product_purchase, 'items'):
            items = obj.product_purchase.items.all()
            if items:
                return items[0].batch_name
        return None

    def get_payment_status(self, obj):
        return "received"

    def get_payment_proof_url(self, obj):
        """
        Build full URL for payment proof file.
        The payment_proof is stored as media/proof/filename in the database (same pattern as invoice_file: media/invoices/filename).
        We prepend the host URL and / to make it a full accessible URL.
        """
        if obj.payment_proof:
            request = self.context.get('request')
            # payment_proof is stored as media/proof/filename (without leading /)
            # Add leading / for URL construction
            proof_path = obj.payment_proof if obj.payment_proof.startswith('/') else f"/{obj.payment_proof}"
            if request:
                scheme = request.scheme
                host = request.get_host()
                return f"{scheme}://{host}{proof_path}"
            return proof_path
        return None

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than 0.")
        return value

    def validate(self, data):
        """
        Validate that the payment amount does not exceed the outstanding invoice amount.
        """
        product_purchase = data.get('product_purchase')
        amount = data.get('amount')
        
        if product_purchase and amount:
            # Handle case where product_purchase might be an ID (integer) or an object
            if isinstance(product_purchase, (int, str)):
                try:
                    product_purchase = ProductPurchase.objects.get(product_purchase_id=int(product_purchase))
                except ProductPurchase.DoesNotExist:
                    raise serializers.ValidationError({
                        'product_purchase': 'Invalid product purchase ID.'
                    })
            
            # Calculate total invoice amount from purchase items
            total_amount = 0
            if hasattr(product_purchase, 'items'):
                items_qs = product_purchase.items.all()
                for item in items_qs:
                    purchase_price = float(item.purchase_price or 0)
                    quantity = float(item.quantity or 0)
                    gst = float(item.gst or 0)
                    line_total = purchase_price * quantity * (1 + gst / 100)
                    total_amount += line_total
            
            # Calculate total already paid (excluding current payment if updating)
            payment_agg = SupplierPayment.objects.filter(
                product_purchase_id=product_purchase.product_purchase_id
            )
            
            # If updating an existing payment, exclude it from the total paid calculation
            if self.instance:
                payment_agg = payment_agg.exclude(supplier_payment_id=self.instance.supplier_payment_id)
            
            total_paid = float(payment_agg.aggregate(total_paid=Sum("amount"))["total_paid"] or 0)
            
            # Calculate outstanding amount
            outstanding_amount = max(total_amount - total_paid, 0)
            
            # Validate that payment amount doesn't exceed outstanding amount
            if float(amount) > outstanding_amount:
                raise serializers.ValidationError({
                    'amount': f'Payment amount (₹{amount:.2f}) cannot exceed the outstanding invoice amount (₹{outstanding_amount:.2f}). '
                              f'Invoice total: ₹{total_amount:.2f}, Already paid: ₹{total_paid:.2f}, Outstanding: ₹{outstanding_amount:.2f}'
                })
        
        return data

    def create(self, validated_data):
        """
        Create a new SupplierPayment instance.
        Explicitly ensure payment_proof is saved.
        """
        import logging
        logger = logging.getLogger(__name__)
        
        # Log validated_data to debug
        logger.info(f"SupplierPaymentSerializer.create() - validated_data keys: {validated_data.keys()}")
        logger.info(f"SupplierPaymentSerializer.create() - payment_proof value: {validated_data.get('payment_proof')}")
        
        # Create the payment instance with all validated data (including payment_proof)
        payment = SupplierPayment.objects.create(**validated_data)
        
        # Log after creation to verify payment_proof was saved
        logger.info(f"SupplierPayment created - ID: {payment.supplier_payment_id}, payment_proof: {payment.payment_proof}")
        
        return payment


# ============================================================================
# CUSTOMER FLOW SERIALIZERS
# ============================================================================

class CustomerSerializer(serializers.ModelSerializer):
    """Serializer for customer management."""
    class Meta:
        model = Customer
        fields = [
            "customer_id",
            "full_name",
            "email",
            "mobile_number",
            "whatsapp_number",
            "address",
            "gst_number",
            "created_via_bot",
            "source_bot",
            "bot_payload",
            "seller_user",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "customer_id": {"read_only": True},
            "seller_user": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "created_via_bot": {"read_only": True},
            "source_bot": {"read_only": True},
            "bot_payload": {"read_only": True},
            "whatsapp_number": {"required": False, "allow_blank": True},
            "address": {"required": False, "allow_blank": True},
            "gst_number": {"required": False, "allow_blank": True},
        }


class ProductListSerializer(serializers.ModelSerializer):
    """Serializer for displaying product list with available stock and batch info."""
    available_stock = serializers.SerializerMethodField()
    batches_available = serializers.SerializerMethodField()
    near_expiry_batch = serializers.SerializerMethodField()
    batch_wise_quantities = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "product_id",
            "product_name",
            "category",
            "brand",
            "unit",
            "sale_price",
            "mrp",
            "gst",
            "available_stock",
            "batches_available",
            "near_expiry_batch",
            "batch_wise_quantities",
        ]

    def get_available_stock(self, obj):
        """
        Source of truth for Product List stock:
        sum of active batches' available_quantity.
        This matches batch-wise inventory operations and avoids transaction drift.
        """
        from django.db.models import Sum

        total = Batch.objects.filter(
            product=obj,
            is_active=True,
        ).aggregate(total=Sum("available_quantity"))["total"] or 0
        return max(total, 0)

    def get_batches_available(self, obj):
        """Get count of active batches with stock."""
        return Batch.objects.filter(
            product=obj,
            is_active=True,
            available_quantity__gt=0
        ).count()

    def get_near_expiry_batch(self, obj):
        """Get the nearest expiry batch for this product."""
        from django.utils import timezone
        from django.db.models import F, Q
        
        today = timezone.now().date()
        batch = Batch.objects.filter(
            product=obj,
            is_active=True,
            available_quantity__gt=0
        ).filter(
            Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
        ).order_by(
            F('expiry_date').asc(nulls_last=True)
        ).first()
        
        if batch:
            return {
                "batch_id": batch.batch_id,
                "batch_name": batch.batch_name,
                "expiry_date": batch.expiry_date,
                "available_quantity": batch.available_quantity,
                "selling_price": batch.selling_price,
                "days_until_expiry": batch.days_until_expiry,
            }
        return None

    def get_batch_wise_quantities(self, obj):
        """Get detailed list of all batches with their quantities for this product."""
        batches = Batch.objects.filter(
            product=obj,
            is_active=True,
            available_quantity__gt=0
        ).order_by('expiry_date')
        
        batch_list = []
        for batch in batches:
            batch_list.append({
                "batch_id": batch.batch_id,
                "batch_name": batch.batch_name,
                "supplier_id": batch.supplier_id,
                "supplier_name": batch.supplier.supplier_name if batch.supplier else None,
                "expiry_date": batch.expiry_date,
                "available_quantity": batch.available_quantity,
                "purchase_price": float(batch.purchase_price) if batch.purchase_price else None,
                "selling_price": float(batch.selling_price) if batch.selling_price else None,
                "days_until_expiry": batch.days_until_expiry,
            })
        return batch_list


class CustomerCartSerializer(serializers.ModelSerializer):
    """Serializer for customer cart items with batch support."""
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    sale_price = serializers.DecimalField(source='product.sale_price', read_only=True, max_digits=12, decimal_places=2)
    mrp = serializers.DecimalField(source='product.mrp', read_only=True, max_digits=12, decimal_places=2)
    gst = serializers.DecimalField(source='product.gst', read_only=True, max_digits=5, decimal_places=2)
    available_stock = serializers.SerializerMethodField()
    batch_info = serializers.SerializerMethodField()

    class Meta:
        model = CustomerCart
        fields = [
            "cart_id",
            "customer",
            "product",
            "product_name",
            "batch",
            "batch_info",
            "quantity",
            "sale_price",
            "mrp",
            "gst",
            "available_stock",
            "added_at",
            "updated_at",
        ]
        extra_kwargs = {
            "cart_id": {"read_only": True},
            "product_name": {"read_only": True},
            "sale_price": {"read_only": True},
            "mrp": {"read_only": True},
            "gst": {"read_only": True},
            "available_stock": {"read_only": True},
            "batch_info": {"read_only": True},
            "added_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "batch": {"required": False, "allow_null": True},
        }

    def get_batch_info(self, obj):
        """Get batch information if available."""
        if obj.batch:
            return {
                "batch_id": obj.batch.batch_id,
                "batch_name": obj.batch.batch_name,
                "expiry_date": obj.batch.expiry_date,
                "available_quantity": obj.batch.available_quantity,
                "selling_price": obj.batch.selling_price,
                "mrp": obj.batch.mrp,
            }
        return None

    def get_available_stock(self, obj):
        """Calculate available stock from batch or inventory transactions."""
        from django.db.models import Sum
        
        # If batch is specified, return batch available quantity
        if obj.batch:
            return obj.batch.available_quantity
        
        # Otherwise calculate from inventory transactions
        stock_in = InventoryTransaction.objects.filter(
            product=obj.product,
            transaction_type=InventoryTransaction.TRANSACTION_TYPE_IN
        ).aggregate(total=Sum('quantity'))['total'] or 0

        stock_out = InventoryTransaction.objects.filter(
            product=obj.product,
            transaction_type=InventoryTransaction.TRANSACTION_TYPE_OUT
        ).aggregate(total=Sum('quantity'))['total'] or 0

        return stock_in - stock_out

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0.")
        return value


class CustomerPurchaseItemSerializer(serializers.ModelSerializer):
    """Serializer for customer purchase line items with batch support."""
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    batch_info = serializers.SerializerMethodField()

    class Meta:
        model = CustomerPurchaseItem
        fields = [
            "item_id",
            "customer_purchase",
            "product",
            "product_name",
            "batch",
            "batch_info",
            "quantity",
            "unit_price",
            "gst",
            "line_total",
        ]
        extra_kwargs = {
            "item_id": {"read_only": True},
            "product_name": {"read_only": True},
            "line_total": {"read_only": True},
            "batch_info": {"read_only": True},
            "batch": {"required": False, "allow_null": True},
        }

    def get_batch_info(self, obj):
        """Get batch information if available."""
        if obj.batch:
            return {
                "batch_id": obj.batch.batch_id,
                "batch_name": obj.batch.batch_name,
                "expiry_date": obj.batch.expiry_date,
            }
        return None

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than 0.")
        return value

    def validate_unit_price(self, value):
        if value < 0:
            raise serializers.ValidationError("Unit price cannot be negative.")
        return value


class CustomerPurchaseSerializer(serializers.ModelSerializer):
    """Serializer for customer purchase/order."""
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    items = CustomerPurchaseItemSerializer(many=True, read_only=True)
    invoice_file_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomerPurchase
        fields = [
            "purchase_id",
            "customer",
            "customer_name",
            "invoice_number",
            "invoice_date",
            "invoice_file",
            "invoice_file_url",
            "total_amount",
            "items",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "purchase_id": {"read_only": True},
            "invoice_number": {"read_only": True},
            "invoice_date": {"read_only": True},
            "invoice_file": {"read_only": True},
            "invoice_file_url": {"read_only": True},
            "customer_name": {"read_only": True},
            "items": {"read_only": True},
            "total_amount": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
        }

    def get_invoice_file_url(self, obj):
        if obj.invoice_file:
            return f"/media/{obj.invoice_file}"
        return None


class CustomerPaymentSerializer(serializers.ModelSerializer):
    """Serializer for customer payments."""
    customer_name = serializers.SerializerMethodField()
    invoice_total = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    payment_proof_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomerPayment
        fields = [
            "payment_id",
            "invoice_number",
            "customer_purchase",
            "customer_name",
            "invoice_total",
            "amount",
            "payment_mode",
            "payment_date",
            "payment_status",
            "payment_proof",
            "payment_proof_url",
            "note",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "payment_id": {"read_only": True},
            "invoice_number": {"required": False},  # Will be auto-populated from customer_purchase
            "customer_name": {"read_only": True},
            "invoice_total": {"read_only": True},
            "payment_status": {"read_only": True},
            "payment_proof": {"required": False, "allow_blank": True, "allow_null": True},
            "payment_proof_url": {"read_only": True},
            "note": {"required": False, "allow_blank": True, "allow_null": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
        }

    def get_customer_name(self, obj):
        return obj.customer_purchase.customer.full_name if obj.customer_purchase and obj.customer_purchase.customer else None

    def get_invoice_total(self, obj):
        return obj.customer_purchase.total_amount if obj.customer_purchase else None

    def get_payment_status(self, obj):
        return "received"

    def get_payment_proof_url(self, obj):
        """
        Build full URL for payment proof file.
        The payment_proof is stored as media/proof/filename in the database (same pattern as supplier payment).
        We prepend the host URL and / to make it a full accessible URL.
        """
        if obj.payment_proof:
            request = self.context.get('request')
            # payment_proof is stored as media/proof/filename (without leading /)
            # Add leading / for URL construction
            proof_path = obj.payment_proof if obj.payment_proof.startswith('/') else f"/{obj.payment_proof}"
            if request:
                scheme = request.scheme
                host = request.get_host()
                return f"{scheme}://{host}{proof_path}"
            return proof_path
        return None

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than 0.")
        return value

    def validate(self, data):
        """
        Validate that the payment amount does not exceed the outstanding invoice amount.
        Also automatically fetch invoice_number from customer_purchase.
        """
        customer_purchase = data.get('customer_purchase')
        amount = data.get('amount')
        
        # Automatically fetch invoice_number from customer_purchase
        if customer_purchase:
            # Handle case where customer_purchase might be an ID (integer) or an object
            if isinstance(customer_purchase, (int, str)):
                try:
                    customer_purchase = CustomerPurchase.objects.get(purchase_id=int(customer_purchase))
                except CustomerPurchase.DoesNotExist:
                    raise serializers.ValidationError({
                        'customer_purchase': 'Invalid customer purchase ID.'
                    })
            
            # Set invoice_number from customer_purchase
            data['invoice_number'] = customer_purchase.invoice_number
        elif not data.get('invoice_number'):
            raise serializers.ValidationError({
                'customer_purchase': 'customer_purchase is required to automatically fetch invoice_number.'
            })
        
        # Validate payment amount doesn't exceed outstanding amount
        if customer_purchase and amount:
            # Get total amount from customer purchase
            total_amount = float(customer_purchase.total_amount or 0)
            
            # Calculate total already paid (excluding current payment if updating)
            payment_agg = CustomerPayment.objects.filter(
                customer_purchase_id=customer_purchase.purchase_id
            )
            
            # If updating an existing payment, exclude it from the total paid calculation
            if self.instance:
                payment_agg = payment_agg.exclude(payment_id=self.instance.payment_id)
            
            total_paid = float(payment_agg.aggregate(total_paid=Sum("amount"))["total_paid"] or 0)
            
            # Calculate outstanding amount
            outstanding_amount = max(total_amount - total_paid, 0)
            
            # Validate that payment amount doesn't exceed outstanding amount
            if float(amount) > outstanding_amount:
                raise serializers.ValidationError({
                    'amount': f'Payment amount (₹{amount:.2f}) cannot exceed the outstanding invoice amount (₹{outstanding_amount:.2f}). '
                              f'Invoice total: ₹{total_amount:.2f}, Already paid: ₹{total_paid:.2f}, Outstanding: ₹{outstanding_amount:.2f}'
                })
        
        return data

    def create(self, validated_data):
        """Ensure invoice_number is set from customer_purchase."""
        customer_purchase = validated_data.get('customer_purchase')
        if customer_purchase and not validated_data.get('invoice_number'):
            validated_data['invoice_number'] = customer_purchase.invoice_number
        return super().create(validated_data)


class CategorySerializer(serializers.ModelSerializer):
    """
    Serializer for Category model.
    """
    category_id = serializers.IntegerField(read_only=True)
    seller_user = serializers.PrimaryKeyRelatedField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Category
        fields = [
            "category_id",
            "category_name",
            "seller_user",
            "description",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "category_name": {"required": True},
            "description": {"required": False, "allow_blank": True},
        }

    def validate_category_name(self, value):
        """Validate that category name is not empty."""
        if not value or not value.strip():
            raise serializers.ValidationError("Category name cannot be empty.")
        return value.strip()

    def create(self, validated_data):
        """Create a category and set seller_user from request context."""
        # Get seller_user from context (set by the view)
        seller_user = self.context.get('seller_user')
        if seller_user:
            validated_data['seller_user'] = seller_user
        return super().create(validated_data)


class LiveChatSessionSerializer(serializers.ModelSerializer):
    """
    Seller-facing serializer: do NOT expose token.
    """

    seller_user = serializers.PrimaryKeyRelatedField(read_only=True)
    source_bot = serializers.PrimaryKeyRelatedField(read_only=True)
    customer = serializers.PrimaryKeyRelatedField(read_only=True)
    visitor_name = serializers.CharField(read_only=True)
    visitor_mobile_number = serializers.CharField(read_only=True)
    visitor_email = serializers.CharField(read_only=True)
    visitor_payload = serializers.JSONField(read_only=True)

    class Meta:
        model = LiveChatSession
        fields = [
            "session_id",
            "customer",
            "seller_user",
            "source_bot",
            "visitor_name",
            "visitor_mobile_number",
            "visitor_email",
            "visitor_payload",
            "lead_pipeline_status",
            "created_at",
            "updated_at",
        ]


class LiveChatSessionPublicSerializer(serializers.ModelSerializer):
    """
    Customer-facing serializer (bot widget): expose token only.
    """

    seller_user = serializers.PrimaryKeyRelatedField(read_only=True)
    source_bot = serializers.PrimaryKeyRelatedField(read_only=True)
    customer = serializers.PrimaryKeyRelatedField(read_only=True)
    visitor_name = serializers.CharField(read_only=True)
    visitor_mobile_number = serializers.CharField(read_only=True)
    visitor_email = serializers.CharField(read_only=True)
    visitor_payload = serializers.JSONField(read_only=True)

    class Meta:
        model = LiveChatSession
        fields = [
            "session_id",
            "customer",
            "seller_user",
            "source_bot",
            "visitor_name",
            "visitor_mobile_number",
            "visitor_email",
            "visitor_payload",
            "public_token",
            "created_at",
            "updated_at",
        ]


class LiveChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = LiveChatMessage
        fields = [
            "message_id",
            "sender_type",
            "message",
            "created_at",
        ]


class LiveChatSupportSessionListSerializer(serializers.ModelSerializer):
    """
    Seller-side inbox row + right panel details.
    Expects `last_message_text`, `last_message_created_at`, `last_message_sender_type`
    to be provided via ORM annotations in the view.
    """

    customer_id = serializers.IntegerField(source="customer.customer_id", read_only=True)
    customer_name = serializers.CharField(source="customer.full_name", read_only=True)
    mobile_number = serializers.CharField(source="customer.mobile_number", read_only=True)
    whatsapp_number = serializers.CharField(source="customer.whatsapp_number", read_only=True)
    email = serializers.EmailField(source="customer.email", read_only=True)
    bot_payload = serializers.JSONField(source="customer.bot_payload", read_only=True)

    last_message_text = serializers.CharField(read_only=True)
    last_message_created_at = serializers.DateTimeField(read_only=True)
    last_message_sender_type = serializers.CharField(read_only=True)

    class Meta:
        model = LiveChatSession
        fields = [
            "session_id",
            "customer_id",
            "customer_name",
            "mobile_number",
            "whatsapp_number",
            "email",
            "bot_payload",
            "last_message_text",
            "last_message_created_at",
            "last_message_sender_type",
            "created_at",
            "updated_at",
        ]


class WhatsAppCampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppCampaign
        fields = "__all__"


class WhatsAppCampaignRecipientSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppCampaignRecipient
        fields = "__all__"
