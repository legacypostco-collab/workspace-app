"""File upload validation helpers.

Что проверяем:
  1. Размер ≤ settings.MAX_*_BYTES
  2. Расширение в allow-list для категории (KYB / docs / prices / drawings)
  3. Magic bytes (content sniffing) — чтобы PDF был реально PDF, не exec
  4. (опц.) Virus scan через ClamAV (см. file_scan.py)

Использование в form:
    from marketplace.upload_validation import validate_upload, KYB_DOC_RULES
    def clean_doc_charter(self):
        f = self.cleaned_data.get('doc_charter')
        validate_upload(f, KYB_DOC_RULES)
        return f
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

# ── Файлы по типу: расширения, MIME-типы, max size ────────────────

PDF_DOC_RULES = {
    "kind": "PDF / документ",
    "extensions": (".pdf",),
    "mime_prefixes": ("application/pdf",),
    "magic_bytes": (b"%PDF-",),
    "max_bytes_env": "MAX_ORDER_DOCUMENT_BYTES",
    "max_bytes_default": 10 * 1024 * 1024,  # 10 MB
}

KYB_DOC_RULES = {
    "kind": "KYB документ (PDF / JPG / PNG)",
    "extensions": (".pdf", ".jpg", ".jpeg", ".png"),
    "mime_prefixes": ("application/pdf", "image/jpeg", "image/png"),
    "magic_bytes": (b"%PDF-", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"),
    "max_bytes_env": "MAX_KYB_DOC_BYTES",
    "max_bytes_default": 5 * 1024 * 1024,  # 5 MB
}

PRICELIST_RULES = {
    "kind": "прайс-лист (CSV / Excel)",
    "extensions": (".csv", ".xlsx", ".xls"),
    "mime_prefixes": ("text/csv", "text/plain",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       "application/vnd.ms-excel",
                       "application/octet-stream"),  # часто браузер так шлёт xlsx
    "magic_bytes": None,  # CSV не имеет magic; xlsx это zip — слишком общий
    "max_bytes_env": "MAX_IMPORT_FILE_BYTES",
    "max_bytes_default": 2 * 1024 * 1024,
}

DRAWING_RULES = {
    "kind": "чертёж (PDF / PNG / JPG / DWG)",
    "extensions": (".pdf", ".png", ".jpg", ".jpeg", ".dwg"),
    "mime_prefixes": ("application/pdf", "image/png", "image/jpeg",
                       "application/acad", "image/vnd.dwg", "application/octet-stream"),
    "magic_bytes": (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"AC1"),
    "max_bytes_env": "MAX_DRAWING_BYTES",
    "max_bytes_default": 20 * 1024 * 1024,  # 20 MB
}

IMAGE_RULES = {
    "kind": "изображение (PNG / JPG / WebP)",
    "extensions": (".png", ".jpg", ".jpeg", ".webp"),
    "mime_prefixes": ("image/png", "image/jpeg", "image/webp"),
    "magic_bytes": (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF"),
    "max_bytes_env": "MAX_IMAGE_BYTES",
    "max_bytes_default": 5 * 1024 * 1024,
}


def validate_upload(uploaded_file, rules: dict) -> None:
    """Raises django.core.exceptions.ValidationError при нарушении.

    `rules` — один из *_RULES выше или совместимый dict с ключами:
       kind, extensions, mime_prefixes, magic_bytes, max_bytes_env, max_bytes_default
    """
    if uploaded_file is None:
        return  # blank=True FileField — пропускаем

    kind = rules.get("kind", "файл")
    # 1. Size
    max_bytes = getattr(settings, rules["max_bytes_env"], rules["max_bytes_default"])
    size = getattr(uploaded_file, "size", 0) or 0
    if size > max_bytes:
        raise ValidationError(
            f"{kind}: файл слишком большой — {size:,} байт, лимит "
            f"{max_bytes:,} ({max_bytes // (1024*1024)} MB)."
        )
    if size == 0:
        raise ValidationError(f"{kind}: файл пустой.")

    # 2. Extension
    name = (getattr(uploaded_file, "name", "") or "").lower()
    allowed_exts = tuple(rules.get("extensions") or ())
    if allowed_exts and not name.endswith(allowed_exts):
        raise ValidationError(
            f"{kind}: недопустимое расширение. Разрешено: {', '.join(allowed_exts)}."
        )

    # 3. MIME (advisory — браузер может ошибиться)
    content_type = (getattr(uploaded_file, "content_type", "") or "").lower()
    mime_prefixes = tuple(rules.get("mime_prefixes") or ())
    if mime_prefixes and content_type and not any(
        content_type.startswith(p) for p in mime_prefixes
    ):
        # Не блокируем — некоторые браузеры шлют application/octet-stream для xlsx
        logger.info("upload mime mismatch: got=%s allowed=%s", content_type, mime_prefixes)

    # 4. Magic bytes — если задан список, реальное содержимое должно совпасть
    magic_bytes = rules.get("magic_bytes")
    if magic_bytes:
        try:
            uploaded_file.seek(0)
            head = uploaded_file.read(16)
            uploaded_file.seek(0)
            if not any(head.startswith(m) for m in magic_bytes):
                raise ValidationError(
                    f"{kind}: содержимое файла не соответствует заявленному типу. "
                    f"Возможно, файл повреждён или это не тот формат."
                )
        except ValidationError:
            raise
        except Exception:
            logger.exception("magic byte check failed (non-fatal)")

    # 5. (опц.) Virus scan
    if getattr(settings, "ENABLE_VIRUS_SCAN", False):
        try:
            from .file_scan import scan_or_raise
            scan_or_raise(uploaded_file)
        except ValidationError:
            raise
        except Exception:
            logger.exception("virus scan failed (non-fatal)")
