"""Virus scan helper for FileField uploads.

Active mode (production):
  Установлен `clamd` (ClamAV daemon) + python `clamd` пакет:
    pip install clamd
    docker run -d --name clam -p 3310:3310 clamav/clamav:latest
  env: CLAMD_HOST=127.0.0.1, CLAMD_PORT=3310

Inactive mode (dev / нет clamd):
  scan_file() возвращает (True, "scan unavailable") — пропускает файл.

Usage:
  from marketplace.file_scan import scan_uploaded_file
  ok, reason = scan_uploaded_file(uploaded_file)
  if not ok:
      raise ValidationError(f"Файл отклонён: {reason}")
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Cap для сканирования — иначе один большой файл блокирует daemon.
MAX_SCAN_BYTES = int(os.environ.get("CLAMD_MAX_SCAN_BYTES", str(50 * 1024 * 1024)))  # 50MB


def _get_client():
    """Lazy import + connection. None если clamd недоступен."""
    try:
        import clamd
    except ImportError:
        return None
    host = os.environ.get("CLAMD_HOST", "127.0.0.1")
    port = int(os.environ.get("CLAMD_PORT", "3310"))
    try:
        client = clamd.ClamdNetworkSocket(host=host, port=port, timeout=5)
        client.ping()
        return client
    except Exception as e:
        logger.warning("clamd not reachable at %s:%s — %s", host, port, e)
        return None


def scan_uploaded_file(uploaded_file) -> tuple[bool, str]:
    """Сканирует UploadedFile / FieldFile. Returns (clean: bool, message: str).

    Поведение:
      ok=True,  msg="scan unavailable" → clamd не настроен, пропускаем
      ok=True,  msg="clean"            → ClamAV: чисто
      ok=False, msg="<signature>"      → ClamAV: вирус найден, signature
      ok=False, msg="scan failed: ..." → не удалось прочитать / сетевая ошибка
    """
    if uploaded_file is None:
        return True, "no file"
    # Размер для безопасности
    size = getattr(uploaded_file, "size", None)
    if size and size > MAX_SCAN_BYTES:
        return False, f"file too large for scan ({size} > {MAX_SCAN_BYTES})"

    client = _get_client()
    if client is None:
        # Не настроен — fallback: пропускаем
        return True, "scan unavailable"

    try:
        # clamd InStream принимает file-like объект с .read()
        uploaded_file.seek(0)
        result = client.instream(uploaded_file)
        uploaded_file.seek(0)  # вернём указатель
        # Format: {'stream': ('OK',  None)}  или  {'stream': ('FOUND', 'Eicar-Test-Signature')}
        status, sig = result.get("stream", ("ERROR", "unknown"))
        if status == "OK":
            return True, "clean"
        if status == "FOUND":
            logger.warning("virus found in upload: %s", sig)
            return False, f"virus detected: {sig}"
        return False, f"scan error: {status}"
    except Exception as e:
        logger.exception("clamd scan failed")
        return False, f"scan failed: {e}"


def scan_or_raise(uploaded_file):
    """Удобный shortcut: raises ValidationError если файл нечист.
    Иначе возвращает True. Используется в form clean_*() методах."""
    from django.core.exceptions import ValidationError
    ok, msg = scan_uploaded_file(uploaded_file)
    if not ok:
        raise ValidationError(
            f"Загруженный файл не прошёл проверку безопасности: {msg}"
        )
    return True
