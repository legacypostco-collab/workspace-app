from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from rest_framework import serializers

from .models import ImportJob, ImportPreviewSession, ImportRow


class ImportJobResponseSerializer(serializers.Serializer):
    job_id = serializers.IntegerField()
    status = serializers.CharField()


class UploadImportFileSerializer(serializers.Serializer):
    file = serializers.FileField(required=True)

    def validate_file(self, value):
        name = (getattr(value, "name", "") or "").lower()
        if not name.endswith(".csv"):
            raise serializers.ValidationError("Поддерживается только CSV файл.")
        max_bytes = int(settings.MAX_IMPORT_FILE_BYTES)
        if int(getattr(value, "size", 0) or 0) > max_bytes:
            raise serializers.ValidationError(f"Файл слишком большой. Максимум: {max_bytes} байт.")
        return value


class UploadGoogleSheetSerializer(serializers.Serializer):
    url = serializers.URLField(required=True)

    def validate_url(self, value: str) -> str:
        parsed = urlparse(value)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        if "docs.google.com" not in host or "/spreadsheets/" not in path:
            raise serializers.ValidationError("Нужна корректная ссылка Google Sheets.")
        return value


class ImportPreviewResponseSerializer(serializers.Serializer):
    preview_id = serializers.IntegerField()
    status = serializers.CharField()
    detected_columns = serializers.JSONField()
    sample_rows = serializers.JSONField()


class ImportPreviewMappingConfirmSerializer(serializers.Serializer):
    mapping = serializers.JSONField(required=True)

    def validate_mapping(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("mapping должен быть объектом.")
        return value


class ImportStartSerializer(serializers.Serializer):
    preview_id = serializers.IntegerField(required=True)


class ImportPreviewDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportPreviewSession
        fields = [
            "id",
            "status",
            "detected_columns",
            "sample_rows",
            "column_mapping",
            "created_at",
            "updated_at",
        ]


class ImportJobSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportJob
        fields = [
            "id",
            "status",
            "total_rows",
            "valid_rows",
            "error_rows",
            "created_products",
            "updated_offers",
            "started_at",
            "finished_at",
        ]


class ImportJobDetailSerializer(serializers.ModelSerializer):
    error_report_url = serializers.SerializerMethodField()

    class Meta:
        model = ImportJob
        fields = [
            "id",
            "status",
            "total_rows",
            "valid_rows",
            "error_rows",
            "created_products",
            "updated_offers",
            "started_at",
            "finished_at",
            "error_report_url",
        ]

    def get_error_report_url(self, obj: ImportJob) -> str | None:
        request = self.context.get("request")
        if not getattr(obj, "error_report", None) or not obj.error_report.file_id:
            return None
        if request is None:
            return f"/api/v1/supplier/imports/{obj.id}/errors"
        return request.build_absolute_uri(f"/api/v1/supplier/imports/{obj.id}/errors")


class ImportRowSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportRow
        fields = [
            "id",
            "row_no",
            "status",
            "validation_status",
            "match_status",
            "matched_by",
            "part_number_raw",
            "part_number_normalized",
            "cross_number_raw",
            "cross_number_normalized",
            "normalized_oem",
            "normalized_brand",
            "parsed_name",
            "parsed_price",
            "parsed_quantity",
            "error_code",
            "error_message",
            "error_hint",
            "matched_product_id",
            "matched_supplier_offer_id",
            "created_at",
            "updated_at",
        ]
