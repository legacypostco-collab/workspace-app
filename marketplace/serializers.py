from rest_framework import serializers

from .models import Category, Order, OrderItem, Part


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "slug"]


class PartSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)
    seller_username = serializers.CharField(source="seller.username", read_only=True)
    brand_name = serializers.CharField(source="brand.name", read_only=True)
    is_mandatory_complete = serializers.BooleanField(read_only=True)
    mandatory_missing_fields = serializers.SerializerMethodField()

    class Meta:
        model = Part
        fields = [
            "id",
            "title",
            "slug",
            "oem_number",
            "description",
            "price",
            "stock_quantity",
            "condition",
            "image_url",
            "is_active",
            "availability",
            "availability_status",
            "currency",
            "incoterm",
            "moq",
            "country_of_origin",
            "is_mandatory_complete",
            "mandatory_missing_fields",
            "category",
            "brand_name",
            "seller_username",
            "created_at",
        ]

    def get_mandatory_missing_fields(self, obj: Part):
        return obj.mandatory_missing_fields()


class OrderItemSerializer(serializers.ModelSerializer):
    part = PartSerializer(read_only=True)
    total_price = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ["id", "part", "quantity", "unit_price", "total_price"]

    def get_total_price(self, obj: OrderItem):
        return obj.total_price


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "customer_name",
            "customer_email",
            "customer_phone",
            "delivery_address",
            "status",
            "supplier_confirm_deadline",
            "ship_deadline",
            "sla_status",
            "sla_breaches_count",
            "logistics_cost",
            "logistics_currency",
            "logistics_provider",
            "total_amount",
            "created_at",
            "items",
        ]
