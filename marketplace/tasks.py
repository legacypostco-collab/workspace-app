"""Celery tasks for marketplace.

Run with: celery -A consolidator_site worker -l info
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_email_task(self, subject: str, body: str, to: list[str], from_email: str = None):
    """Send an email asynchronously. Auto-retries on transient failures."""
    return send_mail(
        subject=subject,
        message=body,
        from_email=from_email or settings.DEFAULT_FROM_EMAIL,
        recipient_list=to,
        fail_silently=False,
    )


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def send_notification_task(self, user_id: int, kind: str, title: str, body: str = "", url: str = ""):
    """Create a Notification + push via WebSocket if user is connected."""
    from .models import Notification
    notif = Notification.objects.create(
        user_id=user_id, kind=kind, title=title, body=body, url=url,
    )
    # Push via Channels (graceful no-op if Channels not configured)
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if layer:
            async_to_sync(layer.group_send)(
                f"user_{user_id}",
                {
                    "type": "notification.message",
                    "data": {
                        "id": notif.id, "kind": kind, "title": title,
                        "body": body[:120], "url": url, "is_read": False,
                        "created_at": notif.created_at.strftime("%d.%m %H:%M"),
                    },
                },
            )
    except Exception as e:
        logger.warning(f"WebSocket push failed: {e}")
    return notif.id


@shared_task
def send_pending_email_notifications():
    """Periodically batch-send unread notifications via email digest (last hour)."""
    from .models import Notification
    one_hour_ago = timezone.now() - timezone.timedelta(hours=1)
    # For each user with unread notifications, send digest if user opted in
    users_with_unread = User.objects.filter(
        notifications__is_read=False,
        notifications__created_at__gte=one_hour_ago,
    ).distinct()
    sent = 0
    for user in users_with_unread:
        if not user.email:
            continue
        unread = Notification.objects.filter(
            user=user, is_read=False, created_at__gte=one_hour_ago,
        )[:10]
        if not unread:
            continue
        subject = f"Consolidator Parts: {len(unread)} new notifications"
        body_lines = [f"You have {len(unread)} new notifications:\n"]
        for n in unread:
            body_lines.append(f"• [{n.get_kind_display()}] {n.title}")
            if n.body:
                body_lines.append(f"  {n.body[:120]}")
        body_lines.append(f"\nView all: {settings.SITE_URL if hasattr(settings, 'SITE_URL') else ''}/notifications/")
        try:
            send_email_task.delay(subject, "\n".join(body_lines), [user.email])
            sent += 1
        except Exception as e:
            logger.error(f"Digest send failed for user {user.id}: {e}")
    return f"Sent {sent} digests"


@shared_task
def check_sla_breaches():
    """Find orders nearing or past SLA deadline; create notifications."""
    from .models import Order, Notification
    now = timezone.now()
    # Orders past their ship_deadline that haven't shipped yet
    overdue = Order.objects.filter(
        ship_deadline__lt=now,
        status__in=["pending", "reserve_paid", "confirmed", "in_production", "ready_to_ship"],
    ).select_related("seller")
    breached = 0
    for order in overdue:
        # Avoid duplicate notifications: dedup per (user, order, day)
        already = Notification.objects.filter(
            user=order.seller,
            kind="sla",
            url=f"/seller/orders/{order.id}/",
            created_at__gte=now - timezone.timedelta(hours=24),
        ).exists()
        if already:
            continue
        send_notification_task.delay(
            user_id=order.seller_id,
            kind="sla",
            title=f"SLA breached: order #{order.id}",
            body=f"Deadline {order.ship_deadline.strftime('%d.%m %H:%M')} passed.",
            url=f"/seller/orders/{order.id}/",
        )
        breached += 1
    return f"Notified about {breached} SLA breaches"


@shared_task
def cleanup_expired_tokens():
    """Delete expired team invites, password reset tokens, etc."""
    from .models import TeamMember
    cutoff = timezone.now() - timezone.timedelta(days=14)
    deleted = TeamMember.objects.filter(
        status="invited", invited_at__lt=cutoff,
    ).delete()
    return f"Deleted {deleted[0]} expired invites"


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def deliver_webhook_task(self, url: str, payload: dict, headers: dict = None):
    """Send webhook with retry on failure. Replaces inline delivery in views."""
    import json
    import urllib.request
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.getcode()
    except Exception as e:
        logger.warning(f"Webhook delivery failed for {url}: {e}")
        raise
