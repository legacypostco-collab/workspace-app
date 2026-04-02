from __future__ import annotations

import hashlib
import os
from pathlib import Path
from dataclasses import dataclass
from uuid import uuid4

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.storage.filesystem import FileSystemStorage
from django.utils import timezone


@dataclass
class StoredUpload:
    storage_key: str
    size_bytes: int
    checksum_sha256: str
    content_type: str
    original_name: str


def _save_bytes_to_storage(storage_path: str, content: bytes) -> str:
    try:
        return default_storage.save(storage_path, ContentFile(content))
    except PermissionError:
        # Fallback for restricted local environments: keep logic compatible with S3/default storage in production.
        fallback_root = Path("/tmp/workspace-app-media")
        fallback_root.mkdir(parents=True, exist_ok=True)
        fs = FileSystemStorage(location=str(fallback_root))
        return fs.save(storage_path, ContentFile(content))


def store_import_source_file(uploaded_file, prefix: str = "imports/source") -> StoredUpload:
    original_name = os.path.basename(getattr(uploaded_file, "name", "") or "upload.csv")
    _, ext = os.path.splitext(original_name)
    ext = (ext or ".csv").lower()

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    size_bytes = 0
    for chunk in uploaded_file.chunks():
        hasher.update(chunk)
        chunks.append(chunk)
        size_bytes += len(chunk)

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
        fallback_root = Path("/tmp/workspace-app-media")
        fallback_path = fallback_root / storage_key
        with fallback_path.open("rb") as fh:
            return fh.read()
