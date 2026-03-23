from django.conf import settings
from django.db import models


class SupplierOffer(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    class Condition(models.TextChoices):
        ORIGINAL = "ORIGINAL", "Original"
        OEM = "OEM", "OEM"
        AFTERMARKET = "AFTERMARKET", "Aftermarket"
        REMAN = "REMAN", "Reman"

    class AvailabilityStatus(models.TextChoices):
        IN_STOCK = "in_stock", "In Stock"
        LIMITED = "limited", "Limited"
        OUT_OF_STOCK = "out_of_stock", "Out of Stock"

    supplier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="supplier_offers",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="supplier_offers",
    )
    price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    condition = models.CharField(max_length=20, choices=Condition.choices, default=Condition.OEM)
    quantity = models.IntegerField(null=True, blank=True)
    warehouse_address = models.CharField(max_length=255, blank=True, default="")
    sea_port = models.CharField(max_length=120, blank=True, default="")
    air_port = models.CharField(max_length=120, blank=True, default="")
    weight = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    length = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    width = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    height = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    availability_status = models.CharField(
        max_length=20,
        choices=AvailabilityStatus.choices,
        default=AvailabilityStatus.IN_STOCK,
    )
    is_hidden = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_import_job = models.ForeignKey(
        "imports.ImportJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_offers_last_import",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["supplier", "product", "condition"],
                name="uq_supplier_offer_supplier_product_condition",
            )
        ]
        indexes = [
            models.Index(fields=["supplier", "status"]),
            models.Index(fields=["product"]),
            models.Index(fields=["last_import_job"]),
            models.Index(fields=["supplier", "updated_at"]),
            models.Index(fields=["supplier", "last_synced_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_id}:{self.product_id}:{self.price}"


class SupplierOfferPrice(models.Model):
    class IncotermBasis(models.TextChoices):
        EXW = "EXW", "EXW"
        FOB = "FOB", "FOB"

    class TransportMode(models.TextChoices):
        NONE = "NONE", "None"
        SEA = "SEA", "Sea"
        AIR = "AIR", "Air"

    supplier_offer = models.ForeignKey(
        SupplierOffer,
        on_delete=models.CASCADE,
        related_name="prices",
    )
    incoterm_basis = models.CharField(max_length=10, choices=IncotermBasis.choices)
    transport_mode = models.CharField(max_length=10, choices=TransportMode.choices, default=TransportMode.NONE)
    price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    source_import_run = models.ForeignKey(
        "imports.ImportJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offer_price_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["supplier_offer", "incoterm_basis", "transport_mode"],
                name="uq_supplier_offer_price_scenario",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_offer_id}:{self.incoterm_basis}:{self.transport_mode}:{self.price}"
