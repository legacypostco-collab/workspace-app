"""Unit tests for marketplace.upload_validation — size/ext/magic/MIME."""
import io
import zipfile

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from marketplace.upload_validation import (
    IMAGE_RULES, KYB_DOC_RULES, PDF_DOC_RULES, PRICELIST_RULES,
    validate_upload,
)
from marketplace.upload_security import UploadSecurityError, validate_uploaded_file


def _pdf_bytes(size=1000):
    return b"%PDF-1.4\n" + b"x" * (size - 9)


def _png_bytes(size=1000):
    return b"\x89PNG\r\n\x1a\n" + b"x" * (size - 8)


def _exe_bytes(size=1000):
    return b"MZ\x90\x00" + b"x" * (size - 4)  # PE/EXE magic


def _upload(name, content, mime=None):
    return SimpleUploadedFile(name, content, content_type=mime or "")


def _minimal_xlsx_bytes(workbook_body=b"<workbook/>"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("xl/workbook.xml", workbook_body)
    return buffer.getvalue()


# ── Size ─────────────────────────────────────────────────────────

def test_validate_rejects_oversized_pdf():
    big = _pdf_bytes(20 * 1024 * 1024)  # 20MB > 10MB default
    with pytest.raises(ValidationError) as exc:
        validate_upload(_upload("big.pdf", big), PDF_DOC_RULES)
    assert "слишком большой" in str(exc.value)


def test_validate_rejects_empty_file():
    with pytest.raises(ValidationError) as exc:
        validate_upload(_upload("empty.pdf", b""), PDF_DOC_RULES)
    assert "пустой" in str(exc.value)


def test_validate_accepts_size_in_limit():
    validate_upload(_upload("small.pdf", _pdf_bytes(1000)), PDF_DOC_RULES)


# ── Extension ────────────────────────────────────────────────────

def test_validate_rejects_wrong_extension():
    with pytest.raises(ValidationError) as exc:
        validate_upload(_upload("doc.exe", _pdf_bytes()), PDF_DOC_RULES)
    assert "расширение" in str(exc.value)


def test_validate_accepts_uppercase_extension():
    # Расширения case-insensitive
    validate_upload(_upload("Document.PDF", _pdf_bytes()), PDF_DOC_RULES)


# ── Magic bytes (content sniffing) ───────────────────────────────

def test_validate_rejects_exe_renamed_to_pdf():
    """Defense vs «вирус.pdf» — расширение PDF, содержимое EXE."""
    with pytest.raises(ValidationError) as exc:
        validate_upload(_upload("evil.pdf", _exe_bytes()), PDF_DOC_RULES)
    assert "не соответствует" in str(exc.value)


def test_validate_kyb_accepts_real_jpg():
    jpg = b"\xff\xd8\xff" + b"x" * 1000
    validate_upload(_upload("photo.jpg", jpg), KYB_DOC_RULES)


def test_validate_kyb_accepts_real_png():
    validate_upload(_upload("photo.png", _png_bytes()), KYB_DOC_RULES)


def test_validate_kyb_rejects_text_renamed_to_png():
    """Текстовый файл с .png расширением → magic mismatch."""
    with pytest.raises(ValidationError):
        validate_upload(_upload("fake.png", b"this is plain text content here"), KYB_DOC_RULES)


# ── None / blank ────────────────────────────────────────────────

def test_validate_accepts_none_file():
    """blank=True FileField может передать None — не должно крашиться."""
    validate_upload(None, KYB_DOC_RULES)


# ── Pricelist (no magic check) ──────────────────────────────────

def test_validate_pricelist_csv_ok():
    csv = b"OEM,price\nFIX-1,100\n" + b"row\n" * 100
    validate_upload(_upload("price.csv", csv, mime="text/csv"), PRICELIST_RULES)


def test_validate_pricelist_xlsx_ok():
    validate_upload(_upload("price.xlsx", _minimal_xlsx_bytes(),
                              mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                     PRICELIST_RULES)


def test_validate_pricelist_rejects_pdf():
    with pytest.raises(ValidationError):
        validate_upload(_upload("price.pdf", _pdf_bytes()), PRICELIST_RULES)


# ── Image rules ─────────────────────────────────────────────────

def test_validate_image_png_ok():
    validate_upload(_upload("img.png", _png_bytes()), IMAGE_RULES)


def test_validate_image_rejects_pdf():
    with pytest.raises(ValidationError):
        validate_upload(_upload("doc.pdf", _pdf_bytes()), IMAGE_RULES)


@override_settings(ENABLE_VIRUS_SCAN=True, VIRUS_SCAN_REQUIRED=True)
def test_required_virus_scanner_fails_closed_when_unavailable(monkeypatch):
    monkeypatch.setattr("marketplace.file_scan._get_client", lambda: None)

    with pytest.raises(ValidationError) as exc:
        validate_upload(_upload("doc.pdf", _pdf_bytes()), PDF_DOC_RULES)

    assert "проверку безопасности" in str(exc.value)


@override_settings(ENABLE_VIRUS_SCAN=True, VIRUS_SCAN_REQUIRED=False)
def test_unavailable_virus_scanner_is_allowed_only_when_not_required(monkeypatch):
    monkeypatch.setattr("marketplace.file_scan._get_client", lambda: None)

    validate_upload(_upload("doc.pdf", _pdf_bytes()), PDF_DOC_RULES)


@override_settings(VIRUS_SCAN_REQUIRED=False)
def test_ooxml_archive_with_dangerous_compression_ratio_is_rejected():
    compressed_bomb = _minimal_xlsx_bytes(b"0" * (2 * 1024 * 1024))

    with pytest.raises(UploadSecurityError, match="степень сжатия"):
        validate_uploaded_file(
            _upload("price.xlsx", compressed_bomb),
            allowed_ext={".xlsx"},
        )


@override_settings(VIRUS_SCAN_REQUIRED=False)
def test_corrupted_ooxml_archive_is_rejected():
    with pytest.raises(UploadSecurityError, match="повреждён"):
        validate_uploaded_file(
            _upload("price.xlsx", b"PK\x03\x04not-a-real-archive"),
            allowed_ext={".xlsx"},
        )
