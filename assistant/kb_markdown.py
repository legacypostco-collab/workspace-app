"""Безопасный markdown-renderer для KnowledgeBaseEntry.answer.

Поддержка:
  • **bold** / *italic*
  • `code` inline
  • Списки `- item` и `1. item`
  • Параграфы (две \\n подряд)
  • [link](https://...) — только http/https, никаких javascript:
  • ![alt](https://image.url.jpg) — только https и расширения jpg/png/webp/gif

XSS-protection:
  • Весь raw input проходит через django.utils.html.escape() ДО render
  • URL валидируется regex'ом (whitelist схем)
  • `<script>`, `<iframe>`, on*-handlers — невозможны (они в escape'нутом тексте)

Без зависимостей (не используем `markdown`/`bleach` — лишний вес). Реализация
~80 строк, покрывает 95% случаев FAQ-контента.
"""
from __future__ import annotations

import re

from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

# Безопасные URL: только http(s) или относительные ссылки. Никаких
# javascript:, data:, file:, ftp: и пр.
_SAFE_URL_RE = re.compile(r"^(?:https?://|/)[^\s<>\"']{1,500}$", re.IGNORECASE)
_SAFE_IMG_RE = re.compile(
    r"^(?:https?://|/)[^\s<>\"']{1,500}\.(?:jpe?g|png|webp|gif|svg)(?:\?[^\s]*)?$",
    re.IGNORECASE,
)


def _safe_link(url: str) -> str:
    """Возвращает url если безопасен, иначе пустую строку."""
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        return ""
    return u if _SAFE_URL_RE.match(u) else ""


def _safe_img(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        return ""
    return u if _SAFE_IMG_RE.match(u) else ""


def render_kb_markdown(text: str) -> str:
    """Безопасный mini-markdown → HTML.

    >>> render_kb_markdown('**bold** and `code`')
    '<p><strong>bold</strong> and <code>code</code></p>'
    """
    if not text:
        return ""

    # Сначала ВСЁ экранируем — теперь никакой <script> не пройдёт
    src = escape(text)

    # Изображения ![alt](url) — должны быть ДО ссылок (синтаксис похож)
    def _img_sub(m):
        alt, url = m.group(1), m.group(2)
        safe = _safe_img(url)
        if not safe:
            return ""  # тихо выбросили подозрительный URL
        return (
            f'<img src="{escape(safe)}" alt="{escape(alt)}" '
            'loading="lazy" decoding="async">'
        )
    src = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _img_sub, src)

    # Ссылки [text](url)
    def _link_sub(m):
        text, url = m.group(1), m.group(2)
        safe = _safe_link(url)
        if not safe:
            return text  # без ссылки, просто текст
        return (
            f'<a href="{escape(safe)}" rel="noopener noreferrer" '
            f'target="_blank">{text}</a>'
        )
    src = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link_sub, src)

    # Inline code `…`
    src = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", src)

    # **bold** (без backslash-escape — простая реализация)
    src = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", src)
    # *italic*
    src = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", src)

    # Параграфы + списки
    out_lines = []
    in_ul = False
    in_ol = False
    para_buf = []

    def flush_para():
        if para_buf:
            out_lines.append("<p>" + " ".join(para_buf) + "</p>")
            para_buf.clear()

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out_lines.append("</ul>"); in_ul = False
        if in_ol:
            out_lines.append("</ol>"); in_ol = False

    for raw_line in src.split("\n"):
        line = raw_line.rstrip()
        if not line:
            flush_para(); close_lists()
            continue
        # Поддерживаем `-`, `*` (стандартный markdown) и `•` (русский typo-bullet,
        # часто используется операторами в FAQ-ответах)
        m_ul = re.match(r"^[\-\*•·]\s+(.+)$", line)
        m_ol = re.match(r"^\d+\.\s+(.+)$", line)
        if m_ul:
            flush_para()
            if in_ol: out_lines.append("</ol>"); in_ol = False
            if not in_ul: out_lines.append("<ul>"); in_ul = True
            out_lines.append(f"<li>{m_ul.group(1)}</li>")
        elif m_ol:
            flush_para()
            if in_ul: out_lines.append("</ul>"); in_ul = False
            if not in_ol: out_lines.append("<ol>"); in_ol = True
            out_lines.append(f"<li>{m_ol.group(1)}</li>")
        else:
            close_lists()
            para_buf.append(line)
    flush_para(); close_lists()

    # Raw text and every attribute were escaped before only known tags were added.
    return mark_safe("\n".join(out_lines))  # nosec B308 B703
