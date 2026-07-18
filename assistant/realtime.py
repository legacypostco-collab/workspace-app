"""Small realtime helpers shared by assistant actions.

Database records remain the source of truth. These helpers only nudge open
browser tabs to refresh RFQ cards after a quote/RFQ mutation.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def rfq_realtime_recipients(rfq):
    """Users who may currently have this RFQ open: owner, invited users, operators."""
    ids = set()
    if getattr(rfq, "created_by_id", None):
        ids.add(rfq.created_by_id)

    try:
        from django.db.models import Q
        from marketplace.models import Notification

        ids.update(
            Notification.objects.filter(kind="rfq")
            .filter(Q(url__contains=f"rfq={rfq.id}") | Q(url__contains=f"/rfq/{rfq.id}/"))
            .values_list("user_id", flat=True)
        )
    except Exception:
        logger.exception("rfq realtime recipients from notifications failed")

    try:
        from .order_events import _operator_users

        ids.update(op.id for op in _operator_users())
    except Exception:
        logger.exception("rfq realtime recipients operators failed")

    return [uid for uid in ids if uid]


def push_rfq_update(rfq, *, event: str = "rfq_update", quote_id: int | None = None):
    """Best-effort live refresh event for RFQ cards, without unread counters."""
    if not rfq:
        return
    try:
        from .consumers import push_rfq_update_to_user

        for uid in rfq_realtime_recipients(rfq):
            push_rfq_update_to_user(uid, rfq_id=rfq.id, event=event, quote_id=quote_id)
    except Exception:
        logger.exception("push rfq update failed")
