from django.conf import settings
from django.db import models


class ImportPreviewSession(models.Model):
    class SourceType(models.TextChoices):
        CSV = "csv", "CSV File"
        GOOGLE_SHEET = "google_sheet", "Google Sheet URL"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        MAPPING_CONFIRMED = "mapping_confirmed", "Mapping Confirmed"
        EXPIRED = "expired", "Expired"

    supplier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="import_preview_sessions",
    )
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    source_file = models.ForeignKey(
        "files.StoredFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_preview_sessions",
    )
    source_url = models.URLField(blank=True, default="")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)
    detected_columns = models.JSONField(default=dict, blank=True)
    sample_rows = models.JSONField(default=list, blank=True)
    column_mapping = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["supplier", "created_at"]),
            models.Index(fields=["supplier", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.id}:{self.supplier_id}:{self.status}"


class ImportJob(models.Model):
    class SourceType(models.TextChoices):
        CSV = "csv", "CSV File"
        GOOGLE_SHEET = "google_sheet", "Google Sheet URL"

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        PARTIAL_SUCCESS = "partial_success", "Partial Success"
        FAILED = "failed", "Failed"

    supplier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="import_jobs",
    )
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    source_file = models.ForeignKey(
        "files.StoredFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    source_url = models.URLField(blank=True, default="")
    preview_session = models.ForeignKey(
        "imports.ImportPreviewSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jobs",
    )
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.QUEUED)
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    column_mapping_json = models.JSONField(default=dict, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    rows_total = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    rows_created_products = models.PositiveIntegerField(default=0)
    rows_created_offers = models.PositiveIntegerField(default=0)
    rows_updated_offers = models.PositiveIntegerField(default=0)
    rows_updated_prices = models.PositiveIntegerField(default=0)
    rows_failed = models.PositiveIntegerField(default=0)
    rows_matched_by_cross_number = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    created_products = models.PositiveIntegerField(default=0)
    updated_offers = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")
    summary_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["supplier", "created_at"]),
            models.Index(fields=["supplier", "status"]),
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["idempotency_key"]),
        ]

    def __str__(self) -> str:
        return f"{self.id}:{self.supplier_id}:{self.status}"


class ImportRow(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        VALID = "valid", "Valid"
        UPSERTED = "upserted", "Upserted"
        INVALID = "invalid", "Invalid"

    class ValidationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        VALID = "valid", "Valid"
        INVALID = "invalid", "Invalid"

    class MatchStatus(models.TextChoices):
        NOT_PROCESSED = "not_processed", "Not processed"
        MATCHED = "matched", "Matched"
        MATCHED_BY_CROSS = "matched_by_cross", "Matched by Cross"
        CREATED_NEW_PRODUCT = "created_new_product", "Created New Product"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        FAILED = "failed", "Failed"

    job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="rows",
    )
    row_no = models.PositiveIntegerField()
    row_number = models.PositiveIntegerField(null=True, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    normalized_payload = models.JSONField(default=dict, blank=True)
    part_number_raw = models.CharField(max_length=255, blank=True, default="")
    part_number_normalized = models.CharField(max_length=255, blank=True, default="")
    cross_number_raw = models.CharField(max_length=255, blank=True, default="")
    cross_number_normalized = models.CharField(max_length=255, blank=True, default="")
    normalized_oem = models.CharField(max_length=255, blank=True, default="")
    normalized_brand = models.CharField(max_length=255, blank=True, default="")
    parsed_name = models.CharField(max_length=255, blank=True, default="")
    parsed_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    parsed_quantity = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    validation_status = models.CharField(
        max_length=20,
        choices=ValidationStatus.choices,
        default=ValidationStatus.PENDING,
    )
    match_status = models.CharField(
        max_length=30,
        choices=MatchStatus.choices,
        default=MatchStatus.NOT_PROCESSED,
    )
    matched_by = models.CharField(max_length=30, blank=True, default="")
    error_code = models.CharField(max_length=80, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    error_hint = models.TextField(blank=True, default="")
    matched_product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_rows",
    )
    matched_supplier_offer = models.ForeignKey(
        "offers.SupplierOffer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_rows",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_rows_product",
    )
    supplier_offer = models.ForeignKey(
        "offers.SupplierOffer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_rows_supplier_offer",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "row_no"],
                name="uq_import_row_job_row_no",
            )
        ]
        indexes = [
            models.Index(fields=["job", "status"]),
            models.Index(fields=["job", "row_no"]),
            models.Index(fields=["normalized_oem"]),
            models.Index(fields=["normalized_oem", "normalized_brand"]),
            models.Index(fields=["job", "validation_status"]),
            models.Index(fields=["job", "match_status"]),
            models.Index(fields=["part_number_normalized"]),
            models.Index(fields=["cross_number_normalized"]),
        ]

    def __str__(self) -> str:
        return f"{self.job_id}:{self.row_no}:{self.status}"


class ImportErrorReport(models.Model):
    class ReportFormat(models.TextChoices):
        CSV = "csv", "CSV"
        JSON = "json", "JSON"

    job = models.OneToOneField(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="error_report",
    )
    file = models.ForeignKey(
        "files.StoredFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_error_reports",
    )
    report_format = models.CharField(max_length=10, choices=ReportFormat.choices, default=ReportFormat.CSV)
    error_count = models.PositiveIntegerField(default=0)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["generated_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.job_id}:{self.report_format}:{self.error_count}"
