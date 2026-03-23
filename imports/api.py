from __future__ import annotations

import logging

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from files.models import StoredFile
from files.storage import read_stored_file_bytes, store_import_source_file
from marketplace.models import UserProfile
from offers.models import SupplierOffer

from .models import ImportJob, ImportPreviewSession, ImportRow
from .serializers import (
    ImportPreviewDetailSerializer,
    ImportPreviewMappingConfirmSerializer,
    ImportPreviewResponseSerializer,
    ImportStartSerializer,
    ImportJobDetailSerializer,
    ImportJobSummarySerializer,
    ImportJobResponseSerializer,
    ImportRowSerializer,
    UploadGoogleSheetSerializer,
    UploadImportFileSerializer,
)
from .services import ColumnMappingResolver, ImportParser
from .tasks import process_import_job

logger = logging.getLogger("imports")


def _is_seller(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = UserProfile.objects.filter(user=user).first()
    return bool(profile and profile.role == "seller")


class SupplierImportFileCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)

        serializer = UploadImportFileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded_file = serializer.validated_data["file"]

        stored = store_import_source_file(uploaded_file)
        stored_file = StoredFile.objects.create(
            supplier=request.user,
            source_type=StoredFile.SourceType.IMPORT_CSV,
            storage_key=stored.storage_key,
            original_name=stored.original_name,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256,
        )
        preview = ImportPreviewSession.objects.create(
            supplier=request.user,
            source_type=ImportPreviewSession.SourceType.CSV,
            source_file=stored_file,
            status=ImportPreviewSession.Status.DRAFT,
        )
        parser = ImportParser()
        preview_result = parser.build_preview(stored_file.storage_key)
        preview.detected_columns = preview_result.detected_columns
        preview.sample_rows = preview_result.sample_rows
        preview.column_mapping = preview_result.detected_columns
        preview.save(update_fields=["detected_columns", "sample_rows", "column_mapping", "updated_at"])
        response = ImportPreviewResponseSerializer(
            {
                "preview_id": preview.id,
                "status": preview.status,
                "detected_columns": preview.detected_columns,
                "sample_rows": preview.sample_rows,
            }
        )
        return Response(response.data, status=status.HTTP_201_CREATED)


class SupplierImportGoogleSheetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)

        serializer = UploadGoogleSheetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sheet_url = serializer.validated_data["url"]

        preview = ImportPreviewSession.objects.create(
            supplier=request.user,
            source_type=ImportPreviewSession.SourceType.GOOGLE_SHEET,
            source_url=sheet_url,
            status=ImportPreviewSession.Status.DRAFT,
            detected_columns={},
            sample_rows=[],
            column_mapping={},
        )
        response = ImportPreviewResponseSerializer(
            {
                "preview_id": preview.id,
                "status": preview.status,
                "detected_columns": preview.detected_columns,
                "sample_rows": preview.sample_rows,
            }
        )
        return Response(response.data, status=status.HTTP_201_CREATED)


class SupplierImportPreviewDetailAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, preview_id: int):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        preview = get_object_or_404(ImportPreviewSession, id=preview_id, supplier=request.user)
        data = ImportPreviewDetailSerializer(preview).data
        return Response(data, status=status.HTTP_200_OK)


class SupplierImportPreviewConfirmMappingAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, preview_id: int):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        preview = get_object_or_404(ImportPreviewSession, id=preview_id, supplier=request.user)
        serializer = ImportPreviewMappingConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mapping = serializer.validated_data["mapping"]

        fieldnames: list[str] = []
        if preview.source_type == ImportPreviewSession.SourceType.CSV and preview.source_file_id:
            parser = ImportParser()
            preview_result = parser.build_preview(preview.source_file.storage_key, rows_limit=1)
            fieldnames = preview_result.fieldnames
        resolver = ColumnMappingResolver()
        ok, reason = resolver.validate_mapping(mapping, fieldnames) if fieldnames else (True, "")
        if not ok:
            return Response({"error": reason}, status=status.HTTP_400_BAD_REQUEST)

        preview.column_mapping = mapping
        preview.status = ImportPreviewSession.Status.MAPPING_CONFIRMED
        preview.save(update_fields=["column_mapping", "status", "updated_at"])
        return Response({"preview_id": preview.id, "status": preview.status}, status=status.HTTP_200_OK)


class SupplierImportStartAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        serializer = ImportStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        preview = get_object_or_404(
            ImportPreviewSession,
            id=serializer.validated_data["preview_id"],
            supplier=request.user,
        )
        if preview.status != ImportPreviewSession.Status.MAPPING_CONFIRMED:
            return Response({"error": "confirm mapping before start"}, status=status.HTTP_400_BAD_REQUEST)

        idempotency_key = ""
        if preview.source_file_id:
            idempotency_key = preview.source_file.checksum_sha256
        job = ImportJob.objects.create(
            supplier=request.user,
            source_type=preview.source_type,
            source_file=preview.source_file,
            source_url=preview.source_url,
            preview_session=preview,
            column_mapping_json=preview.column_mapping or {},
            status=ImportJob.Status.QUEUED,
            idempotency_key=idempotency_key,
        )
        try:
            process_import_job.delay(job.id)
        except Exception as exc:
            logger.warning(
                "import_job_enqueue_failed",
                extra={"job_id": job.id, "supplier_id": request.user.id, "error": str(exc)},
            )
        response = ImportJobResponseSerializer({"job_id": job.id, "status": job.status})
        return Response(response.data, status=status.HTTP_201_CREATED)


class SupplierImportListAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        qs = ImportJob.objects.filter(supplier=request.user).order_by("-created_at")[:50]
        data = ImportJobSummarySerializer(qs, many=True).data
        return Response({"items": data}, status=status.HTTP_200_OK)


class SupplierImportDetailAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id: int):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        job = get_object_or_404(ImportJob.objects.select_related("error_report__file"), id=import_id, supplier=request.user)
        data = ImportJobDetailSerializer(job, context={"request": request}).data
        return Response(data, status=status.HTTP_200_OK)


class SupplierImportRowsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id: int):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        job = get_object_or_404(ImportJob, id=import_id, supplier=request.user)
        rows = ImportRow.objects.filter(job=job).order_by("row_no")
        row_status = (request.GET.get("row_status") or "").strip()
        if row_status:
            rows = rows.filter(status=row_status)
        validation_status = (request.GET.get("validation_status") or "").strip()
        if validation_status:
            rows = rows.filter(validation_status=validation_status)
        match_status = (request.GET.get("match_status") or "").strip()
        if match_status:
            rows = rows.filter(match_status=match_status)
        data = ImportRowSerializer(rows[:500], many=True).data
        return Response({"items": data}, status=status.HTTP_200_OK)


class SupplierImportProgressAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id: int):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        job = get_object_or_404(ImportJob.objects.select_related("error_report__file"), id=import_id, supplier=request.user)
        detail = ImportJobDetailSerializer(job, context={"request": request}).data
        total = int(job.total_rows or job.rows_total or 0)
        processed = int(job.processed_rows or job.valid_rows + job.error_rows)
        progress_percent = int((processed / total) * 100) if total > 0 else 0
        return Response(
            {
                **detail,
                "processed_rows": processed,
                "progress_percent": progress_percent,
                "is_finished": job.status in {ImportJob.Status.COMPLETED, ImportJob.Status.PARTIAL_SUCCESS, ImportJob.Status.FAILED},
            },
            status=status.HTTP_200_OK,
        )


class SupplierImportErrorsDownloadAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id: int):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        job = get_object_or_404(ImportJob.objects.select_related("error_report__file"), id=import_id, supplier=request.user)
        if not getattr(job, "error_report", None) or not job.error_report.file_id:
            return Response({"error": "error report not available"}, status=status.HTTP_404_NOT_FOUND)

        stored_file = job.error_report.file
        content = read_stored_file_bytes(stored_file.storage_key)
        response = HttpResponse(content, content_type=stored_file.content_type or "text/csv")
        filename = stored_file.original_name or f"import_errors_{job.id}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class SupplierImportRollbackAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, import_id: int):
        if not _is_seller(request.user):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        job = get_object_or_404(ImportJob, id=import_id, supplier=request.user)
        if job.status not in {ImportJob.Status.COMPLETED, ImportJob.Status.PARTIAL_SUCCESS}:
            return Response({"error": "only completed imports can be rolled back"}, status=status.HTTP_400_BAD_REQUEST)

        offer_ids = list(
            ImportRow.objects.filter(job=job, supplier_offer_id__isnull=False)
            .values_list("supplier_offer_id", flat=True)
            .distinct()
        )
        with transaction.atomic():
            updated = SupplierOffer.objects.filter(id__in=offer_ids, supplier=request.user).update(
                status=SupplierOffer.Status.INACTIVE,
                is_hidden=True,
            )
            summary = job.summary_json or {}
            summary["rollback"] = {"rolled_back_offer_count": updated}
            job.summary_json = summary
            job.save(update_fields=["summary_json", "updated_at"])

        return Response(
            {
                "ok": True,
                "job_id": job.id,
                "rolled_back_offer_count": updated,
            },
            status=status.HTTP_200_OK,
        )
