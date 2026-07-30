from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from assistant.permissions import detect_user_role
from files.models import StoredFile
from files.storage import read_stored_file_bytes, store_import_source_file
from marketplace.models import UserProfile
from offers.models import SupplierOffer

from .google_sheets import GoogleSheetImportError, create_google_sheet_preview
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
from .services import ColumnMappingResolver, ImportParser, StrictImportService
from .tasks import process_import_job

logger = logging.getLogger("imports")


def _is_seller(request) -> bool:
    user = request.user
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return detect_user_role(user, request=request) == "seller"


def _can_manage_imports(request) -> bool:
    if not _is_seller(request):
        return False
    if request.user.is_superuser:
        return True
    profile = UserProfile.objects.filter(user=request.user).first()
    return bool(profile and profile.can_manage_assortment)


class SupplierImportFileCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _can_manage_imports(request):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)

        serializer = UploadImportFileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded_file = serializer.validated_data["file"]

        try:
            uploaded_file.seek(0)
            raw = uploaded_file.read(int(settings.MAX_IMPORT_FILE_BYTES) + 1)
            uploaded_file.seek(0)
            preview_result = ImportParser().build_preview_from_bytes(raw)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except OSError:
            logger.exception(
                "import preview read failed supplier_id=%s",
                request.user.id,
            )
            return Response(
                {"error": "Не удалось безопасно прочитать файл."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        stored = store_import_source_file(uploaded_file)
        with transaction.atomic():
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
                detected_columns=preview_result.detected_columns,
                sample_rows=preview_result.sample_rows,
                column_mapping=preview_result.detected_columns,
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


class SupplierImportGoogleSheetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _can_manage_imports(request):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)

        serializer = UploadGoogleSheetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sheet_url = serializer.validated_data["url"]

        try:
            google_preview = create_google_sheet_preview(
                sheet_url=sheet_url,
                supplier=request.user,
            )
        except GoogleSheetImportError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        preview = google_preview.session
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
        if not _is_seller(request):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        preview = get_object_or_404(ImportPreviewSession, id=preview_id, supplier=request.user)
        data = ImportPreviewDetailSerializer(preview).data
        return Response(data, status=status.HTTP_200_OK)


class SupplierImportPreviewConfirmMappingAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, preview_id: int):
        if not _can_manage_imports(request):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        preview = get_object_or_404(ImportPreviewSession, id=preview_id, supplier=request.user)
        serializer = ImportPreviewMappingConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mapping = serializer.validated_data["mapping"]

        fieldnames: list[str] = []
        if preview.source_file_id:
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
        if not _can_manage_imports(request):
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
        if not preview.source_file_id:
            return Response({"error": "source file is unavailable"}, status=status.HTTP_400_BAD_REQUEST)

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
            job.status = ImportJob.Status.FAILED
            job.error_message = "Не удалось поставить импорт в очередь."
            job.save(update_fields=["status", "error_message", "updated_at"])
            return Response(
                {"error": "import queue is temporarily unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        response = ImportJobResponseSerializer({"job_id": job.id, "status": job.status})
        return Response(response.data, status=status.HTTP_201_CREATED)


class SupplierImportListAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_seller(request):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        qs = ImportJob.objects.filter(supplier=request.user).order_by("-created_at")[:50]
        data = ImportJobSummarySerializer(qs, many=True).data
        return Response({"items": data}, status=status.HTTP_200_OK)


class SupplierImportDetailAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id: int):
        if not _is_seller(request):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        job = get_object_or_404(ImportJob.objects.select_related("error_report__file"), id=import_id, supplier=request.user)
        data = ImportJobDetailSerializer(job, context={"request": request}).data
        return Response(data, status=status.HTTP_200_OK)


class SupplierImportRowsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, import_id: int):
        if not _is_seller(request):
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
        if not _is_seller(request):
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
        if not _is_seller(request):
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


class SupplierStrictImportAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _can_manage_imports(request):
            return Response({"error": "seller role required"}, status=status.HTTP_403_FORBIDDEN)
        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return Response({"error": "Файл не выбран."}, status=status.HTTP_400_BAD_REQUEST)

        ext = (uploaded_file.name or "").rsplit(".", 1)[-1].lower()
        if ext not in ("xlsx", "csv"):
            return Response(
                {"error": "Неподдерживаемый формат. Загрузите XLSX или CSV."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            from marketplace.upload_security import validate_uploaded_file

            validate_uploaded_file(
                uploaded_file,
                allowed_ext={".xlsx", ".csv"},
                max_bytes=int(settings.MAX_IMPORT_FILE_BYTES),
            )
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            uploaded_file.seek(0)
            raw = uploaded_file.read(int(settings.MAX_IMPORT_FILE_BYTES) + 1)
            uploaded_file.seek(0)
        except OSError:
            return Response(
                {"error": "Не удалось безопасно прочитать файл."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = StrictImportService()
        ok, error_msg, missing = service.validate_content(
            raw,
            uploaded_file.name,
        )
        if not ok:
            return Response(
                {"error": error_msg, "missing_columns": missing},
                status=status.HTTP_400_BAD_REQUEST,
            )

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

        result = service.process_file(
            storage_key=stored_file.storage_key,
            original_name=stored_file.original_name,
            supplier=request.user,
            stored_file=stored_file,
        )

        return Response(
            {
                "ok": True,
                "job_id": result.job_id,
                "total_rows": result.total_rows,
                "loaded_rows": result.loaded_rows,
                "error_rows": result.error_rows,
                "errors": [
                    {"row": e.row_number, "column": e.column, "message": e.message}
                    for e in result.errors[:100]
                ],
            },
            status=status.HTTP_201_CREATED if result.loaded_rows > 0 else status.HTTP_400_BAD_REQUEST,
        )


class SupplierImportRollbackAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, import_id: int):
        if not _can_manage_imports(request):
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
