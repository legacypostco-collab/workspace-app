from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass
from uuid import uuid4

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.storage.filesystem import FileSystemStorage
from django.conf import settings
from django.utils import timezone


@dataclass
class StoredUpload:
    storage_key: str
    size_bytes: int
    checksum_sha256: str
    content_type: str
    original_name: str


def _local_fallback_enabled() -> bool:
    return bool(
        getattr(settings, "DEBUG", False)
        or getattr(settings, "TESTING", False)
    )


def _fallback_root() -> Path:
    return (Path(tempfile.gettempdir()) / "workspace-app-media").resolve()


def _fallback_path(storage_key: str) -> Path:
    fallback_root = _fallback_root()
    candidate = (fallback_root / str(storage_key or "")).resolve()
    if candidate != fallback_root and fallback_root not in candidate.parents:
        raise ValueError("Invalid fallback storage key.")
    return candidate


def _save_bytes_to_storage(storage_path: str, content: bytes) -> str:
    try:
        return default_storage.save(storage_path, ContentFile(content))
    except PermissionError:
        if not _local_fallback_enabled():
            raise
        fallback_root = _fallback_root()
        fallback_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fallback_root.chmod(0o700)
        except OSError:
            pass
        fs = FileSystemStorage(location=str(fallback_root))
        return fs.save(storage_path, ContentFile(content))


def store_import_source_file(uploaded_file, prefix: str = "imports/source") -> StoredUpload:
    original_name = os.path.basename(getattr(uploaded_file, "name", "") or "upload.csv")
    _, ext = os.path.splitext(original_name)
    ext = (ext or ".csv").lower()

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    size_bytes = 0
    max_bytes = int(getattr(settings, "MAX_IMPORT_FILE_BYTES", 50 * 1024 * 1024))
    for chunk in uploaded_file.chunks():
        hasher.update(chunk)
        chunks.append(chunk)
        size_bytes += len(chunk)
        if size_bytes > max_bytes:
            raise ValueError(f"Файл слишком большой. Максимум: {max_bytes} байт.")

    content = b"".join(chunks)
    checksum_sha256 = hasher.hexdigest()

    date_path = timezone.now().strftime("%Y/%m/%d")
    file_name = f"{uuid4().hex}{ext}"
    storage_path = f"{prefix}/{date_path}/{file_name}"
    storage_key = _save_bytes_to_storage(storage_path, content)

    return StoredUpload(
        storage_key=storage_key,
        size_bytes=size_bytes,
        checksum_sha256=checksum_sha256,
        content_type=getattr(uploaded_file, "content_type", "") or "text/csv",
        original_name=original_name,
    )


def store_generated_file_bytes(
    *,
    content: bytes,
    original_name: str,
    content_type: str,
    prefix: str = "imports/reports",
) -> StoredUpload:
    hasher = hashlib.sha256()
    hasher.update(content)
    checksum_sha256 = hasher.hexdigest()

    _, ext = os.path.splitext(original_name or "")
    ext = (ext or ".csv").lower()

    date_path = timezone.now().strftime("%Y/%m/%d")
    file_name = f"{uuid4().hex}{ext}"
    storage_path = f"{prefix}/{date_path}/{file_name}"
    storage_key = _save_bytes_to_storage(storage_path, content)

    return StoredUpload(
        storage_key=storage_key,
        size_bytes=len(content),
        checksum_sha256=checksum_sha256,
        content_type=content_type,
        original_name=original_name or file_name,
    )


def read_stored_file_bytes(storage_key: str) -> bytes:
    try:
        with default_storage.open(storage_key, "rb") as fh:
            return fh.read()
    except Exception:
        if not _local_fallback_enabled():
            raise
        fallback_path = _fallback_path(storage_key)
        with fallback_path.open("rb") as fh:
            return fh.read()
