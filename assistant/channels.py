"""Durable notification channels: Email + Telegram.

WebSocket push работает только когда у пользователя открыта вкладка.
Эти каналы дублируют важные нотификации в email и Telegram, чтобы
пользователь не пропускал события когда оффлайн.

Архитектура:

  marketplace.Notification.create() → assistant._notify() → этот модуль

  fanout_to_durable(user, kind, title, body, url)
    ├→ EmailChannel.send (Django send_mail; SMTP via env)
    └→ TelegramChannel.send (HTTP POST → api.telegram.org/bot…/sendMessage)

  Per-user preferences хранятся в UserProfile:
    notif_email_enabled (bool)
    notif_telegram_chat_id (str, opt)
    notif_telegram_enabled (bool)
    notif_kinds (CSV: order,payment,rfq,sla,claim,system)

Daily digest: assistant.management.commands.send_email_digest
запускается через cron / Celery beat.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Per-user prefs helpers ───────────────────────────────────

def _get_profile(user):
    """Возвращает UserProfile или None — без падений для anon/system."""
    if not user or not user.is_authenticated:
        return None
    return getattr(user, "profile", None) or getattr(user, "userprofile", None)


def _kind_in_prefs(kind: str, csv: str) -> bool:
    if not csv:
        return False
    return kind in {p.strip() for p in csv.split(",") if p.strip()}


# ── Email channel ────────────────────────────────────────────

def _build_email_link(url: str) -> str:
    """Если url относительный — добавим SITE_URL префикс из settings."""
    if not url:
        return ""
    if url.startswith("http"):
        return url
    site = (
        os.getenv("SITE_URL")
        or getattr(settings, "SITE_URL", "")
        or "http://72.56.234.89"
    )
    return site.rstrip("/") + url


def send_email(user, *, kind: str, title: str, body: str = "", url: str = "") -> bool:
    """Отправить письмо через Django backend. Возвращает True если ушло."""
    if not user or not user.email:
        return False
    profile = _get_profile(user)
    if profile and not profile.notif_email_enabled:
        return False
    if profile and not _kind_in_prefs(kind, profile.notif_kinds):
        return False

    full_url = _build_email_link(url)
    subject = f"[Consolidator] {title}"
    text_body = f"{body}\n\n{full_url}" if full_url else body
    html_body = (
        f"<p>{body}</p>" + (f"<p><a href='{full_url}'>Открыть в Consolidator</a></p>" if full_url else "")
    )
    try:
        msg = EmailMultiAlternatives(
            subject=subject, body=text_body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@consolidator.local"),
            to=[user.email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("send_email failed for user_id=%s", user.id)
        return False


# ── Telegram channel ─────────────────────────────────────────

def send_telegram(user, *, kind: str, title: str, body: str = "", url: str = "") -> bool:
    """Отправить сообщение через Telegram bot API. Требует TELEGRAM_BOT_TOKEN env."""
    if not user:
        return False
    profile = _get_profile(user)
    if not profile:
        return False
    if not (profile.notif_telegram_enabled and profile.notif_telegram_chat_id):
        return False
    if not _kind_in_prefs(kind, profile.notif_kinds):
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        # demo mode — нет токена, тихо игнорим
        return False

    full_url = _build_email_link(url)
    text_lines = [f"<b>{title}</b>"]
    if body:
        text_lines.append(body)
    if full_url:
        text_lines.append(f"<a href='{full_url}'>Открыть</a>")
    text = "\n".join(text_lines)

    try:
        import httpx
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": profile.notif_telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("telegram %s for user_id=%s: %s",
                            resp.status_code, user.id, resp.text[:200])
            return False
        return True
    except Exception:
        logger.exception("send_telegram failed for user_id=%s", user.id)
        return False


# ── Fanout — единый вход для durable каналов ────────────────

def fanout_to_durable(user, *, kind: str, title: str, body: str = "", url: str = "") -> dict:
    """Дублировать нотификацию во все включённые durable каналы.

    Возвращает {email: bool, telegram: bool}.
    Не падает если каналы не настроены — best-effort.
    """
    result = {"email": False, "telegram": False}
    try:
        result["email"] = send_email(user, kind=kind, title=title, body=body, url=url)
    except Exception:
        logger.exception("durable email fanout failed")
    try:
        result["telegram"] = send_telegram(user, kind=kind, title=title, body=body, url=url)
    except Exception:
        logger.exception("durable telegram fanout failed")
    return result


# ── Email digest (daily) ─────────────────────────────────────

def send_digest(user) -> bool:
    """Дневной digest непрочитанных нотификаций.

    Запускается через `python manage.py send_email_digest --window-hours=24`
    либо Celery beat каждое утро.
    """
    if not user or not user.email:
        return False
    profile = _get_profile(user)
    if profile and not profile.notif_email_enabled:
        return False

    from marketplace.models import Notification

    cutoff = timezone.now() - timedelta(hours=int(os.getenv("DIGEST_WINDOW_HOURS", "24")))
    qs = Notification.objects.filter(
        user=user, is_read=False, created_at__gte=cutoff,
    ).order_by("-created_at")[:30]
    items = list(qs)
    if not items:
        return False

    n_total = len(items)
    by_kind = {}
    for it in items:
        by_kind.setdefault(it.kind, []).append(it)

    text_parts = [f"За последние 24 часа у вас {n_total} непрочитанных уведомлений:"]
    html_parts = [f"<p>За последние 24 часа у вас <b>{n_total}</b> непрочитанных уведомлений:</p><ul>"]
    for kind, ks in by_kind.items():
        text_parts.append(f"\n[{kind.upper()}]")
        html_parts.append(f"<li><b>{kind.upper()}</b><ul>")
        for it in ks[:10]:
            full_url = _build_email_link(it.url) if it.url else ""
            text_parts.append(f"  • {it.title}")
            if it.body:
                text_parts.append(f"    {it.body[:120]}")
            if full_url:
                text_parts.append(f"    {full_url}")
            html_link = f" — <a href='{full_url}'>открыть</a>" if full_url else ""
            html_parts.append(f"<li>{it.title}{html_link}<br><small>{it.body[:120]}</small></li>")
        html_parts.append("</ul></li>")
    html_parts.append("</ul>")

    try:
        msg = EmailMultiAlternatives(
            subject=f"[Consolidator] Сводка · {n_total} новых уведомлений",
            body="\n".join(text_parts),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@consolidator.local"),
            to=[user.email],
        )
        msg.attach_alternative("".join(html_parts), "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("send_digest failed for user_id=%s", user.id)
        return False
