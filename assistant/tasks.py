"""Celery tasks for background indexing."""
from celery import shared_task


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def index_part_task(self, part_id):
    from marketplace.models import Part
    from .indexer import index_part
    try:
        part = Part.objects.select_related("brand", "category").get(id=part_id)
    except Part.DoesNotExist:
        return f"Part {part_id} not found"
    chunk = index_part(part)
    return str(chunk.id)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def index_order_task(self, order_id):
    from marketplace.models import Order
    from .indexer import index_order
    try:
        order = Order.objects.select_related("buyer", "seller").get(id=order_id)
    except Order.DoesNotExist:
        return f"Order {order_id} not found"
    chunk = index_order(order)
    return str(chunk.id)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def index_rfq_task(self, rfq_id):
    from marketplace.models import RFQ
    from .indexer import index_rfq
    try:
        rfq = RFQ.objects.get(id=rfq_id)
    except RFQ.DoesNotExist:
        return f"RFQ {rfq_id} not found"
    chunk = index_rfq(rfq)
    return str(chunk.id)


@shared_task
def reindex_all_task():
    """Nightly full reindex (called from beat schedule)."""
    from .indexer import index_all_orders, index_all_parts, index_all_rfqs, index_faq
    return {
        "faq": index_faq(),
        "parts": index_all_parts(),
        "orders": index_all_orders(limit=500),
        "rfqs": index_all_rfqs(limit=200),
    }
