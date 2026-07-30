"""Celery tasks for background indexing."""
from celery import shared_task
from django.utils.translation import gettext as _


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def index_part_task(self, part_id):
    from marketplace.models import Part

    from .indexer import index_part
    try:
        part = Part.objects.select_related("brand", "category").get(id=part_id)
    except Part.DoesNotExist:
        return f"Part {part_id} not found"
    from .embeddings import EmbeddingProviderUnavailable
    try:
        chunk = index_part(part)
    except EmbeddingProviderUnavailable:
        return "Skipped: embedding provider unavailable"
    return str(chunk.id)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def index_order_task(self, order_id):
    from marketplace.models import Order

    from .indexer import index_order
    try:
        # У Order нет поля seller (продавцы — через позиции/товары), только buyer.
        order = Order.objects.select_related("buyer").get(id=order_id)
    except Order.DoesNotExist:
        return f"Order {order_id} not found"
    from .embeddings import EmbeddingProviderUnavailable
    try:
        chunk = index_order(order)
    except EmbeddingProviderUnavailable:
        return "Skipped: embedding provider unavailable"
    return str(chunk.id)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def index_rfq_task(self, rfq_id):
    from marketplace.models import RFQ

    from .indexer import index_rfq
    try:
        rfq = RFQ.objects.get(id=rfq_id)
    except RFQ.DoesNotExist:
        return f"RFQ {rfq_id} not found"
    from .embeddings import EmbeddingProviderUnavailable
    try:
        chunk = index_rfq(rfq)
    except EmbeddingProviderUnavailable:
        return "Skipped: embedding provider unavailable"
    return str(chunk.id)


@shared_task
def reindex_all_task():
    """Nightly full reindex (called from beat schedule)."""
    from .embeddings import embedding_provider_available
    if not embedding_provider_available():
        return {"skipped": "embedding provider unavailable"}
    from .indexer import index_all_orders, index_all_parts, index_all_rfqs, index_faq
    return {
        "faq": index_faq(),
        "parts": index_all_parts(),
        "orders": index_all_orders(limit=500),
        "rfqs": index_all_rfqs(limit=200),
    }


# ──────────────────────────────────────────────────────────────────
# ТЗ §8 — Постоянный мониторинг KYB.
# Раз в неделю (Celery beat) перегоняем всех verified поставщиков
# через run_all_checks → если red signal → переводим в excluded и
# уведомляем оператора.
# ──────────────────────────────────────────────────────────────────

@shared_task
def kyb_weekly_monitor():
    """Перепроверка всех verified KYB-анкет.
    Запускается Celery beat раз в неделю (см. CELERY_BEAT_SCHEDULE).
    """
    from django.utils import timezone

    from marketplace.models import CompanyVerification

    from .kyb_api_checks import evaluate_risk, run_all_checks
    from .actions import _notify  # internal notification helper (правильный модуль)

    now = timezone.now()
    excluded = 0
    checked = 0
    errors = 0
    qs = CompanyVerification.objects.filter(status="verified")
    for kyb in qs.iterator(chunk_size=50):
        try:
            kyb.api_results = run_all_checks(kyb)
            decision, risk, reasons = evaluate_risk(kyb.api_results)
            kyb.risk_indicator = risk
            kyb.last_monitored_at = now
            if decision == "auto_reject":
                kyb.status = "rejected"  # ТЗ §8: «Исключён автоматически»
                kyb.rejection_reason = (
                    "ИСКЛЮЧЁН по мониторингу (раз в неделю):\n• "
                    + "\n• ".join(reasons[:5])
                )
                kyb.reviewed_at = now
                excluded += 1
                # Уведомить оператора
                try:
                    from django.contrib.auth import get_user_model
                    from marketplace.models import UserProfile
                    operator_ids = UserProfile.objects.filter(
                        role="operator",
                    ).values_list("user_id", flat=True)
                    for op in get_user_model().objects.filter(
                        id__in=operator_ids, is_active=True,
                    )[:5]:
                        _notify(op, kind="system",
                                title=_('⚠️ KYB исключён: %(p0)s') % {"p0": f'{kyb.legal_name}'},
                                body=_('Мониторинг выявил red signals: %(p0)s') % {"p0": f"{(reasons[0] if reasons else '?')}"},
                                url="/chat/")
                except Exception:
                    pass
            kyb.save()
            checked += 1
        except Exception:
            errors += 1
    return {"checked": checked, "excluded": excluded, "errors": errors}


# ──────────────────────────────────────────────────────────────────
# Audit log retention. Старые OrderEvent / Message могут раздуть БД.
# Беспредельное хранение не нужно — деньги уже завершены.
# ──────────────────────────────────────────────────────────────────

@shared_task
def prune_old_audit(days: int = 1095):  # 3 года
    """Удалить OrderEvent старше N дней (по умолчанию 3 года).
    Беги ежемесячно через Celery beat. Безопасно: события касаются только
    завершённых заказов; для completed-orders это история, которую можно
    архивировать в отдельный data warehouse если нужно.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .models import ActionExecution
    from marketplace.models import OrderEvent
    cutoff = timezone.now() - timedelta(days=days)
    # Только по завершённым / отменённым заказам — активные не трогаем
    qs = OrderEvent.objects.filter(
        created_at__lt=cutoff,
        order__status__in=("completed", "cancelled"),
    )
    deleted, _ = qs.delete()

    # Ключ нужен только для защиты короткого повторного запроса. Завершённые
    # записи старше 90 дней и оборванные операции старше суток не несут
    # прикладной истории и иначе бесконечно раздувают таблицу.
    operation_cutoff = timezone.now() - timedelta(days=90)
    stale_cutoff = timezone.now() - timedelta(days=1)
    completed_deleted, _ = ActionExecution.objects.filter(
        completed_at__isnull=False,
        completed_at__lt=operation_cutoff,
    ).delete()
    stale_deleted, _ = ActionExecution.objects.filter(
        completed_at__isnull=True,
        created_at__lt=stale_cutoff,
    ).delete()
    return {
        "deleted_events": deleted,
        "deleted_completed_operations": completed_deleted,
        "deleted_stale_operations": stale_deleted,
        "cutoff": cutoff.isoformat(),
    }


@shared_task(bind=True, max_retries=0)
def run_pricelist_import(self, import_id, mapping, transform_rules, constants,
                          save_profile, warehouse_id):
    """Фоновый импорт прайса в каталог (тяжёлая часть коммита).

    Исполняется в отдельном процессе Celery-воркера — не грузит daphne/AI на
    больших файлах (100K+ строк). Прогресс/результат пишутся в общий Redis-кэш
    caches["pricelist"], откуда их поллит daphne. retry отключён: импорт —
    upsert, но повтор на середине нежелателен; ошибки фиксируются в статусе.
    """
    from assistant.pricelist import _execute_import_job
    return _execute_import_job(
        import_id, mapping, transform_rules, constants, save_profile, warehouse_id,
    )
