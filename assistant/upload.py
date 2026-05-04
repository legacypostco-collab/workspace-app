"""File upload endpoint: спецификации (Excel/CSV/PDF) → список артикулов → search_parts.

Использует openpyxl (Excel), стандартный csv, и опциональный pypdf для PDF.
Если pypdf не установлен — PDF не парсится, возвращается понятная ошибка.
"""
from __future__ import annotations

import csv
import io
import os
import re
from typing import Iterable

from django.shortcuts import get_object_or_404
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Conversation
from .permissions import detect_user_role
from .rag import execute_action

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_ARTICLES = 200

_OEM_RE = re.compile(r"^[A-ZА-Я0-9][A-ZА-Я0-9\-./]{2,}$", re.IGNORECASE)


def _looks_like_article(token: str) -> bool:
    token = (token or "").strip().strip(".").strip()
    if not token or len(token) < 3 or len(token) > 40:
        return False
    if not _OEM_RE.match(token):
        return False
    if not any(ch.isdigit() for ch in token):
        return False
    return True


def _extract_from_text(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\s,;|]+", text or ""):
        token = raw.strip().strip(".").strip()
        if _looks_like_article(token) and token.upper() not in seen:
            seen.add(token.upper())
            out.append(token)
            if len(out) >= MAX_ARTICLES:
                break
    return out


def _extract_from_xlsx(blob: bytes) -> list[str]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return []
    try:
        wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(values_only=True):
            for cell in row:
                if cell is None:
                    continue
                token = str(cell).strip()
                if _looks_like_article(token) and token.upper() not in seen:
                    seen.add(token.upper())
                    out.append(token)
                    if len(out) >= MAX_ARTICLES:
                        return out
    return out


def _extract_from_csv(blob: bytes) -> list[str]:
    try:
        text = blob.decode("utf-8-sig", errors="replace")
    except Exception:
        text = blob.decode("latin-1", errors="replace")
    out: list[str] = []
    seen: set[str] = set()
    for delim in (",", ";", "\t"):
        try:
            reader = csv.reader(io.StringIO(text), delimiter=delim)
            sample = list(reader)
            if sample and any(len(r) > 1 for r in sample[:5]):
                for row in sample:
                    for cell in row:
                        token = (cell or "").strip()
                        if _looks_like_article(token) and token.upper() not in seen:
                            seen.add(token.upper())
                            out.append(token)
                            if len(out) >= MAX_ARTICLES:
                                return out
                if out:
                    return out
        except csv.Error:
            continue
    # Fallback: token-by-token
    return _extract_from_text(text)


def _extract_from_pdf(blob: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            return []
    try:
        reader = PdfReader(io.BytesIO(blob))
    except Exception:
        return []
    chunks: list[str] = []
    for page in reader.pages[:50]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return _extract_from_text("\n".join(chunks))


def _detect_kind(filename: str, content_type: str) -> str:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xls") or "spreadsheet" in ctype or "excel" in ctype:
        return "xlsx"
    if name.endswith(".csv") or "csv" in ctype:
        return "csv"
    if name.endswith(".pdf") or "pdf" in ctype:
        return "pdf"
    return "unknown"


class RecognizePhotoView(APIView):
    """POST /api/assistant/recognize-photo/   (multipart, field "photo")

    Принимает фото шильды/детали → пытается извлечь артикул, бренд, модель.
    Использует Anthropic Claude vision (claude-haiku-4-5 поддерживает images).
    Без API-ключа возвращает понятный fallback.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        photo = request.FILES.get("photo")
        if not photo:
            return Response({"error": "photo is required"}, status=400)
        if photo.size > 10 * 1024 * 1024:
            return Response({"error": "файл > 10 МБ"}, status=400)

        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return Response({
                "text": "",
                "error": ("Распознавание фото не настроено (нет ANTHROPIC_API_KEY). "
                          "Опишите деталь словами или загрузите Excel/CSV со списком."),
            }, status=200)

        try:
            import base64
            import anthropic
            blob = photo.read()
            b64 = base64.b64encode(blob).decode("ascii")
            media_type = photo.content_type or "image/jpeg"
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=os.getenv("ANTHROPIC_VISION_MODEL", "claude-haiku-4-5-20251001"),
                max_tokens=600,
                system=(
                    "Ты — AI-снабженец. Получаешь фото шильды или детали техники. "
                    "Извлеки: бренд (Komatsu/CAT/Volvo/...), модель техники, "
                    "артикул/part number, серийный номер, любые видимые "
                    "технические параметры. Отвечай в формате JSON: "
                    '{"brand":"...","model":"...","part_number":"...",'
                    '"serial":"...","notes":"кратко"}. '
                    "Если ничего не видно — верни {} с notes о причине."
                ),
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": media_type, "data": b64,
                        }},
                        {"type": "text", "text": "Распознай шильду / деталь."},
                    ],
                }],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            return Response({"text": text})
        except Exception as exc:
            return Response({"error": str(exc)}, status=200)


class TranscribeAudioView(APIView):
    """POST /api/assistant/transcribe-audio/   (multipart, field "audio")

    Принимает аудио-blob от MediaRecorder. Возвращает текст расшифровки.
    Если есть OPENAI_API_KEY — использует Whisper API. Иначе — fallback
    через Web Speech API на клиенте (этот endpoint просто скажет,
    что серверная расшифровка не настроена).
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        audio = request.FILES.get("audio")
        if not audio:
            return Response({"error": "audio is required"}, status=400)
        if audio.size > 20 * 1024 * 1024:
            return Response({"error": "файл > 20 МБ"}, status=400)

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return Response({
                "text": "",
                "error": "Серверная расшифровка не настроена (нет OPENAI_API_KEY). Используется встроенный Web Speech API в браузере.",
            }, status=200)

        # Реальный вызов Whisper
        try:
            import requests
            files = {"file": (audio.name or "audio.webm", audio.read(), audio.content_type or "audio/webm")}
            data = {"model": "whisper-1", "language": "ru"}
            r = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files, data=data, timeout=60,
            )
            if not r.ok:
                return Response({"error": f"Whisper API: {r.status_code} {r.text[:200]}"}, status=200)
            return Response({"text": r.json().get("text", "")})
        except Exception as exc:
            return Response({"error": str(exc)}, status=200)


class UploadSpecView(APIView):
    """POST /api/assistant/upload-spec/   (multipart, field "file")

    Парсит файл, извлекает артикулы, вызывает search_parts.
    Возвращает тот же формат, что и обычный chat-ответ (text, cards, actions).
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        upload = request.FILES.get("file")
        if not upload:
            return Response({"error": "file is required"}, status=400)
        if upload.size > MAX_FILE_BYTES:
            return Response(
                {"error": f"файл слишком большой (>{MAX_FILE_BYTES // (1024*1024)} МБ)"},
                status=400,
            )

        blob = upload.read()
        kind = _detect_kind(upload.name, upload.content_type or "")

        if kind == "xlsx":
            articles = _extract_from_xlsx(blob)
        elif kind == "csv":
            articles = _extract_from_csv(blob)
        elif kind == "pdf":
            articles = _extract_from_pdf(blob)
            if not articles and blob[:4] == b"%PDF":
                return Response({
                    "error": "Не удалось извлечь текст из PDF. Возможно, скан без OCR.",
                    "filename": upload.name,
                }, status=200)
        else:
            return Response(
                {"error": "Поддерживаются Excel (.xlsx/.xls), CSV и PDF"},
                status=400,
            )

        conv_id = request.data.get("conversation_id")
        if conv_id:
            conv = get_object_or_404(
                Conversation, id=conv_id, user=request.user, is_active=True
            )
        else:
            conv = Conversation.objects.create(
                user=request.user, role=detect_user_role(request.user)
            )

        if not articles:
            return Response({
                "conversation_id": str(conv.id),
                "filename": upload.name,
                "articles_found": 0,
                "text": (
                    f"В файле «{upload.name}» не нашлось артикулов в распознаваемом формате.\n"
                    "Проверьте, что артикулы в отдельной колонке и содержат цифры (например, AB-1234, 12345-XY)."
                ),
                "cards": [],
                "actions": [
                    {"label": "Загрузить другой файл", "action": "upload_spec", "params": {}},
                ],
                "suggestions": ["Создать RFQ вручную", "Поиск по бренду"],
            })

        # Прокидываем найденные артикулы в search_parts (multi-article path)
        try:
            result = execute_action(
                conv, "search_parts", {"articles": articles}, request.user
            )
        except Exception as exc:  # pragma: no cover
            return Response({"error": str(exc)}, status=500)

        # Префиксная подпись о загрузке
        prefix = (
            f"Из «{upload.name}» извлёк {len(articles)} артикулов. "
        )
        result_text = result.get("text") or ""
        result["text"] = prefix + result_text

        return Response({
            "conversation_id": str(conv.id),
            "filename": upload.name,
            "articles_found": len(articles),
            "articles": articles[:50],
            **result,
        })
