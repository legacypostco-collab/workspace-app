"""ТЗ §12.2: генераторы документов по заказу (PDF).

  Commercial Invoice  — для оплаты + customs
  Packing List         — для отгрузки и приёмки
  Quality Report (QC)  — финальный контроль перед отгрузкой

Все три используют reportlab (чистый Python). Сохраняются в MEDIA_ROOT
с записью в OrderDocument (ссылка на файл + тип). Возвращают
ActionResult с download URL.

Actions:
  generate_invoice_pdf       — buyer / seller / operator
  generate_packing_list_pdf  — seller / operator
  generate_qc_report_pdf     — seller / operator
  list_order_documents       — все
"""
from __future__ import annotations

import io
import logging
import os
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils.translation import gettext as _
from django.utils import timezone

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .actions import ActionResult, register

logger = logging.getLogger(__name__)


# ── Шрифты: Helvetica (дефолт reportlab) НЕ содержит кириллицу — имена
# покупателей, статусы и русские названия рендерились квадратами ■■■.
# Регистрируем кириллический DejaVu (бандл в assistant/fonts/, fallback на
# системный Linux-путь). Если не вышло — деградируем на Helvetica (латиница).
_FONTS_DIR = os.path.join(os.path.dirname(__file__), "fonts")
# Фирменный знак (PNG из brand-SVG) для шапки документов.
_BRAND_LOGO = os.path.join(os.path.dirname(__file__), "brand", "logo-mark-black.png")
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_OBLIQUE = "Helvetica-Oblique"
_fonts_ready = False


def _ensure_fonts():
    """Регистрирует DejaVu в reportlab один раз (idempotent, потокобезопасно
    достаточно для наших целей)."""
    global _fonts_ready, FONT_REGULAR, FONT_BOLD, FONT_OBLIQUE
    if _fonts_ready:
        return
    _fonts_ready = True
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        for name, fname in (
            ("DejaVuSans", "DejaVuSans.ttf"),
            ("DejaVuSans-Bold", "DejaVuSans-Bold.ttf"),
            ("DejaVuSans-Oblique", "DejaVuSans-Oblique.ttf"),
        ):
            path = os.path.join(_FONTS_DIR, fname)
            if not os.path.exists(path):
                path = f"/usr/share/fonts/truetype/dejavu/{fname}"
            pdfmetrics.registerFont(TTFont(name, path))
        pdfmetrics.registerFontFamily(
            "DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold",
            italic="DejaVuSans-Oblique", boldItalic="DejaVuSans-Bold")
        FONT_REGULAR, FONT_BOLD, FONT_OBLIQUE = (
            "DejaVuSans", "DejaVuSans-Bold", "DejaVuSans-Oblique")
    except Exception:
        logger.exception("DejaVu registration failed — fallback to Helvetica (no Cyrillic)")
        FONT_REGULAR, FONT_BOLD, FONT_OBLIQUE = (
            "Helvetica", "Helvetica-Bold", "Helvetica-Oblique")


# ── PDF helpers (reportlab) ──────────────────────────────────

def _pdf_canvas(title: str):
    """Создаёт reportlab Canvas + standard styles."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    _ensure_fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(title)
    return c, buf


def _draw_header(c, title: str, doc_no: str | int):
    """Шапка документа: логотип-плашка, реквизиты Consolidator, номер+дата."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    width, height = A4

    # Фирменный логотип (знак + лого-надпись). Fallback — плашка «C» + текст.
    logo_drawn = False
    try:
        if os.path.exists(_BRAND_LOGO):
            c.drawImage(_BRAND_LOGO, 20 * mm, height - 30.5 * mm,
                        width=33 * mm, height=8.7 * mm,
                        preserveAspectRatio=True, anchor="sw", mask="auto")
            logo_drawn = True
    except Exception:
        logger.exception("brand logo draw failed — fallback to text")
    if not logo_drawn:
        c.setFillColor(colors.HexColor("#0f172a"))
        c.rect(20 * mm, height - 30 * mm, 8 * mm, 8 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(FONT_BOLD, 11)
        c.drawCentredString(24 * mm, height - 25.5 * mm, "C")
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont(FONT_BOLD, 16)
        c.drawString(32 * mm, height - 24 * mm, "CONSOLIDATOR PARTS")

    # Реквизиты под логотипом
    c.setFont(FONT_REGULAR, 8)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(20 * mm, height - 35 * mm,
                  "B2B Heavy Equipment Spare Parts Marketplace")
    c.drawString(20 * mm, height - 38.5 * mm,
                  "marketplace@consolidator.parts · +7 (495) 123-45-67")

    # Номер документа + дата справа
    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 11)
    c.drawRightString(width - 20 * mm, height - 24 * mm, str(doc_no))
    c.setFont(FONT_REGULAR, 8)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawRightString(width - 20 * mm, height - 28.5 * mm,
                       timezone.now().strftime("Date: %Y-%m-%d %H:%M UTC"))

    # Заголовок документа на отдельной полосе
    c.setFillColor(colors.HexColor("#0f172a"))
    c.rect(20 * mm, height - 49 * mm, width - 40 * mm, 8 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(FONT_BOLD, 12)
    c.drawString(24 * mm, height - 46 * mm, title)

    c.setFillColor(colors.black)
    return height - 57 * mm  # y-cursor


def _draw_kv_block(c, y, rows: list[tuple[str, str]]):
    from reportlab.lib.units import mm
    c.setFont(FONT_REGULAR, 10)
    for label, value in rows:
        c.drawString(20 * mm, y, f"{label}:")
        c.drawString(70 * mm, y, str(value))
        y -= 6 * mm
    return y - 4 * mm


def _draw_table(c, y, header: list[str], rows: list[list], widths: list[int]):
    """Простой табличный layout с заголовком на тёмном фоне."""
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    x0 = 20 * mm
    row_h = 7 * mm

    # header
    c.setFillColor(colors.HexColor("#1f2937"))
    c.rect(x0, y - row_h + 1.5 * mm, sum(widths), row_h, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(FONT_BOLD, 9)
    x = x0
    for i, h in enumerate(header):
        c.drawString(x + 2 * mm, y - row_h + 4 * mm, h)
        x += widths[i]
    y -= row_h
    # rows
    c.setFillColor(colors.black)
    c.setFont(FONT_REGULAR, 9)
    for row in rows:
        x = x0
        for i, cell in enumerate(row):
            txt = str(cell)
            if len(txt) > 38:
                txt = txt[:36] + "…"
            c.drawString(x + 2 * mm, y - row_h + 4 * mm, txt)
            x += widths[i]
        y -= row_h
    return y - 2 * mm


def _draw_footer(c):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    width, _ = A4
    c.setFont(FONT_OBLIQUE, 7)
    c.setFillGray(0.4)
    c.drawCentredString(
        width / 2, 10 * mm,
        "Consolidator Parts · marketplace@consolidator.parts · "
        "Generated by Consolidator AI Assistant",
    )


# ── Save PDF buffer → OrderDocument ─────────────────────────

def _save_pdf(order, doc_type: str, title: str, buf: io.BytesIO,
              uploaded_by) -> OrderDocument:
    from marketplace.models import OrderDocument
    filename = f"ORD-{order.id}-{doc_type}-{timezone.now():%Y%m%d-%H%M%S}.pdf"
    buf.seek(0)
    doc = OrderDocument.objects.create(
        order=order, doc_type=doc_type, title=title,
        uploaded_by=uploaded_by,
    )
    doc.file_obj.save(filename, ContentFile(buf.read()), save=True)
    return doc


def _regenerate_signed_pdf(doc) -> bool:
    """Перегенерирует PDF документа С ВПЕЧАТАННЫМ блоком подписей и
    перезаписывает file_obj — чтобы открытый PDF уже содержал подписи."""
    builders = {
        "invoice": _build_invoice_pdf,
        "packing_list": _build_packing_list_pdf,
        "quality_report": _build_qc_report_pdf,
    }
    builder = builders.get(doc.doc_type)
    if not builder:
        return False
    try:
        sigs = list(doc.signatures.all())
        buf = builder(doc.order, signatures=sigs)
        buf.seek(0)
        fn = (doc.file_obj.name.rsplit("/", 1)[-1]
              if doc.file_obj and doc.file_obj.name
              else f"ORD-{doc.order_id}-{doc.doc_type}-signed.pdf")
        doc.file_obj.save(fn, ContentFile(buf.read()), save=True)
        return True
    except Exception:
        logger.exception("regenerate signed pdf failed for doc %s", doc.id)
        return False


def _doc_url(doc) -> str:
    """URL для просмотра/скачивания документа заказа.

    Ведём на авторизованную Django-вьюху (assistant-order-doc-file), а НЕ на
    сырой file_obj.url (/media/...): на проде /media/order_documents/ не
    раздаётся (SERVE_MEDIA=False) → счёт открывался с 404. Вьюха стримит файл
    сама и работает в обоих контурах (local + prod).
    """
    try:
        if doc.file_obj and doc.order_id:
            return f"/api/assistant/orders/{doc.order_id}/documents/{doc.id}/file/"
    except Exception:
        pass
    try:
        return doc.file_obj.url
    except Exception:
        return doc.file_url or ""


# ── Save proforma PDF (нет Order — пишем напрямую в media) ──

def _save_proforma_pdf(rfq, quote, buf: io.BytesIO) -> str:
    """Сохраняет pro-forma PDF на диск, возвращает MEDIA URL."""
    media_root = settings.MEDIA_ROOT
    rel_dir = os.path.join("proforma_invoices", timezone.now().strftime("%Y/%m"))
    abs_dir = os.path.join(str(media_root), rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    filename = f"PRO-RFQ{rfq.id}-Q{quote.id}-{timezone.now():%Y%m%d-%H%M%S}.pdf"
    abs_path = os.path.join(abs_dir, filename)
    buf.seek(0)
    with open(abs_path, "wb") as fh:
        fh.write(buf.read())
    return f"{settings.MEDIA_URL.rstrip('/')}/{rel_dir}/{filename}"


# ── Generators ──────────────────────────────────────────────

def _draw_parties_block(c, y, buyer_name, buyer_email, seller_name, seller_label):
    """Блок «Buyer / Seller» в две колонки."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    width, _ = A4
    col_w = (width - 40 * mm - 4 * mm) / 2

    # Buyer колонка
    c.setFillColor(colors.HexColor("#f1f5f9"))
    c.rect(20 * mm, y - 28 * mm, col_w, 28 * mm, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#475569"))
    c.setFont(FONT_BOLD, 8)
    c.drawString(22 * mm, y - 5 * mm, "BILL TO / BUYER")
    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 10)
    c.drawString(22 * mm, y - 11 * mm, (buyer_name or "—")[:48])
    c.setFont(FONT_REGULAR, 9)
    c.drawString(22 * mm, y - 17 * mm, (buyer_email or "—")[:48])

    # Seller колонка
    sx = 20 * mm + col_w + 4 * mm
    c.setFillColor(colors.HexColor("#f1f5f9"))
    c.rect(sx, y - 28 * mm, col_w, 28 * mm, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#475569"))
    c.setFont(FONT_BOLD, 8)
    c.drawString(sx + 2 * mm, y - 5 * mm, "SUPPLIER / SELLER")
    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 10)
    c.drawString(sx + 2 * mm, y - 11 * mm, seller_label[:48])
    c.setFont(FONT_REGULAR, 9)
    c.drawString(sx + 2 * mm, y - 17 * mm, "via Consolidator Parts")

    return y - 32 * mm


def _build_proforma_invoice_pdf(rfq, quote, logistics_cost: Decimal,
                                  buyer, anonymize_seller: bool = True) -> io.BytesIO:
    """Pro-forma Invoice по RFQ + Quote (Order ещё не создан).

    Используется в стадии КП до confirm_kp_and_reserve. После confirm
    отдельно генерируется Commercial Invoice уже на Order.
    """
    from reportlab.lib import colors
    from reportlab.lib.units import mm

    c, buf = _pdf_canvas(f"Pro-forma RFQ-{rfq.id}")
    doc_no = f"PRO-{rfq.id}/{quote.id}"
    y = _draw_header(c, "PRO-FORMA INVOICE", doc_no)

    seller_label = (
        f"Supplier #{quote.id} (rank by price)"
        if anonymize_seller and buyer.id != (quote.seller_id or 0)
        else (quote.seller.username if quote.seller else "—")
    )
    y = _draw_parties_block(
        c, y,
        buyer_name=buyer.get_full_name() or buyer.username,
        buyer_email=buyer.email or "—",
        seller_name=quote.seller.username if quote.seller else "—",
        seller_label=seller_label,
    )

    # Reference info
    y = _draw_kv_block(c, y, [
        ("Reference RFQ", f"RFQ-{rfq.id}"),
        ("Mode",          rfq.mode.upper()),
        ("Valid until",   (quote.valid_until or
                            (timezone.now())).strftime("%Y-%m-%d")
                          if hasattr(quote, "valid_until") and quote.valid_until
                          else "7 days"),
        ("Delivery time", f"{quote.delivery_days} days"),
        ("Payment terms", "10% reserve from deposit on confirmation, 90% before shipment"),
    ])

    rows = []
    parts_total = Decimal("0")
    for qi in quote.items.select_related("part").all():
        title = (qi.title_snapshot or (qi.part.title if qi.part else "—"))[:38]
        oem = (qi.part.oem_number if qi.part else "—")
        line_total = qi.unit_price * qi.quantity
        parts_total += line_total
        rows.append([
            title, oem, str(qi.quantity),
            f"${qi.unit_price:,.2f}",
            f"${line_total:,.2f}",
        ])
    y = _draw_table(
        c, y,
        header=["Position", "OEM", "Qty", "Unit, USD", "Total, USD"],
        rows=rows,
        widths=[55 * mm, 35 * mm, 15 * mm, 30 * mm, 30 * mm],
    )

    grand = parts_total + logistics_cost
    reserve = (grand * Decimal("0.10")).quantize(Decimal("0.01"))

    # Totals блок (выровнен справа)
    y -= 4 * mm
    from reportlab.lib.pagesizes import A4
    width, _ = A4
    box_x = width - 90 * mm
    c.setFillColor(colors.HexColor("#f1f5f9"))
    c.rect(box_x, y - 36 * mm, 70 * mm, 36 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont(FONT_REGULAR, 9)

    rows_t = [
        ("Goods total",           f"${parts_total:,.2f}"),
        ("Logistics & customs",   f"${logistics_cost:,.2f}"),
        ("INVOICE 100%",          f"${grand:,.2f}"),
        ("Reserve 10% on confirm", f"${reserve:,.2f}"),
        ("Outstanding (90%)",     f"${(grand - reserve):,.2f}"),
    ]
    yy = y - 5 * mm
    for label, value in rows_t:
        if "INVOICE" in label or "Reserve" in label:
            c.setFont(FONT_BOLD, 10)
        else:
            c.setFont(FONT_REGULAR, 9)
        c.drawString(box_x + 2 * mm, yy, label)
        c.drawRightString(box_x + 68 * mm, yy, value)
        yy -= 6 * mm
    y = yy - 4 * mm

    # Сноски
    c.setFont(FONT_OBLIQUE, 8)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(20 * mm, y - 4 * mm,
                  "This is a PRO-FORMA invoice — used for client review and "
                  "approval. Not a final tax document.")
    c.drawString(20 * mm, y - 9 * mm,
                  "Upon buyer confirmation, 10% of the total is reserved on the "
                  "Consolidator escrow.")
    c.drawString(20 * mm, y - 14 * mm,
                  "A Commercial Invoice (legal document) will be issued after "
                  "the order is created.")

    _draw_footer(c)
    c.showPage()
    if signatures:
        _draw_signatures(c, signatures, order)
    c.save()
    return buf


_SIG_ROLE_RU = {"buyer": _("Покупатель / Buyer"), "seller": _("Продавец / Seller"),
                "operator": _("Оператор / Operator"), "admin": _("Оператор / Operator")}


def _draw_signatures(c, signatures, order):
    """Страница «Подписи / Signatures» — впечатывает ПЭП-подписи в сам PDF
    (кто/роль/когда/IP + SHA-256 подписанного содержимого)."""
    from reportlab.lib.units import mm
    y = _draw_header(c, "ПОДПИСИ / SIGNATURES", f"ORD-{order.id}")
    c.setFont(FONT_REGULAR, 10)
    c.drawString(20 * mm, y, "Документ подписан простой электронной подписью (ПЭП), ст. 6 ФЗ-63.")
    y -= 11 * mm
    for s in signatures:
        role = _SIG_ROLE_RU.get(s.signer_role, s.signer_role or "—")
        mark = "✓" if s.method == "ep" else "📎"
        c.setFont(FONT_BOLD, 11)
        c.drawString(22 * mm, y, f"{mark} {role}: {s.signer_name or '—'}")
        y -= 6 * mm
        c.setFont(FONT_REGULAR, 9)
        when = s.signed_at.strftime("%d.%m.%Y %H:%M") if s.signed_at else "—"
        kind = "ПЭП в платформе" if s.method == "ep" else "загружен подписанный скан"
        c.drawString(26 * mm, y, f"{kind} · {when} · IP {s.ip or '—'}")
        y -= 5 * mm
        if s.doc_sha256:
            c.setFont(FONT_REGULAR, 8)
            c.drawString(26 * mm, y, f"SHA-256: {s.doc_sha256}")
            y -= 9 * mm
        else:
            y -= 4 * mm
    _draw_footer(c)
    c.showPage()


def _build_invoice_pdf(order, signatures=None) -> io.BytesIO:
    """Commercial Invoice — официальный документ на оплату по Order."""
    from reportlab.lib.units import mm
    c, buf = _pdf_canvas(f"Invoice ORD-{order.id}")
    y = _draw_header(c, "COMMERCIAL INVOICE", f"ORD-{order.id}")

    seller_label = "via Consolidator Parts (escrow)"
    y = _draw_parties_block(
        c, y,
        buyer_name=order.customer_name or (order.buyer.username if order.buyer else "—"),
        buyer_email=order.customer_email or "—",
        seller_name="—",
        seller_label=seller_label,
    )

    y = _draw_kv_block(c, y, [
        ("Buyer", order.customer_name or (order.buyer.username if order.buyer else "—")),
        ("Email", order.customer_email or "—"),
        ("Delivery", order.delivery_address or "—"),
        ("Payment status", order.get_payment_status_display()),
        ("Order created", order.created_at.strftime("%Y-%m-%d") if order.created_at else "—"),
    ])

    rows = []
    for it in order.items.select_related("part").all():
        title = (it.part.title if it.part else "—") + (
            f" · {it.part.oem_number}" if it.part and it.part.oem_number else ""
        )
        rows.append([
            title,
            f"{it.quantity}",
            f"${it.unit_price:,.2f}",
            f"${(it.unit_price * it.quantity):,.2f}",
        ])
    y = _draw_table(
        c, y,
        header=["Position", "Qty", "Unit, USD", "Total, USD"],
        rows=rows,
        widths=[100 * mm, 20 * mm, 25 * mm, 25 * mm],
    )

    parts_total = sum(
        (it.unit_price * it.quantity) for it in order.items.all()
    ) or Decimal("0")
    logi = order.logistics_cost or Decimal("0")
    grand = parts_total + logi

    y -= 4 * mm
    y = _draw_kv_block(c, y, [
        ("Goods total",  f"${parts_total:,.2f}"),
        ("Logistics",    f"${logi:,.2f}"),
        ("INVOICE 100%", f"${grand:,.2f}"),
        ("Reserve 10%",  f"${order.reserve_amount or 0:,.2f}"),
        ("Outstanding",  f"${(grand - (order.reserve_amount or Decimal('0'))):,.2f}"),
    ])

    _draw_footer(c)
    c.showPage()
    if signatures:
        _draw_signatures(c, signatures, order)
    c.save()
    return buf


def _build_packing_list_pdf(order, signatures=None) -> io.BytesIO:
    from reportlab.lib.units import mm
    c, buf = _pdf_canvas(f"Packing List ORD-{order.id}")
    y = _draw_header(c, "PACKING LIST", f"ORD-{order.id}")

    total_weight = Decimal("0")
    for it in order.items.select_related("part"):
        if it.part and it.part.gross_weight_kg:
            total_weight += Decimal(str(it.part.gross_weight_kg)) * it.quantity

    y = _draw_kv_block(c, y, [
        ("Buyer", order.customer_name or "—"),
        ("Delivery", order.delivery_address or "—"),
        ("Total positions", str(order.items.count())),
        ("Total gross weight", f"{total_weight:,.2f} kg"),
    ])

    rows = []
    for it in order.items.select_related("part"):
        p = it.part
        rows.append([
            (p.title if p else "—")[:38],
            (p.oem_number if p else "—"),
            f"{it.quantity}",
            f"{(p.gross_weight_kg if p else 0) or 0:.2f}",
            (p.country_of_origin if p else "—") or "—",
        ])
    y = _draw_table(
        c, y,
        header=["Title", "OEM", "Qty", "kg/pc", "Origin"],
        rows=rows,
        widths=[60 * mm, 35 * mm, 15 * mm, 20 * mm, 35 * mm],
    )

    y -= 6 * mm
    c.setFont(FONT_REGULAR, 10)
    c.drawString(20 * mm, y, "Inspector signature: ________________________")
    y -= 8 * mm
    c.drawString(20 * mm, y, "Date: ________________________")

    _draw_footer(c)
    c.showPage()
    if signatures:
        _draw_signatures(c, signatures, order)
    c.save()
    return buf


def _build_qc_report_pdf(order, signatures=None) -> io.BytesIO:
    from reportlab.lib.units import mm
    c, buf = _pdf_canvas(f"QC Report ORD-{order.id}")
    y = _draw_header(c, "QUALITY CONTROL REPORT", f"ORD-{order.id}")

    y = _draw_kv_block(c, y, [
        ("Order", f"ORD-{order.id}"),
        ("Buyer", order.customer_name or "—"),
        ("Status", order.get_status_display() if order.status else "—"),
        ("Inspection date", timezone.now().strftime("%Y-%m-%d")),
    ])

    rows = []
    for it in order.items.select_related("part"):
        rows.append([
            (it.part.title if it.part else "—")[:42],
            f"{it.quantity}",
            "PASS",
            "—",
        ])
    y = _draw_table(
        c, y,
        header=["Position", "Qty", "Result", "Notes"],
        rows=rows,
        widths=[90 * mm, 20 * mm, 25 * mm, 35 * mm],
    )

    y -= 8 * mm
    c.setFont(FONT_BOLD, 10)
    c.drawString(20 * mm, y, "Conclusion: All positions inspected and approved for shipment.")
    y -= 12 * mm
    c.setFont(FONT_REGULAR, 10)
    c.drawString(20 * mm, y, "QC Inspector: ________________________   Signature: ___________________")

    _draw_footer(c)
    c.showPage()
    if signatures:
        _draw_signatures(c, signatures, order)
    c.save()
    return buf


# ── Public actions ──────────────────────────────────────────

def _get_order(params, user, role):
    """Returns (order, error_result|None). Order должен принадлежать user
    или к нему имеет доступ его роль (seller с item'ами, operator)."""
    from marketplace.models import Order, OrderItem
    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return None, ActionResult(text=_("Заказ не найден."))
    # Доступ: buyer = автор; seller = у заказа есть его part'ы; operator = всегда
    if role.startswith("operator") or user.is_staff:
        return order, None
    if order.buyer_id == user.id:
        return order, None
    # seller: проверяем что у него есть part'ы в этом заказе
    if OrderItem.objects.filter(order=order, part__seller=user).exists():
        return order, None
    return None, ActionResult(text=_("Нет доступа к этому заказу."))


def _build_topup_pdf(topup, details) -> io.BytesIO:
    """PDF с инструкциями по оплате заявки на пополнение депозита (bank wire):
    сумма, payment reference, бенефициар, банковские реквизиты, назначение."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit

    c, buf = _pdf_canvas(f"Invoice {topup.reference_code}")
    _draw_header(c, "DEPOSIT TOP-UP — PAYMENT INSTRUCTIONS", f"INV-{topup.id:06d}")
    width, height = A4
    x = 20 * mm
    y = height - 58 * mm

    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 15)
    c.drawString(x, y, f"К оплате: {details['amount']}")
    c.setFont(FONT_REGULAR, 8)
    c.setFillColor(colors.HexColor("#64748b"))
    c.drawRightString(width - 20 * mm, y, "Срок оплаты: 7 дней")
    y -= 8 * mm

    # Payment reference — выделенная плашка
    c.setFillColor(colors.HexColor("#fff7ed"))
    c.rect(x, y - 9 * mm, width - 40 * mm, 12 * mm, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#9a3412"))
    c.setFont(FONT_BOLD, 7.5)
    c.drawString(x + 3 * mm, y - 2 * mm, "PAYMENT REFERENCE — обязательно в назначении платежа:")
    c.setFont(FONT_BOLD, 12)
    c.setFillColor(colors.black)
    c.drawString(x + 3 * mm, y - 7 * mm, details["reference_code"])
    y -= 17 * mm

    def section(title):
        nonlocal y
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont(FONT_BOLD, 9)
        c.drawString(x, y, title.upper())
        y -= 6 * mm

    def row(label, value):
        nonlocal y
        c.setFont(FONT_REGULAR, 8)
        c.setFillColor(colors.HexColor("#64748b"))
        c.drawString(x, y, label)
        c.setFillColor(colors.black)
        for ln in simpleSplit(str(value), FONT_REGULAR, 8, width - 20 * mm - 62 * mm):
            c.drawString(x + 42 * mm, y, ln)
            y -= 4.6 * mm
        y -= 1.2 * mm

    section("Получатель (Beneficiary)")
    row("Компания", details["beneficiary"])
    row("Адрес", details["beneficiary_address"])
    row("Trade License", details["trade_license"])
    row("Tax Reg No.", details["tax_no"])
    y -= 2 * mm
    section("Банковские реквизиты")
    row("Банк", details["bank_name"])
    row("SWIFT / BIC", details["swift"])
    row("IBAN", details["iban"])
    row("Account No.", details["account"])
    row("Branch Code", details["branch_code"])
    row("Валюта счёта", details["account_currency"])
    y -= 2 * mm
    section("Назначение платежа")
    row("Payment purpose", details["purpose"])
    y -= 2 * mm
    section("Контакт по платежу")
    row("Ответственный", details["contact_name"])
    row("Телефон", details["contact_phone"])
    row("Email", details["contact_email"])

    c.setFont(FONT_REGULAR, 7)
    c.setFillColor(colors.HexColor("#94a3b8"))
    c.drawString(x, 16 * mm, "Consolidator Parts · реквизиты выданы для конкретной заявки — не пересылайте третьим лицам.")
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


class TopupInvoicePdfView(APIView):
    """GET /api/assistant/topup/<ref>/invoice.pdf — инвойс заявки на пополнение
    депозита (bank wire), генерится на лету, без хранения. Только свой ref."""
    permission_classes = [IsAuthenticated]

    def get(self, request, ref):
        from django.http import Http404, HttpResponse

        from .actions import _bank_wire_details
        from .models import WalletTopupRequest
        topup = WalletTopupRequest.objects.filter(
            reference_code=ref, user=request.user).first()
        if not topup:
            raise Http404("topup not found")
        details = _bank_wire_details(topup.amount, topup.currency, topup.reference_code)
        try:
            buf = _build_topup_pdf(topup, details)
        except Exception:
            logger.exception("topup pdf build failed")
            return HttpResponse("PDF generation failed", status=500)
        resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="invoice-{ref}.pdf"'
        return resp


@register("generate_invoice_pdf")
def generate_invoice_pdf(params, user, role):
    """ТЗ §12.2: коммерческий инвойс PDF на 100% суммы заказа."""
    order, err = _get_order(params, user, role)
    if err:
        return err
    try:
        buf = _build_invoice_pdf(order)
        doc = _save_pdf(order, "invoice", f"Счёт на оплату ORD-{order.id}", buf, user)
    except Exception as e:
        logger.exception("invoice PDF generation failed")
        return ActionResult(text=_('⚠️ Не удалось создать счёт: %(p0)s') % {"p0": f'{e}'})
    url = _doc_url(doc)
    return ActionResult(
        text=(
            _('Счёт на оплату создан.\nЗаказ ORD-%(p0)s · $%(p1)s') % {"p0": f'{order.id}', "p1": f'{order.total_amount:,.2f}'}
        ),
        cards=[{"type": "doc", "data": {
            "id": str(doc.id),
            "title": doc.title,
            "kind": "invoice",
            "url": url,
            "size_kb": (doc.file_obj.size // 1024) if doc.file_obj else None,
        }}],
        actions=[
            {"action": "open_url", "label": _("📄 Открыть PDF"), "params": {"_url": url}},
            {"action": "sign_document", "label": _("✍️ Подписать и отправить"),
             "params": {"document_id": doc.id}},
            {"action": "list_order_documents", "label": _("Все документы заказа"),
             "params": {"order_id": order.id}},
        ],
    )


@register("generate_packing_list_pdf")
def generate_packing_list_pdf(params, user, role):
    """ТЗ §12.2: packing list — состав отгрузки + вес/объём."""
    order, err = _get_order(params, user, role)
    if err:
        return err
    try:
        buf = _build_packing_list_pdf(order)
        doc = _save_pdf(order, "packing_list", f"Упаковочный лист ORD-{order.id}", buf, user)
    except Exception as e:
        logger.exception("packing list PDF generation failed")
        return ActionResult(text=_('⚠️ Не удалось создать упаковочный лист: %(p0)s') % {"p0": f'{e}'})
    url = _doc_url(doc)
    return ActionResult(
        text=_('Упаковочный лист по заказу ORD-%(p0)s готов.') % {"p0": f'{order.id}'},
        cards=[{"type": "doc", "data": {
            "id": str(doc.id), "title": doc.title, "kind": "packing_list",
            "url": url,
        }}],
        actions=[
            {"action": "open_url", "label": _("📄 Открыть PDF"), "params": {"_url": url}},
            {"action": "sign_document", "label": _("✍️ Подписать и отправить"),
             "params": {"document_id": doc.id}},
        ],
    )


@register("generate_qc_report_pdf")
def generate_qc_report_pdf(params, user, role):
    """ТЗ §12.2: QC-отчёт перед отгрузкой."""
    order, err = _get_order(params, user, role)
    if err:
        return err
    try:
        buf = _build_qc_report_pdf(order)
        doc = _save_pdf(order, "quality_report", f"Акт контроля качества ORD-{order.id}", buf, user)
    except Exception as e:
        logger.exception("QC report PDF generation failed")
        return ActionResult(text=_('⚠️ Не удалось создать акт качества: %(p0)s') % {"p0": f'{e}'})
    url = _doc_url(doc)
    return ActionResult(
        text=_('Акт контроля качества по заказу ORD-%(p0)s готов.') % {"p0": f'{order.id}'},
        cards=[{"type": "doc", "data": {
            "id": str(doc.id), "title": doc.title, "kind": "qc_report",
            "url": url,
        }}],
        actions=[
            {"action": "open_url", "label": _("📄 Открыть PDF"), "params": {"_url": url}},
            {"action": "sign_document", "label": _("✍️ Подписать и отправить"),
             "params": {"document_id": doc.id}},
        ],
    )


@register("list_order_documents")
def list_order_documents(params, user, role):
    """Список всех документов заказа."""
    order, err = _get_order(params, user, role)
    if err:
        return err
    docs = list(order.documents.all().order_by("-created_at")
                .prefetch_related("signatures"))
    if not docs:
        return ActionResult(
            text=_('По заказу ORD-%(p0)s пока нет документов.') % {"p0": f'{order.id}'},
            actions=[
                {"action": "generate_invoice_pdf", "label": _("Создать счёт на оплату"),
                 "params": {"order_id": order.id}},
                {"action": "generate_packing_list_pdf", "label": _("Создать упаковочный лист"),
                 "params": {"order_id": order.id}},
                {"action": "generate_qc_report_pdf", "label": _("Создать акт качества"),
                 "params": {"order_id": order.id}},
            ],
        )
    _ROLE_RU = {"buyer": _("Покупатель"), "seller": _("Продавец"),
                "operator": _("Оператор"), "admin": _("Оператор")}
    is_op_view = bool(role and (role.startswith("operator") or role == "admin"))
    cards = []
    lines = []
    sign_actions = []
    hidden_drafts = 0
    for d in docs:
        sigs = list(d.signatures.all())
        creator_id = d.uploaded_by_id
        # Документ «отправлен» (виден контрагенту), когда его СОЗДАТЕЛЬ подписал.
        creator_signed = (creator_id is None) or any(
            s.signer_id == creator_id and s.method == "ep" for s in sigs)
        is_creator = (creator_id == user.id)
        # Черновик (создатель ещё не подписал) виден только автору и оператору.
        if not creator_signed and not is_creator and not is_op_view:
            hidden_drafts += 1
            continue
        if sigs:
            parts = [f"{_ROLE_RU.get(s.signer_role, s.signer_role or '—')} "
                     f"{'✓' if s.method == 'ep' else '📎'}" for s in sigs]
            status = _("подписи: ") + ", ".join(parts)
        elif is_creator and not creator_signed:
            status = _("черновик — подпишите, чтобы отправить")
        else:
            status = _("не подписан")
        lines.append(f"• {d.title} — {status}")
        cards.append({"type": "doc", "data": {
            "id": str(d.id),
            "title": d.title,
            "kind": d.doc_type,
            "url": _doc_url(d),
            "created_at": d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else "",
            "sign_status": status,
        }})
        if not any(s.signer_id == user.id and s.method == "ep" for s in sigs):
            sign_actions.append({"action": "sign_document",
                                  "label": _('✍️ Подписать и отправить: %(p0)s') % {"p0": f'{d.title[:16]}'},
                                  "params": {"document_id": d.id}})
    gen_actions = [
        {"action": "generate_invoice_pdf", "label": _("+ Счёт на оплату"),
         "params": {"order_id": order.id}},
        {"action": "generate_packing_list_pdf", "label": _("+ Упаковочный лист"),
         "params": {"order_id": order.id}},
        {"action": "generate_qc_report_pdf", "label": _("+ Акт качества"),
         "params": {"order_id": order.id}},
    ]
    if not cards:
        return ActionResult(
            text=(_('По заказу ORD-%(p0)s пока нет отправленных документов (контрагент ещё не подписал и не отправил).') % {"p0": f'{order.id}'}),
            actions=gen_actions,
        )
    foot = _("\n\nПодпись (ПЭП) фиксирует кто/когда/IP + хэш документа.")
    if hidden_drafts:
        foot += _("\nЕщё %(count)s в черновиках у контрагента (не отправлены).") % {"count": hidden_drafts}
    return ActionResult(
        text=(_("📄 Документы по заказу ORD-%(order_id)s (%(count)s):\n") % {"order_id": order.id, "count": len(cards)}
              + "\n".join(lines) + foot),
        cards=cards,
        actions=sign_actions[:6] + gen_actions,
    )


@register("sign_document")
def sign_document(params, user, role):
    """ПЭП: участник сделки подписывает документ заказа (Этап 1).

    Фиксируем подпись (кто/роль/когда/IP) + SHA-256 файла на момент подписи —
    tamper-evident. Доступ — только участникам заказа (через _get_order).
    """
    import hashlib

    from marketplace.models import DocumentSignature, OrderDocument
    doc_id = params.get("document_id") or params.get("doc_id")
    if not doc_id:
        return ActionResult(text=_("⚠️ Не указан документ."))
    doc = OrderDocument.objects.filter(id=doc_id).select_related("order").first()
    if not doc:
        return ActionResult(text=_("Документ не найден."))
    # Доступ — тот же гейт, что у списка документов (участник заказа).
    _order, err = _get_order({"order_id": doc.order_id}, user, role)
    if err:
        return ActionResult(text=_("Нет доступа к этому документу."))
    if DocumentSignature.objects.filter(document=doc, signer=user, method="ep").exists():
        return ActionResult(text=_('Вы уже подписали «%(p0)s».') % {"p0": f'{doc.title}'})
    # Хэш файла на момент подписи (tamper-evident).
    sha = ""
    try:
        if doc.file_obj and doc.file_obj.name:
            h = hashlib.sha256()
            with doc.file_obj.open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            sha = h.hexdigest()
    except Exception:
        logger.exception("sign_document hash failed for doc %s", doc.id)
    DocumentSignature.objects.create(
        document=doc, signer=user, signer_role=role or "",
        signer_name=(user.get_full_name() or user.username)[:200],
        method="ep", doc_sha256=sha, ip=(params.get("_client_ip") or "")[:64])
    # Впечатываем подпись в сам PDF (перегенерация с блоком «Подписи»).
    _regenerate_signed_pdf(doc)
    # Маршрутизация: уведомляем ОСТАЛЬНЫХ участников сделки (покупатель,
    # продавцы, оператор) — документ подписан, дальше их очередь смотреть/
    # подписывать. Оператор платформы — центральный контроль сделки.
    notified = []
    try:
        from marketplace.models import OrderItem

        from .actions import _notify
        order = doc.order
        recipients = {}  # id -> (user, role_label)
        if order.buyer_id and order.buyer:
            recipients[order.buyer_id] = (order.buyer, _("Покупатель"))
        if order.assigned_operator_id and order.assigned_operator:
            recipients[order.assigned_operator_id] = (order.assigned_operator, _("Оператор"))
        for it in OrderItem.objects.filter(order=order).select_related("part__seller"):
            if it.part and it.part.seller_id and it.part.seller:
                recipients.setdefault(it.part.seller_id, (it.part.seller, _("Продавец")))
        signer_ru = _SIG_ROLE_RU.get(role, role or "").split(" / ")[0] or _("участник")
        for rid, (rcp, label) in recipients.items():
            if rid == user.id:
                continue
            notified.append(label)
            _notify(rcp, kind="order",
                    title=_('📄 Подпись по ORD-%(p0)s') % {"p0": f'{order.id}'},
                    body=(_('«%(p0)s» подписан (%(p1)s). Откройте «Все документы» по заказу — посмотреть/подписать.') % {"p0": f'{doc.title}', "p1": f'{signer_ru}'}),
                    url="/chat/")
    except Exception:
        logger.exception("sign_document routing notify failed")
    sent_line = ((_(" Отправлено — уведомлены: ")
                  + ", ".join(dict.fromkeys(notified)) + ".") if notified else "")
    sigs = list(doc.signatures.all())
    status = ", ".join(
        f"{_SIG_ROLE_RU.get(s.signer_role, s.signer_role or '—').split(' / ')[0]} "
        f"{'✓' if s.method == 'ep' else '📎'}" for s in sigs)
    url = _doc_url(doc)
    return ActionResult(
        text=(_("✅ Вы подписали «%(title)s» (ПЭП). Подпись впечатана в PDF.") % {"title": doc.title}
              + sent_line + _("\nПодписи: %(status)s") % {"status": status}),
        cards=[{"type": "doc", "data": {
            "id": str(doc.id), "title": doc.title, "kind": doc.doc_type,
            "url": url, "sign_status": _("подписи: ") + status}}],
        actions=[
            {"action": "open_url", "label": _("📄 Открыть подписанный PDF"),
             "params": {"_url": url}},
            {"action": "list_order_documents", "label": _("Все документы"),
             "params": {"order_id": doc.order_id}},
        ],
    )
