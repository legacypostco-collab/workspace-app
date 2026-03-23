from django.conf import settings
from django.db import models


class Product(models.Model):
    brand = models.ForeignKey(
        "marketplace.Brand",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="catalog_products",
    )
    category = models.ForeignKey(
        "marketplace.Category",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="catalog_products",
    )
    part_number = models.CharField(max_length=255, blank=True, default="")
    normalized_part_number = models.CharField(max_length=255, blank=True, default="")
    oem_raw = models.CharField(max_length=255, blank=True, default="")
    oem_normalized = models.CharField(max_length=255)
    brand_raw = models.CharField(max_length=255, blank=True, default="")
    brand_normalized = models.CharField(max_length=255, blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_by_supplier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="catalog_products_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["oem_normalized", "brand_normalized"],
                name="uq_product_oem_brand_normalized",
            ),
            models.UniqueConstraint(
                fields=["brand", "normalized_part_number"],
                name="uq_product_brand_part_number",
            ),
        ]
        indexes = [
            models.Index(fields=["oem_normalized"]),
            models.Index(fields=["oem_normalized", "brand_normalized"]),
            models.Index(fields=["brand_normalized"]),
            models.Index(fields=["normalized_part_number"]),
        ]

    def __str__(self) -> str:
        return f"{self.oem_normalized}:{self.brand_normalized or '-'}"


class CatalogChangeLog(models.Model):
    class ChangeType(models.TextChoices):
        PRODUCT_CREATED = "product_created", "Product Created"
        PRODUCT_UPDATED = "product_updated", "Product Updated"
        SUPPLIER_OFFER_CREATED = "supplier_offer_created", "Supplier Offer Created"
        SUPPLIER_OFFER_UPDATED = "supplier_offer_updated", "Supplier Offer Updated"

    supplier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="catalog_change_logs",
    )
    import_job = models.ForeignKey(
        "imports.ImportJob",
        on_delete=models.CASCADE,
        related_name="catalog_changes",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="change_logs",
    )
    supplier_offer = models.ForeignKey(
        "offers.SupplierOffer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="change_logs",
    )
    change_type = models.CharField(max_length=40, choices=ChangeType.choices)
    details = models.JSONField(default=dict, blank=True)
    payload_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["import_job", "created_at"]),
            models.Index(fields=["supplier", "created_at"]),
            models.Index(fields=["product", "created_at"]),
            models.Index(fields=["change_type", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.import_job_id}:{self.change_type}:{self.product_id}"


class ProductCrossReference(models.Model):
    class CrossType(models.TextChoices):
        ANALOG = "analog", "Analog"
        REPLACEMENT = "replacement", "Replacement"
        OTHER = "other", "Other"

    class Source(models.TextChoices):
        IMPORT = "import", "Import"
        MANUAL = "manual", "Manual"

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="cross_references",
    )
    cross_number = models.CharField(max_length=255)
    normalized_cross_number = models.CharField(max_length=255)
    cross_type = models.CharField(max_length=20, choices=CrossType.choices, default=CrossType.ANALOG)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.IMPORT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "normalized_cross_number"],
                name="uq_product_cross_ref_product_number",
            ),
        ]
        indexes = [
            models.Index(fields=["normalized_cross_number"]),
        ]

    def __str__(self) -> str:
        return f"{self.product_id}:{self.normalized_cross_number}"
