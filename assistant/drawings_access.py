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
  build_watermarked_copy(file, filename, user, drawing) → защищённая копия
"""
from __future__ import annotations

from io import BytesIO
import logging
from pathlib import Path

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

    # Оператор с явно выданной ролью и superuser видят все чертежи.
    if user.is_superuser:
        return True, "admin"

    # Оператор (роль из профиля) видит чертежи всех
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


def _watermark_text(user, drawing) -> str:
    username = (getattr(user, "username", "") or f"user-{user.id}")[:60]
    return f"Consolidator Parts | {username} | drawing #{drawing.id}"


def _watermark_pdf(source, text: str) -> BytesIO:
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    reader = PdfReader(source)
    writer = PdfWriter()
    for page in reader.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        overlay_bytes = BytesIO()
        overlay = canvas.Canvas(overlay_bytes, pagesize=(width, height))
        overlay.saveState()
        try:
            overlay.setFillAlpha(0.16)
        except AttributeError:
            pass
        overlay.setFillColorRGB(0.82, 0.20, 0.08)
        overlay.setFont("Helvetica-Bold", max(12, min(width, height) / 32))
        for y_ratio in (0.28, 0.58, 0.88):
            overlay.saveState()
            overlay.translate(width / 2, height * y_ratio)
            overlay.rotate(28)
            overlay.drawCentredString(0, 0, text)
            overlay.restoreState()
        overlay.restoreState()
        overlay.save()
        overlay_bytes.seek(0)
        page.merge_page(PdfReader(overlay_bytes).pages[0])
        writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    output.seek(0)
    return output


def _watermark_image(source, text: str, suffix: str) -> tuple[BytesIO, str]:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(source).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    font_size = max(16, min(image.size) // 24)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = max(1, bbox[2] - bbox[0])
    text_height = max(1, bbox[3] - bbox[1])
    for y in range(text_height, image.height, max(text_height * 5, 120)):
        for x in range(-text_width // 2, image.width, max(text_width + 80, 280)):
            draw.text(
                (x, y),
                text,
                font=font,
                fill=(210, 52, 20, 82),
                stroke_width=1,
                stroke_fill=(255, 255, 255, 65),
            )
    result = Image.alpha_composite(image, overlay)
    output = BytesIO()
    if suffix in {".jpg", ".jpeg"}:
        result.convert("RGB").save(output, format="JPEG", quality=92)
        content_type = "image/jpeg"
    else:
        result.save(output, format="PNG")
        content_type = "image/png"
    output.seek(0)
    return output, content_type


def build_watermarked_copy(source, filename: str, user, drawing):
    """Return a real watermarked copy for PDF/PNG/JPEG, or ``None`` for CAD."""
    suffix = Path(filename or "").suffix.lower()
    text = _watermark_text(user, drawing)
    if suffix == ".pdf":
        return _watermark_pdf(source, text), "application/pdf"
    if suffix in {".png", ".jpg", ".jpeg"}:
        return _watermark_image(source, text, suffix)
    return None


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
