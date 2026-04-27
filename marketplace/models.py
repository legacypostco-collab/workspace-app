from decimal import Decimal

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Category(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)

    def __str__(self) -> str:
        return self.name


class Brand(models.Model):
    REGION_CHOICES = [
        ("global", "Global"),
        ("korea", "Korea"),
        ("china", "China"),
        ("europe", "Europe"),
        ("components", "Component Manufacturer"),
    ]

    name = models.CharField(max_length=140, unique=True)
    slug = models.SlugField(max_length=180, unique=True)
    region = models.CharField(max_length=20, choices=REGION_CHOICES, default="global")
    is_component_manufacturer = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Part(models.Model):
    CONDITION_CHOICES = [
        ("oem", "OEM"),
        ("aftermarket", "Aftermarket"),
        ("reman", "REMAN"),
    ]

    AVAILABILITY_CHOICES = [
        ("in_stock", "IN_STOCK"),
        ("backorder", "BACKORDER"),
    ]
    AVAILABILITY_STATUS_CHOICES = [
        ("active", "active"),
        ("limited", "limited"),
        ("made_to_order", "made_to_order"),
        ("discontinued", "discontinued"),
        ("blocked", "blocked"),
    ]
    CURRENCY_CHOICES = [
        ("USD", "USD"),
        ("EUR", "EUR"),
        ("RUB", "RUB"),
        ("CNY", "CNY"),
    ]
    INCOTERM_CHOICES = [
        ("FOB", "FOB"),
        ("CIF", "CIF"),
        ("DDP", "DDP"),
    ]
    MAPPING_STATUS_CHOICES = [
        ("auto", "auto"),
        ("confirmed", "confirmed"),
        ("needs_review", "needs_review"),
    ]

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True)
    oem_number = models.CharField(max_length=100, db_index=True)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    stock_quantity = models.PositiveIntegerField(default=0)
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default="oem")
    image_url = models.URLField(blank=True)
    seller = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="parts")
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name="parts")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="parts")
    availability = models.CharField(max_length=20, choices=AVAILABILITY_CHOICES, default="in_stock")
    availability_status = models.CharField(max_length=20, choices=AVAILABILITY_STATUS_CHOICES, default="active")
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="USD")
    incoterm = models.CharField(max_length=3, choices=INCOTERM_CHOICES, default="FOB")
    moq = models.PositiveIntegerField(default=1)
    production_lead_days = models.PositiveIntegerField(default=1)
    prep_to_ship_days = models.PositiveIntegerField(default=1)
    shipping_lead_days = models.PositiveIntegerField(default=1)
    gross_weight_kg = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal("0.100"))
    length_cm = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    width_cm = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    height_cm = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    country_of_origin = models.CharField(max_length=120, default="Unknown")
    cross_numbers = models.CharField(max_length=500, blank=True)
    backorder_allowed = models.BooleanField(default=False)
    mapping_status = models.CharField(max_length=20, choices=MAPPING_STATUS_CHOICES, default="auto")
    supplier_part_uid = models.CharField(max_length=80, blank=True)
    data_updated_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    admin_note = models.TextField(blank=True, help_text="Комментарий администратора (причина блокировки и т.д.)")
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="moderated_parts")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["seller", "-data_updated_at", "-id"], name="part_seller_updated_idx"),
            models.Index(fields=["seller", "availability_status"], name="part_seller_avail_idx"),
            models.Index(fields=["seller", "is_active"], name="part_seller_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.oem_number})"

    def mandatory_missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.oem_number:
            missing.append("oem_number")
        if not self.title:
            missing.append("title")
        if self.price is None or self.price <= 0:
            missing.append("price")
        if not self.currency:
            missing.append("currency")
        if not self.incoterm:
            missing.append("incoterm")
        if self.moq <= 0:
            missing.append("moq")
        if self.production_lead_days < 0:
            missing.append("production_lead_days")
        if self.prep_to_ship_days < 0:
            missing.append("prep_to_ship_days")
        if self.shipping_lead_days < 0:
            missing.append("shipping_lead_days")
        if self.gross_weight_kg <= 0:
            missing.append("gross_weight_kg")
        if self.length_cm <= 0 or self.width_cm <= 0 or self.height_cm <= 0:
            missing.append("dimensions")
        if not self.country_of_origin:
            missing.append("country_of_origin")
        if self.availability == "in_stock" and self.stock_quantity <= 0:
            missing.append("stock_quantity")
        if self.availability == "backorder" and not self.backorder_allowed:
            missing.append("backorder_allowed")
        if self.availability_status in {"blocked", "discontinued"}:
            missing.append("availability_status")
        if self.mapping_status == "needs_review":
            missing.append("mapping_status")
        return missing

    @property
    def is_mandatory_complete(self) -> bool:
        return len(self.mandatory_missing_fields()) == 0

    @property
    def is_eligible_for_matching(self) -> bool:
        if not self.is_active:
            return False
        if self.availability_status not in {"active", "limited", "made_to_order"}:
            return False
        return self.is_mandatory_complete


class Drawing(models.Model):
    """Чертёж / CAD-файл, привязанный к детали поставщика."""

    FORMAT_CHOICES = [
        ("pdf", "PDF"),
        ("dwg", "DWG"),
        ("dxf", "DXF"),
        ("step", "STEP"),
        ("iges", "IGES"),
        ("stl", "STL"),
        ("png", "PNG"),
        ("jpg", "JPG"),
    ]
    STATUS_CHOICES = [
        ("draft", "Черновик"),
        ("on_review", "На проверке"),
        ("approved", "Утверждён"),
        ("rejected", "Отклонён"),
        ("archived", "Архив"),
    ]

    title = models.CharField(max_length=255)
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="drawings", null=True, blank=True)
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name="drawings")
    file_format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default="pdf")
    file_url = models.URLField(blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size_kb = models.PositiveIntegerField(default=0)
    revision = models.CharField(max_length=20, default="A")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    description = models.TextField(blank=True)
    oem_number = models.CharField(max_length=100, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.title} (rev {self.revision})"


class RFQ(models.Model):
    MODE_CHOICES = [
        ("auto", "AUTO"),
        ("semi", "SEMI"),
        ("manual_oem", "MANUAL OEM"),
    ]
    URGENCY_CHOICES = [
        ("standard", "Standard"),
        ("urgent", "Urgent"),
        ("critical", "Critical"),
    ]
    STATUS_CHOICES = [
        ("new", "New"),
        ("quoted", "Quoted"),
        ("needs_review", "Needs Review"),
        ("cancelled", "Cancelled"),
    ]

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="rfqs")
    customer_name = models.CharField(max_length=180)
    customer_email = models.EmailField()
    company_name = models.CharField(max_length=255, blank=True)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default="semi")
    urgency = models.CharField(max_length=20, choices=URGENCY_CHOICES, default="standard")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new")
    notes = models.TextField(blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Общая скидка на весь RFQ (%)")
    discount_note = models.CharField(max_length=255, blank=True, help_text="Комментарий к скидке")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"RFQ #{self.id} - {self.customer_name}"

    @property
    def estimated_total(self):
        return sum((item.estimated_line_total for item in self.items.all()), 0)


class RFQItem(models.Model):
    STATE_CHOICES = [
        ("new", "NEW"),
        ("auto_matched", "AUTO MATCHED"),
        ("needs_review", "NEEDS REVIEW"),
        ("oem_manual", "OEM MANUAL"),
    ]

    rfq = models.ForeignKey(RFQ, on_delete=models.CASCADE, related_name="items")
    query = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    matched_part = models.ForeignKey(Part, on_delete=models.SET_NULL, null=True, blank=True, related_name="rfq_items")
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default="new")
    confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    recommended_supplier_status = models.CharField(max_length=20, blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Скидка на позицию (%)")
    discount_fixed = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Фиксированная скидка ($)")

    def __str__(self) -> str:
        return f"{self.query} x{self.quantity}"

    @property
    def estimated_line_total(self):
        if not self.matched_part:
            return 0
        return self.matched_part.price * self.quantity


class Order(models.Model):
    STATUS_CHOICES = [
        ("pending", "Ожидание оплаты"),
        ("reserve_paid", "Резерв оплачен"),
        ("confirmed", "Формирование заказа"),
        ("in_production", "В производстве"),
        ("ready_to_ship", "Готов к отгрузке"),
        ("transit_abroad", "Транзит (Зарубеж)"),
        ("customs", "Таможня"),
        ("transit_rf", "Транзит (РФ)"),
        ("issuing", "Выдача"),
        ("shipped", "Отгружен"),
        ("delivered", "Доставлен"),
        ("completed", "Завершён"),
        ("cancelled", "Отменён"),
    ]
    PAYMENT_STATUS_CHOICES = [
        ("awaiting_reserve", "Ожидает резерва"),
        ("reserve_paid", "Резерв оплачен"),
        ("mid_paid", "Подтверждение оплачено"),
        ("customs_paid", "Таможня оплачена"),
        ("paid", "Оплачен"),
        ("refund_pending", "Возврат в обработке"),
        ("refunded", "Возвращён"),
    ]
    PAYMENT_SCHEME_CHOICES = [
        ("simple", "10% + 90%"),
        ("staged", "10% + 50% + 40%"),
    ]
    SLA_STATUS_CHOICES = [
        ("on_track", "В норме"),
        ("at_risk", "Под угрозой"),
        ("breached", "Нарушен"),
    ]

    customer_name = models.CharField(max_length=180)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=50)
    delivery_address = models.TextField()
    buyer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    supplier_confirm_deadline = models.DateTimeField(null=True, blank=True)
    ship_deadline = models.DateTimeField(null=True, blank=True)
    sla_status = models.CharField(max_length=20, choices=SLA_STATUS_CHOICES, default="on_track")
    sla_breaches_count = models.PositiveIntegerField(default=0)
    logistics_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    logistics_currency = models.CharField(max_length=10, default="USD")
    logistics_provider = models.CharField(max_length=60, default="internal_fallback")
    logistics_meta = models.JSONField(default=dict, blank=True)
    invoice_number = models.CharField(max_length=80, blank=True)
    payment_status = models.CharField(max_length=30, choices=PAYMENT_STATUS_CHOICES, default="awaiting_reserve")
    payment_scheme = models.CharField(max_length=20, choices=PAYMENT_SCHEME_CHOICES, default="simple")
    reserve_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("10.00"))
    reserve_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reserve_paid_at = models.DateTimeField(null=True, blank=True)
    mid_payment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    mid_paid_at = models.DateTimeField(null=True, blank=True)
    customs_payment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    customs_paid_at = models.DateTimeField(null=True, blank=True)
    final_paid_at = models.DateTimeField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["buyer", "status"], name="order_buyer_status_idx"),
            models.Index(fields=["status", "sla_status"], name="order_status_sla_idx"),
            models.Index(fields=["buyer", "-created_at"], name="order_buyer_created_idx"),
        ]

    def __str__(self) -> str:
        return f"Order #{self.id} - {self.customer_name}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    part = models.ForeignKey(Part, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    @property
    def total_price(self):
        return self.quantity * self.unit_price


class OrderEvent(models.Model):
    EVENT_CHOICES = [
        ("order_created", "Order Created"),
        ("status_changed", "Status Changed"),
        ("sla_status_changed", "SLA Status Changed"),
        ("invoice_opened", "Invoice Opened"),
        ("reserve_paid", "Reserve Paid"),
        ("mid_payment_paid", "Mid Payment Paid"),
        ("customs_payment_paid", "Customs Payment Paid"),
        ("final_payment_paid", "Final Payment Paid"),
        ("quality_confirmed", "Quality Confirmed"),
        ("document_uploaded", "Document Uploaded"),
        ("claim_opened", "Claim Opened"),
        ("claim_status_changed", "Claim Status Changed"),
    ]
    SOURCE_CHOICES = [
        ("system", "System"),
        ("buyer", "Buyer"),
        ("seller", "Seller"),
        ("operator", "Operator"),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="system")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_events")
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "event_type"], name="event_order_type_idx"),
        ]

    def __str__(self) -> str:
        return f"Order #{self.order_id} {self.event_type}"


class OrderDocument(models.Model):
    DOC_TYPE_CHOICES = [
        ("invoice", "Invoice"),
        ("packing_list", "Packing List"),
        ("certificate", "Certificate"),
        ("quality_report", "Quality Report"),
        ("customs", "Customs"),
        ("other", "Other"),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=30, choices=DOC_TYPE_CHOICES, default="other")
    title = models.CharField(max_length=255)
    file_url = models.URLField(blank=True)
    file_obj = models.FileField(upload_to="order_documents/%Y/%m/%d", blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_order_documents")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Order #{self.order_id} {self.title}"


class OrderClaim(models.Model):
    STATUS_CHOICES = [
        ("open", "Open"),
        ("in_review", "In Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("closed", "Closed"),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="claims")
    title = models.CharField(max_length=255)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    opened_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="opened_claims")
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="resolved_claims")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Claim #{self.id} for Order #{self.order_id}"


class WebhookDeliveryLog(models.Model):
    order_event = models.ForeignKey(OrderEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_logs")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="webhook_logs")
    endpoint = models.URLField()
    success = models.BooleanField(default=False)
    attempt = models.PositiveIntegerField(default=1)
    status_code = models.IntegerField(null=True, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_body = models.TextField(blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Webhook {self.endpoint} order#{self.order_id} success={self.success} attempt={self.attempt}"


class UserProfile(models.Model):
    ROLE_CHOICES = [
        ("buyer", "Buyer"),
        ("seller", "Seller"),
    ]
    SUPPLIER_STATUS_CHOICES = [
        ("trusted", "Надёжный"),
        ("sandbox", "Песочница"),
        ("risky", "Рисковый"),
        ("rejected", "Исключён"),
    ]

    DEPARTMENT_CHOICES = [
        ("director", "Director"),
        ("sales", "Sales"),
        ("logistics", "Logistics"),
        ("finance", "Finance"),
        ("engineering", "Engineering"),
        ("viewer", "Viewer"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="buyer")
    company_name = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=20, choices=DEPARTMENT_CHOICES, default="director")
    allowed_regions = models.CharField(max_length=255, blank=True, help_text="CSV list: europe,china,components,...")
    allowed_brands = models.ManyToManyField(Brand, blank=True, related_name="allowed_profiles")
    can_manage_assortment = models.BooleanField(default=True)
    can_manage_pricing = models.BooleanField(default=True)
    can_manage_orders = models.BooleanField(default=True)
    can_manage_drawings = models.BooleanField(default=False)
    can_view_analytics = models.BooleanField(default=True)
    can_manage_team = models.BooleanField(default=False)
    external_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("60.00"))
    behavioral_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("60.00"))
    rating_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("60.00"), editable=False)
    supplier_status = models.CharField(
        max_length=20,
        choices=SUPPLIER_STATUS_CHOICES,
        default="sandbox",
        editable=False,
    )
    bankruptcy_flag = models.BooleanField(default=False)
    liquidation_flag = models.BooleanField(default=False)
    last_rating_recalculated_at = models.DateTimeField(null=True, blank=True, editable=False)
    admin_note = models.TextField(blank=True, help_text="Заметка администратора о поставщике")

    @staticmethod
    def _clamp_score(value: Decimal) -> Decimal:
        if value < 0:
            return Decimal("0.00")
        if value > 100:
            return Decimal("100.00")
        return value.quantize(Decimal("0.01"))

    def recalculate_supplier_rating(self):
        if self.role != "seller":
            self.rating_score = Decimal("0.00")
            self.supplier_status = "sandbox"
            self.last_rating_recalculated_at = timezone.now()
            return

        if self.bankruptcy_flag or self.liquidation_flag:
            self.rating_score = Decimal("0.00")
            self.supplier_status = "rejected"
            self.last_rating_recalculated_at = timezone.now()
            return

        external = self._clamp_score(Decimal(self.external_score))
        behavioral = self._clamp_score(Decimal(self.behavioral_score))
        score = (external * Decimal("0.6")) + (behavioral * Decimal("0.4"))
        score = self._clamp_score(score)
        self.rating_score = score

        if score >= 80:
            self.supplier_status = "trusted"
        elif score >= 60:
            self.supplier_status = "sandbox"
        elif score >= 0:
            self.supplier_status = "risky"
        self.last_rating_recalculated_at = timezone.now()

    def save(self, *args, **kwargs):
        self.recalculate_supplier_rating()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.user.username} ({self.role})"


class SupplierRatingEvent(models.Model):
    EVENT_CHOICES = [
        ("rfq_response", "RFQ Response"),
        ("data_mismatch", "Data Mismatch"),
        ("delivery_delay", "Delivery Delay"),
        ("order_cancellation", "Order Cancellation"),
        ("return", "Return"),
        ("sandbox_selected", "Sandbox Selected"),
        ("risky_selected", "Risky Selected"),
        ("manual_oem_escalation", "Manual OEM Escalation"),
    ]

    supplier = models.ForeignKey(User, on_delete=models.CASCADE, related_name="supplier_rating_events")
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES)
    impact_score = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.supplier_id}:{self.event_type}:{self.impact_score}"


class SellerImportRun(models.Model):
    MODE_CHOICES = [
        ("preview", "Preview"),
        ("apply", "Apply"),
    ]
    STATUS_CHOICES = [
        ("success", "Success"),
        ("failed", "Failed"),
    ]

    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name="import_runs")
    filename = models.CharField(max_length=255, blank=True, default="")
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default="apply")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="success")
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_no_price_count = models.PositiveIntegerField(default=0)
    skipped_invalid_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.seller_id}:{self.filename}:{self.status}:{self.created_at.isoformat()}"
