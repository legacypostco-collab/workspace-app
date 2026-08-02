from __future__ import annotations

from django.db.models import QuerySet

from .models import Order, OrderClaim, OrderDocument, OrderEvent, TeamMember


def seller_principal(user):
    """Return the company owner represented by a seller team member."""
    if not user or not getattr(user, "is_authenticated", False):
        return user
    membership = (
        TeamMember.objects.filter(user=user, status="active")
        .exclude(owner=user)
        .select_related("owner")
        .first()
    )
    return membership.owner if membership else user


def seller_company_user_ids(user) -> set[int]:
    principal = seller_principal(user)
    if not principal or not getattr(principal, "id", None):
        return set()
    member_ids = TeamMember.objects.filter(
        owner=principal,
        status="active",
        user__isnull=False,
    ).values_list("user_id", flat=True)
    return {principal.id, *(int(member_id) for member_id in member_ids)}


def seller_ids_for_order(order: Order) -> set[int]:
    prefetched = getattr(order, "_prefetched_objects_cache", {}).get("items")
    if prefetched is not None:
        return {
            int(item.part.seller_id)
            for item in prefetched
            if item.part and item.part.seller_id
        }
    return {
        int(seller_id)
        for seller_id in order.items.values_list(
            "part__seller_id",
            flat=True,
        ).distinct()
        if seller_id
    }


def seller_can_access_claim(user, claim: OrderClaim) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    principal = seller_principal(user)
    sellers = seller_ids_for_order(claim.order)
    if principal.id not in sellers:
        return False
    if len(sellers) == 1:
        return True
    return claim.opened_by_id in seller_company_user_ids(principal)


def seller_visible_claims(order: Order, user) -> list[OrderClaim]:
    claims = list(order.claims.all())
    return [claim for claim in claims if seller_can_access_claim(user, claim)]


def seller_can_access_document(user, document: OrderDocument) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    principal = seller_principal(user)
    if principal.id not in seller_ids_for_order(document.order):
        return False
    # Без отдельной маркировки аудитории продавцу доступны только документы
    # его команды. Покупательские и системные файлы могут содержать реквизиты.
    return document.uploaded_by_id in seller_company_user_ids(principal)


def seller_visible_documents(order: Order, user) -> QuerySet:
    principal = seller_principal(user)
    if not principal or principal.id not in seller_ids_for_order(order):
        return OrderDocument.objects.none()
    return order.documents.filter(
        uploaded_by_id__in=seller_company_user_ids(principal),
    )


def seller_visible_events(order: Order, user) -> list[OrderEvent]:
    principal = seller_principal(user)
    company_ids = seller_company_user_ids(principal)
    sellers = seller_ids_for_order(order)
    if not principal or principal.id not in sellers:
        return []
    return [
        event
        for event in order.events.all()
        if not (
            event.source == "seller"
            and len(sellers) > 1
            and event.actor_id not in company_ids
        )
    ]
