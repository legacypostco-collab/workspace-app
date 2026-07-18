from __future__ import annotations

import os

from django.conf import settings
from django.utils.text import get_valid_filename


MAGIC = {
    ".pdf": lambda h: h.startswith(b"%PDF"),
    ".png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
    ".jpg": lambda h: h[:3] == b"\xff\xd8\xff",
    ".jpeg": lambda h: h[:3] == b"\xff\xd8\xff",
    ".webp": lambda h: h.startswith(b"RIFF") and h[8:12] == b"WEBP",
    ".xlsx": lambda h: h.startswith(b"PK\x03\x04"),
    ".docx": lambda h: h.startswith(b"PK\x03\x04"),
    ".xls": lambda h: h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    ".doc": lambda h: h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    ".heic": lambda h: b"ftyp" in h[:16],
}

TEXT_EXT = {".txt", ".csv"}
CAD_EXT = {".dwg", ".dxf", ".step", ".stp", ".iges", ".igs", ".stl"}
DANGEROUS_SIGNATURES = (
    b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe", b"<?php", b"<script", b"#!/",
)


class UploadSecurityError(ValueError):
    pass


def validate_uploaded_file(upload, *, allowed_ext: set[str], max_bytes: int | None = None) -> str:
    if upload is None:
        raise UploadSecurityError("Файл не приложен.")
    ext = os.path.splitext(getattr(upload, "name", "") or "")[1].lower()
    if ext not in allowed_ext:
        raise UploadSecurityError("Тип файла не разрешен.")
    size = int(getattr(upload, "size", 0) or 0)
    limit = int(max_bytes or getattr(settings, "MAX_UPLOAD_FILE_BYTES", 50 * 1024 * 1024))
    if size > limit:
        raise UploadSecurityError(f"Файл слишком большой. Максимум {limit} байт.")

    try:
        pos = upload.tell()
    except Exception:
        pos = None
    try:
        upload.seek(0)
        head = upload.read(64)
    finally:
        try:
            upload.seek(pos if pos is not None else 0)
        except Exception:
            pass

    low = (head or b"").lower()
    if any(low.startswith(sig.lower()) for sig in DANGEROUS_SIGNATURES):
        raise UploadSecurityError("Содержимое файла не разрешено.")
    checker = MAGIC.get(ext)
    if checker and not checker(head):
        raise UploadSecurityError("Содержимое файла не соответствует расширению.")
    if ext in TEXT_EXT:
        if b"\x00" in head:
            raise UploadSecurityError("Текстовый файл содержит бинарные данные.")
    # CAD-форматы имеют разные сигнатуры; запрещаем только явно опасные бинарники.

    try:
        from marketplace.file_scan import scan_uploaded_file

        ok, reason = scan_uploaded_file(upload)
        if not ok:
            raise UploadSecurityError(f"Файл не прошёл антивирусную проверку: {reason}")
    finally:
        try:
            upload.seek(pos if pos is not None else 0)
        except Exception:
            pass
    return ext


def safe_upload_name(upload, ext: str) -> str:
    base = os.path.splitext(getattr(upload, "name", "") or "")[0]
    safe = get_valid_filename(base)[:180] or "document"
    return f"{safe}{ext}"
