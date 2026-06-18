"""Data-fix: рекламации только на доставленных/завершённых заказах.

Исторически сиды вешали OrderClaim на заказы любого статуса (баг — комментарии
обещали «на доставленный заказ», код брал последний/случайный). Гейт open_claim
и сами сиды уже исправлены, но СУЩЕСТВУЮЩИЕ некогерентные записи на проде нужно
перепривязать. Эта миграция идемпотентна: на чистой БД находит 0 и выходит.

Перепривязываем каждую «раннюю» рекламацию на delivered/completed заказ
(приоритет — заказ того же покупателя), opened_by = покупатель заказа, без двух
рекламаций на один заказ. Если подходящего заказа нет — оставляем как есть.
"""
from django.db import migrations

FINAL = ("delivered", "completed")


def fix_claims(apps, schema_editor):
    Order = apps.get_model("marketplace", "Order")
    OrderClaim = apps.get_model("marketplace", "OrderClaim")

    bad = [c for c in OrderClaim.objects.select_related("order")
           if not c.order_id or (c.order and c.order.status not in FINAL)]
    if not bad:
        return
    used = set(OrderClaim.objects.exclude(id__in=[c.id for c in bad])
               .values_list("order_id", flat=True))
    for c in bad:
        buyer_id = c.opened_by_id or (c.order.buyer_id if c.order_id else None)
        cand = None
        if buyer_id:
            cand = (Order.objects.filter(buyer_id=buyer_id, status__in=FINAL)
                    .exclude(id__in=used).order_by("-id").first())
        if not cand:
            cand = (Order.objects.filter(status__in=FINAL)
                    .exclude(id__in=used).order_by("-id").first())
        if cand:
            c.order_id = cand.id
            c.opened_by_id = cand.buyer_id
            c.save(update_fields=["order", "opened_by"])
            used.add(cand.id)


def noop(apps, schema_editor):
    # Необратимо по смыслу (старая привязка была некорректной) — откат no-op.
    pass


class Migration(migrations.Migration):
    dependencies = [("marketplace", "0088_documentsignature")]
    operations = [migrations.RunPython(fix_claims, noop)]
