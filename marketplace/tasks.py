"""Celery tasks for marketplace.

Run with: celery -A consolidator_site worker -l info
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F, Q
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
    # Единый realtime fanout: чат слушает одну пользовательскую группу.
    try:
        from assistant.consumers import push_notification_to_user
        push_notification_to_user(user_id, {
            "id": notif.id,
            "kind": kind,
            "title": title,
            "body": body,
            "url": url,
            "created_at": notif.created_at.isoformat() if notif.created_at else "",
        })
    except Exception as e:
        logger.warning(f"WebSocket push failed: {e}")
    return notif.id


@shared_task
def send_pending_email_notifications():
    """Отправляет каждое непрочитанное уведомление по email не более одного раза."""
    from .models import Notification, UserProfile
    now = timezone.now()
    cutoff = now - timezone.timedelta(hours=24)
    stale_claim = now - timezone.timedelta(minutes=15)
    users_with_unread = User.objects.filter(
        notifications__is_read=False,
        notifications__created_at__gte=cutoff,
        notifications__email_sent_at__isnull=True,
        notifications__email_attempts__lt=3,
        profile__notif_email_enabled=True,
    ).distinct()
    sent = 0
    for user in users_with_unread:
        if not user.email:
            continue
        profile = UserProfile.objects.filter(user=user).first()
        allowed_kinds = {
            kind.strip() for kind in (profile.notif_kinds or "").split(",")
            if kind.strip()
        } if profile else set()
        if not allowed_kinds:
            continue
        with transaction.atomic():
            unread = list(
                Notification.objects.select_for_update()
                .filter(
                    user=user,
                    is_read=False,
                    created_at__gte=cutoff,
                    email_sent_at__isnull=True,
                    email_attempts__lt=3,
                    kind__in=allowed_kinds,
                )
                .filter(Q(email_claimed_at__isnull=True) | Q(email_claimed_at__lt=stale_claim))
                .order_by("created_at")[:10]
            )
            notification_ids = [notification.id for notification in unread]
            if notification_ids:
                Notification.objects.filter(id__in=notification_ids).update(
                    email_claimed_at=now,
                    email_attempts=F("email_attempts") + 1,
                )
        if not unread:
            continue
        subject = f"Consolidator Parts: {len(unread)} new notifications"
        body_lines = [f"You have {len(unread)} new notifications:\n"]
        for n in unread:
            body_lines.append(f"• [{n.get_kind_display()}] {n.title}")
            if n.body:
                body_lines.append(f"  {n.body[:120]}")
        body_lines.append(f"\nView all: {settings.SITE_URL if hasattr(settings, 'SITE_URL') else ''}/chat/#notifications")
        try:
            send_mail(
                subject=subject,
                message="\n".join(body_lines),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
            Notification.objects.filter(id__in=notification_ids).update(
                email_sent_at=timezone.now(),
                email_claimed_at=None,
                email_last_error="",
            )
            sent += 1
        except Exception as e:
            Notification.objects.filter(id__in=notification_ids).update(
                email_claimed_at=None,
                email_last_error=str(e)[:300],
            )
            logger.exception("Digest send failed for user %s", user.id)
    return f"Sent {sent} digests"


@shared_task
def check_sla_breaches():
    """Find orders nearing or past SLA deadline; create notifications."""
    from .models import Notification, Order
    from assistant.actions import _notify
    now = timezone.now()
    # Orders past their ship_deadline that haven't shipped yet
    overdue = Order.objects.filter(
        ship_deadline__lt=now,
        status__in=["pending", "reserve_paid", "confirmed", "in_production", "ready_to_ship"],
    ).prefetch_related("items__part__seller")
    breached = 0
    for order in overdue:
        seller_ids = {
            item.part.seller_id
            for item in order.items.all()
            if item.part_id and item.part.seller_id
        }
        if order.assigned_operator_id:
            seller_ids.add(order.assigned_operator_id)
        if order.buyer_id:
            seller_ids.add(order.buyer_id)
        for recipient_id in seller_ids:
            url = f"/chat/?order={order.id}"
            already = Notification.objects.filter(
                user_id=recipient_id,
                kind="sla",
                url=url,
                created_at__gte=now - timezone.timedelta(hours=24),
            ).exists()
            if already:
                continue
            recipient = User.objects.filter(id=recipient_id, is_active=True).first()
            if not recipient:
                continue
            _notify(
                recipient,
                kind="sla",
                title=f"SLA breached: order #{order.id}",
                body=f"Deadline {order.ship_deadline.strftime('%d.%m %H:%M')} passed.",
                url=url,
            )
            breached += 1
        if order.sla_status != "breached":
            Order.objects.filter(id=order.id).update(
                sla_status="breached",
                sla_breaches_count=F("sla_breaches_count") + 1,
            )
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
    from urllib.parse import urlsplit
    from assistant.security import safe_outbound_url, urlopen_no_redirect

    try:
        parsed_url = urlsplit(url)
        log_target = (
            f"{parsed_url.scheme}://{parsed_url.hostname or ''}"
            f"{':' + str(parsed_url.port) if parsed_url.port else ''}"
        )
    except (TypeError, ValueError):
        log_target = "<invalid-url>"

    ok_url, reason = safe_outbound_url(url, allow_query=False)
    if not ok_url:
        logger.warning("Webhook delivery blocked for %s: %s", log_target, reason)
        return 0

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        # The target passed safe_outbound_url with a production allowlist.
        with urlopen_no_redirect(req, timeout=10) as resp:
            return resp.getcode()
    except Exception:
        logger.warning(
            "Webhook delivery failed for %s",
            log_target,
            exc_info=True,
        )
        raise
