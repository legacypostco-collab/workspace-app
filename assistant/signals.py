"""Signals for auto-indexing on model changes.

Async via Celery — uses .delay() so signal handler returns instantly.

В dev без Redis-брокера Celery .delay() может зависать на коннект-таймауте.
Поэтому при первом сбое мы запоминаем «брокера нет» и больше не пытаемся —
сигналы становятся no-op и не тормозят запросы.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

_BROKER_AVAILABLE = True


def _safe_delay(task, *args):
    """Call task.delay() but bail out fast if broker is unreachable."""
    global _BROKER_AVAILABLE
    if not _BROKER_AVAILABLE:
        return
    try:
        task.delay(*args)
    except Exception:
        # Disable for the rest of the process — dev без Redis не должен
        # ронять p99 каждого сохранения.
        _BROKER_AVAILABLE = False


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
