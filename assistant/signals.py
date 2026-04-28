"""Signals for auto-indexing on model changes.

Async via Celery — uses .delay() so signal handler returns instantly.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver


def _safe_delay(task, *args):
    """Call task.delay() but don't break if Celery broker unavailable."""
    try:
        task.delay(*args)
    except Exception:
        pass


def _connect():
    """Wire up signals lazily to avoid AppRegistry issues during startup."""
    try:
        from marketplace.models import Order, Part, RFQ
    except ImportError:
        return
    from .tasks import index_order_task, index_part_task, index_rfq_task

    @receiver(post_save, sender=Part, weak=False)
    def _on_part_save(sender, instance, **kwargs):
        _safe_delay(index_part_task, instance.id)

    @receiver(post_save, sender=Order, weak=False)
    def _on_order_save(sender, instance, **kwargs):
        _safe_delay(index_order_task, instance.id)

    @receiver(post_save, sender=RFQ, weak=False)
    def _on_rfq_save(sender, instance, **kwargs):
        _safe_delay(index_rfq_task, instance.id)


_connect()
