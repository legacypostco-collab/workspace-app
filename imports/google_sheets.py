from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from files.models import StoredFile
from files.storage import store_import_source_file
from marketplace.external_downloads import (
    ExternalDownloadError,
    download_get_with_checked_redirects,
)
from marketplace.upload_security import validate_uploaded_file

from .models import ImportPreviewSession
from .services import ColumnMappingResolver, ImportParser, PreviewResult


class GoogleSheetImportError(ValueError):
    pass


@dataclass(frozen=True)
class GoogleSheetPreview:
    session: ImportPreviewSession
    result: PreviewResult
    row_count: int


def _export_url(sheet_url: str) -> tuple[str, str]:
    parsed = urlparse((sheet_url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    match = re.fullmatch(
        r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/.*)?",
        parsed.path or "",
    )
    if parsed.scheme != "https" or host != "docs.google.com" or not match:
        raise GoogleSheetImportError("Нужна корректная ссылка Google Sheets.")

    sheet_id = match.group(1)
    query_gid = parse_qs(parsed.query).get("gid", [])
    fragment_gid = parse_qs(parsed.fragment).get("gid", [])
    gid = (query_gid or fragment_gid or ["0"])[0]
    if not str(gid).isdigit():
        raise GoogleSheetImportError("Некорректный идентификатор листа Google Sheets.")
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}",
        sheet_id,
    )


def create_google_sheet_preview(
    *,
    sheet_url: str,
    supplier,
    mapping_builder: Callable[[list[str]], dict[str, str]] | None = None,
    auto_confirm: bool = False,
) -> GoogleSheetPreview:
    export_url, sheet_id = _export_url(sheet_url)
    max_bytes = int(getattr(settings, "MAX_IMPORT_FILE_BYTES", 2 * 1024 * 1024))
    try:
        status_code, raw, _final_url = download_get_with_checked_redirects(
            export_url,
            allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
            allow_private_setting="GOOGLE_SHEETS_ALLOW_PRIVATE_IPS",
            allow_insecure_setting="GOOGLE_SHEETS_ALLOW_INSECURE_HTTP",
            max_bytes=max_bytes,
            timeout=20,
        )
    except (ExternalDownloadError, OSError, TimeoutError) as exc:
        raise GoogleSheetImportError(
            "Не удалось безопасно загрузить Google-таблицу. Проверьте ссылку и доступ."
        ) from exc
    if status_code in {401, 403}:
        raise GoogleSheetImportError(
            "Google-таблица закрыта для чтения. Откройте доступ по ссылке."
        )
    if status_code != 200:
        raise GoogleSheetImportError("Google-таблица недоступна для чтения.")

    uploaded = SimpleUploadedFile(
        name=f"google-sheet-{sheet_id}.csv",
        content=raw,
        content_type="text/csv",
    )
    try:
        validate_uploaded_file(uploaded, allowed_ext={".csv"}, max_bytes=max_bytes)
        preview_result = ImportParser().build_preview_from_bytes(raw)
    except ValueError as exc:
        raise GoogleSheetImportError(str(exc)) from exc

    mapping = (
        mapping_builder(preview_result.fieldnames)
        if mapping_builder is not None
        else preview_result.detected_columns
    )
    mapping = mapping or {}
    mapping_ok, _reason = ColumnMappingResolver().validate_mapping(
        mapping,
        preview_result.fieldnames,
    )
    initial_status = (
        ImportPreviewSession.Status.MAPPING_CONFIRMED
        if auto_confirm and mapping_ok
        else ImportPreviewSession.Status.DRAFT
    )

    uploaded.seek(0)
    stored = store_import_source_file(uploaded)
    try:
        with transaction.atomic():
            stored_file = StoredFile.objects.create(
                supplier=supplier,
                source_type=StoredFile.SourceType.IMPORT_GOOGLE_SHEET,
                storage_key=stored.storage_key,
                original_name=stored.original_name,
                content_type=stored.content_type,
                size_bytes=stored.size_bytes,
                checksum_sha256=stored.checksum_sha256,
            )
            session = ImportPreviewSession.objects.create(
                supplier=supplier,
                source_type=ImportPreviewSession.SourceType.GOOGLE_SHEET,
                source_url=sheet_url,
                source_file=stored_file,
                status=initial_status,
                detected_columns=preview_result.detected_columns,
                sample_rows=preview_result.sample_rows,
                column_mapping=mapping,
            )
    except Exception:
        default_storage.delete(stored.storage_key)
        raise

    return GoogleSheetPreview(
        session=session,
        result=preview_result,
        row_count=preview_result.row_count,
    )
