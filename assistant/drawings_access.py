"""ТЗ §3, §12.1: контроль доступа к чертежам с водяными знаками и аудит-логом.

Правила доступа:
  • access_level='private'    — только владелец (drawing.seller)
  • access_level='for_sale'   — buyer ПОСЛЕ оплаты резерва (10%) по заказу
                                 с этим part'ом
  • access_level='rewardable' — by request, при использовании в сделке —
                                 reward_amount → автору

API:
  can_access(user, drawing, order=None) → (bool, reason)
  record_access(user, drawing, action, order=None, request=None)
  apply_watermark_url(file_url, user) → URL с watermark-параметром
"""
from __future__ import annotations

import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)


def can_access(user, drawing, order=None) -> tuple[bool, str]:
    """Можно ли пользователю получить доступ к чертежу.

    Returns: (allowed, reason)
    """
    if not drawing:
        return False, "drawing not found"
    if not user or not user.is_authenticated:
        return False, "не авторизован"

    # Владелец всегда видит свой чертёж
    if drawing.seller_id == user.id:
        return True, "owner"

    # Operator/admin видят всё
    if user.is_superuser or user.is_staff:
        return True, "staff"

    # Оператор (роль из профиля, не обязательно is_staff) видит чертежи всех
    # сторон — чтобы сверять «что нужно» vs «что предлагают» по артикулу.
    try:
        from .permissions import detect_user_role
        if (detect_user_role(user) or "").startswith("operator"):
            return True, "operator"
    except Exception:
        pass

    # private — никому кроме владельца
    if drawing.access_level == "private":
        return False, "приватный чертёж — доступ только у владельца"

    # for_sale — нужно payment_status='reserve_paid' или выше по заказу
    if drawing.access_level == "for_sale":
        if not order:
            # Найдём любой заказ buyer'а с этим part'ом
            try:
                from marketplace.models import OrderItem
                oi = (
                    OrderItem.objects.filter(part=drawing.part, order__buyer=user)
                    .select_related("order").order_by("-order__created_at").first()
                )
                if oi:
                    order = oi.order
            except Exception:
                pass
        if not order:
            return False, "нет заказа на эту деталь — оплатите резерв чтобы получить чертёж"
        if order.payment_status not in ("reserve_paid", "mid_paid", "customs_paid", "paid"):
            return False, f"чертёж откроется после оплаты резерва (текущий статус: {order.payment_status})"
        return True, f"оплачен резерв по заказу #{order.id}"

    # rewardable — открыто всем (запрос → потом reward автору)
    if drawing.access_level == "rewardable":
        return True, "rewardable — доступно всем (reward автору)"

    return False, "неизвестный access_level"


def record_access(user, drawing, action: str, *, order=None, request=None, note: str = ""):
    """Записать факт доступа в DrawingAccessLog."""
    try:
        from marketplace.models import DrawingAccessLog
        ip = ""
        ua = ""
        if request is not None:
            ip = request.META.get("REMOTE_ADDR", "")[:64]
            ua = request.META.get("HTTP_USER_AGENT", "")[:300]
        DrawingAccessLog.objects.create(
            drawing=drawing,
            user=user if (user and user.is_authenticated) else None,
            action=action,
            order=order,
            ip=ip, user_agent=ua, note=note[:200],
        )
    except Exception:
        logger.exception("record_access failed for drawing=%s user=%s", drawing, user)


def apply_watermark_url(file_url: str, user, drawing=None) -> str:
    """Возвращает URL с добавленным watermark-параметром.

    Реализация: для PDF и картинок добавляем `?wm=user_id_<id>` в URL —
    клиент-сторонняя обёртка превью либо backend-side rendering применяет
    overlay. Для STEP/DWG/STL — watermark не визуальный, помечается в
    DrawingAccessLog как метаданные.
    """
    if not file_url:
        return ""
    wm_token = f"wm-u{user.id}" if (user and user.is_authenticated) else "wm-anon"
    if drawing:
        wm_token += f"-d{drawing.id}"
    sep = "&" if "?" in file_url else "?"
    return f"{file_url}{sep}wm={quote(wm_token)}"


def grant_drawing_reward(drawing, *, order=None, multiplier=1):
    """ТЗ §3: при использовании rewardable-чертежа в сделке —
    выплатить автору reward_amount.

    Реализация: создаём WalletTx для автора (kind='topup', description='reward').
    multiplier обычно 1 (один заказ — одна выплата); для будущих кастомов.
    """
    if not drawing or drawing.reward_amount <= 0:
        return None
    try:
        from decimal import Decimal as _D

        from .models import Wallet, WalletTx
        amount = _D(str(drawing.reward_amount)) * _D(str(multiplier))
        author_wallet = Wallet.for_user(drawing.seller, demo_seed_amount=0)
        author_wallet.balance = author_wallet.balance + amount
        author_wallet.save(update_fields=["balance", "updated_at"])
        tx = WalletTx.objects.create(
            wallet=author_wallet, kind="topup", amount=amount,
            description=f"Reward за чертёж #{drawing.id} ({drawing.title[:60]})",
            order_id=order.id if order else None,
            balance_after=author_wallet.balance,
        )
        return tx
    except Exception:
        logger.exception("grant_drawing_reward failed for drawing=%s", drawing)
        return None
