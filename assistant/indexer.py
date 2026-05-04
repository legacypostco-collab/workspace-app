"""Index existing marketplace data (Part, Order, RFQ) into KnowledgeChunk."""
from __future__ import annotations

import logging

from .embeddings import get_embedding
from .models import KnowledgeChunk

logger = logging.getLogger(__name__)


# ── Catalog (Part) ─────────────────────────────────────────
def _part_to_text(part) -> str:
    parts = [f"{part.oem_number} {part.brand.name if part.brand else ''} {part.title}"]
    if part.category:
        parts.append(f"Категория: {part.category.name}")
    if hasattr(part, "get_condition_display"):
        parts.append(f"Состояние: {part.get_condition_display()}")
    if part.price:
        parts.append(f"Цена: {part.price} {getattr(part, 'currency', 'USD')}")
    if hasattr(part, "stock_qty"):
        parts.append(f"Наличие: {part.stock_qty} шт")
    if part.description:
        parts.append(f"Описание: {part.description[:500]}")
    return "\n".join(parts)


def index_part(part) -> KnowledgeChunk:
    content = _part_to_text(part)
    embedding = get_embedding(content)
    chunk, _ = KnowledgeChunk.objects.update_or_create(
        source_type=KnowledgeChunk.SourceType.PRODUCT,
        source_id=str(part.id),
        defaults={
            "title": f"{part.oem_number} — {part.title[:200]}",
            "content": content,
            "embedding": embedding,
            "metadata": {
                "oem": part.oem_number,
                "brand": part.brand.name if part.brand else None,
                "category": part.category.name if part.category else None,
                "price": str(part.price) if part.price else None,
                "in_stock": getattr(part, "stock_qty", 0) > 0,
            },
            "language": "ru",
            "access_roles": ["buyer", "seller", "operator_manager", "admin"],
            "is_active": getattr(part, "is_active", True),
        },
    )
    return chunk


def index_all_parts(batch_size: int = 100, limit: int = None) -> int:
    from marketplace.models import Part
    qs = Part.objects.select_related("brand", "category").filter(is_active=True)
    if limit:
        qs = qs[:limit]
    total = qs.count()
    indexed = 0
    for i in range(0, total, batch_size):
        for part in qs[i:i + batch_size]:
            try:
                index_part(part)
                indexed += 1
            except Exception as e:
                logger.error(f"index_part({part.id}) failed: {e}")
        logger.info(f"Indexed {indexed}/{total} parts")
    return indexed


# ── Orders ─────────────────────────────────────────────────
def _order_to_text(order) -> str:
    lines = [
        f"Заказ #{order.id}",
        f"Дата: {order.created_at.strftime('%d.%m.%Y')}",
        f"Статус: {order.get_status_display() if hasattr(order, 'get_status_display') else order.status}",
        f"Сумма: {order.total_amount}",
        f"Покупатель: {order.customer_name or order.buyer.get_full_name()}",
    ]
    if hasattr(order, "seller") and order.seller:
        lines.append(f"Поставщик: {order.seller.get_full_name() or order.seller.username}")
    if hasattr(order, "items"):
        items = order.items.all()[:20]
        if items:
            lines.append(f"Позиции: {len(items)}")
            for it in items:
                lines.append(f"  - {getattr(it, 'part_title', '?')} x{it.quantity}")
    return "\n".join(lines)


def index_order(order) -> KnowledgeChunk:
    content = _order_to_text(order)
    embedding = get_embedding(content)
    access_roles = ["operator_logist", "operator_manager", "operator_payment", "admin"]
    if order.buyer_id:
        access_roles.append("buyer")
    if hasattr(order, "seller_id") and order.seller_id:
        access_roles.append("seller")

    chunk, _ = KnowledgeChunk.objects.update_or_create(
        source_type=KnowledgeChunk.SourceType.ORDER,
        source_id=str(order.id),
        defaults={
            "title": f"Заказ #{order.id} — {order.get_status_display() if hasattr(order, 'get_status_display') else order.status}",
            "content": content,
            "embedding": embedding,
            "metadata": {
                "status": order.status,
                "total": str(order.total_amount),
                "buyer_id": order.buyer_id,
                "seller_id": getattr(order, "seller_id", None),
            },
            "language": "ru",
            "access_roles": access_roles,
            "is_active": True,
        },
    )
    return chunk


def index_all_orders(limit: int = None) -> int:
    from marketplace.models import Order
    qs = Order.objects.select_related("buyer", "seller").order_by("-created_at")
    if limit:
        qs = qs[:limit]
    indexed = 0
    for order in qs:
        try:
            index_order(order)
            indexed += 1
        except Exception as e:
            logger.error(f"index_order({order.id}) failed: {e}")
    logger.info(f"Indexed {indexed} orders")
    return indexed


# ── RFQ ────────────────────────────────────────────────────
def _rfq_to_text(rfq) -> str:
    lines = [
        f"RFQ #{rfq.id}",
        f"Дата: {rfq.created_at.strftime('%d.%m.%Y')}",
        f"Покупатель: {rfq.customer_name}",
        f"Статус: {rfq.get_status_display() if hasattr(rfq, 'get_status_display') else rfq.status}",
    ]
    if hasattr(rfq, "items"):
        items = rfq.items.all()[:20]
        if items:
            lines.append(f"Запрошено позиций: {len(items)}")
            for it in items:
                lines.append(f"  - {getattr(it, 'oem_query', '?')} x{getattr(it, 'quantity', 1)}")
    return "\n".join(lines)


def index_rfq(rfq) -> KnowledgeChunk:
    content = _rfq_to_text(rfq)
    embedding = get_embedding(content)
    chunk, _ = KnowledgeChunk.objects.update_or_create(
        source_type=KnowledgeChunk.SourceType.RFQ,
        source_id=str(rfq.id),
        defaults={
            "title": f"RFQ #{rfq.id} — {rfq.customer_name[:80]}",
            "content": content,
            "embedding": embedding,
            "metadata": {
                "status": rfq.status,
                "buyer_id": getattr(rfq, "buyer_id", None),
            },
            "language": "ru",
            "access_roles": ["buyer", "seller", "operator_manager", "admin"],
            "is_active": True,
        },
    )
    return chunk


def index_all_rfqs(limit: int = None) -> int:
    from marketplace.models import RFQ
    qs = RFQ.objects.order_by("-created_at")
    if limit:
        qs = qs[:limit]
    indexed = 0
    for rfq in qs:
        try:
            index_rfq(rfq)
            indexed += 1
        except Exception as e:
            logger.error(f"index_rfq({rfq.id}) failed: {e}")
    return indexed


# ── FAQ (built-in) ─────────────────────────────────────────
FAQ_ITEMS = [
    ("How to register?", "buyer",
     "Перейдите на /register/, выберите роль (Покупатель/Поставщик), укажите ИНН компании. После подтверждения email — доступ к кабинету. Полная активация после KYB верификации."),
    ("Что такое RFQ?", "buyer",
     "RFQ (Request For Quote) — запрос котировки. Покупатель указывает нужные запчасти, поставщики отвечают ценами и сроками."),
    ("Как устроена оплата?", "buyer",
     "Трёхступенчатая схема: 10% резерв (подтверждение) → 50% основной платёж (готовность к отгрузке) → 40% таможенный платёж. Все средства проходят через эскроу оператора."),
    ("Что такое KYB?", "all",
     "KYB (Know Your Business) — проверка легальности компании. Загрузите Устав, Выписку ЕГРЮЛ, паспорт директора в /kyb/. Проверка 1-2 рабочих дня."),
    ("How to scale my catalog?", "seller",
     "Используйте /seller/products/ → Загрузить файл (Excel/CSV). Превью покажет ошибки перед импортом. Можно загружать до 10 000 позиций за раз."),
]


def index_faq() -> int:
    indexed = 0
    for question, role, answer in FAQ_ITEMS:
        content = f"Вопрос: {question}\nОтвет: {answer}"
        try:
            KnowledgeChunk.objects.update_or_create(
                source_type=KnowledgeChunk.SourceType.FAQ,
                source_id=str(hash(question)),
                defaults={
                    "title": question[:200],
                    "content": content,
                    "embedding": get_embedding(content),
                    "metadata": {"category": "faq"},
                    "language": "ru",
                    "access_roles": ["buyer", "seller", "operator_logist",
                                     "operator_customs", "operator_payment",
                                     "operator_manager", "admin"] if role == "all" else [role],
                    "is_active": True,
                },
            )
            indexed += 1
        except Exception as e:
            logger.error(f"FAQ index failed for {question}: {e}")
    return indexed
