from django.conf import settings
from django.db import models


class StoredFile(models.Model):
    class SourceType(models.TextChoices):
        IMPORT_CSV = "import_csv", "Import CSV"
        IMPORT_GOOGLE_SHEET = "import_google_sheet", "Import Google Sheet"
        IMPORT_ERROR_REPORT = "import_error_report", "Import Error Report"
        OTHER = "other", "Other"

    class StorageProvider(models.TextChoices):
        LOCAL = "local", "Local"
        S3 = "s3", "S3"

    supplier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stored_files",
    )
    source_type = models.CharField(max_length=40, choices=SourceType.choices, default=SourceType.OTHER)
    storage_provider = models.CharField(max_length=20, choices=StorageProvider.choices, default=StorageProvider.LOCAL)
    storage_key = models.CharField(max_length=500, unique=True)
    original_name = models.CharField(max_length=255, blank=True, default="")
    original_filename = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="")
    mime_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    checksum_sha256 = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["supplier", "created_at"]),
            models.Index(fields=["source_type", "created_at"]),
            models.Index(fields=["checksum_sha256"]),
        ]

    def __str__(self) -> str:
        return f"{self.id}:{self.source_type}:{self.original_name or self.storage_key}"
