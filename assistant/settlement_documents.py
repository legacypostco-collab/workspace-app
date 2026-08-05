from __future__ import annotations

import io
import logging
from decimal import Decimal

from django.core.files.base import ContentFile

from marketplace.models import OrderDocument

logger = logging.getLogger(__name__)


def _styles():
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    from .documents import FONT_BOLD, FONT_REGULAR, _ensure_fonts

    _ensure_fonts()
    sheet = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "SettlementTitle",
            parent=sheet["Title"],
            fontName=FONT_BOLD,
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#15121a"),
            alignment=TA_CENTER,
            spaceAfter=12,
        ),
        "h2": ParagraphStyle(
            "SettlementH2",
            parent=sheet["Heading2"],
            fontName=FONT_BOLD,
            fontSize=11,
            leading=14,
            textColor=colors.HexColor("#d8482f"),
            spaceBefore=8,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "SettlementBody",
            parent=sheet["BodyText"],
            fontName=FONT_REGULAR,
            fontSize=9,
            leading=13,
            textColor=colors.HexColor("#27232d"),
            spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "SettlementSmall",
            parent=sheet["BodyText"],
            fontName=FONT_REGULAR,
            fontSize=7.5,
            leading=10,
            textColor=colors.HexColor("#625d69"),
        ),
        "right": ParagraphStyle(
            "SettlementRight",
            parent=sheet["BodyText"],
            fontName=FONT_BOLD,
            fontSize=10,
            alignment=TA_RIGHT,
        ),
    }


def _safe(value, fallback="—") -> str:
    text = str(value or "").strip()
    return text or fallback


def _money(value, currency: str) -> str:
    return f"{Decimal(value):,.2f} {currency}"


def _party_lines(party: dict) -> list[str]:
    lines = [_safe(party.get("legal_name") or party.get("name"))]
    identifiers = []
    if party.get("tax_id"):
        identifiers.append(f"ИНН / Tax ID: {party['tax_id']}")
    if party.get("registration_no"):
        identifiers.append(f"Регистрационный номер: {party['registration_no']}")
    if identifiers:
        lines.append(" · ".join(identifiers))
    if party.get("address"):
        lines.append(f"Адрес: {party['address']}")
    if party.get("bank_name"):
        lines.append(f"Банк: {party['bank_name']}")
    if party.get("bank_account"):
        lines.append(f"Счёт / IBAN: {party['bank_account']}")
    if party.get("bank_swift"):
        lines.append(f"SWIFT / БИК: {party['bank_swift']}")
    if party.get("contact"):
        lines.append(f"Контакт: {party['contact']}")
    return lines


def _document(on_page, *, title: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title=title,
    )
    document._settlement_buffer = buffer
    document._settlement_page = on_page
    return document


def _page(canvas, document):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm

    from .documents import FONT_BOLD, FONT_REGULAR, _ensure_fonts

    _ensure_fonts()
    width, height = A4
    canvas.saveState()
    canvas.setFillColor(colors.HexColor("#15121a"))
    canvas.rect(0, height - 13 * mm, width, 13 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont(FONT_BOLD, 11)
    canvas.drawString(18 * mm, height - 8.5 * mm, "CONSOLIDATOR PARTS")
    canvas.setFillColor(colors.HexColor("#746f79"))
    canvas.setFont(FONT_REGULAR, 7)
    canvas.drawString(18 * mm, 9 * mm, "Документ сформирован системой Consolidator Parts")
    canvas.drawRightString(width - 18 * mm, 9 * mm, f"Страница {document.page}")
    canvas.restoreState()


def _party_table(left_title, left, right_title, right, styles):
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    left_text = "<br/>".join(_party_lines(left))
    right_text = "<br/>".join(_party_lines(right))
    table = Table(
        [
            [Paragraph(left_title, styles["h2"]), Paragraph(right_title, styles["h2"])],
            [Paragraph(left_text, styles["body"]), Paragraph(right_text, styles["body"])],
        ],
        colWidths=[84 * mm, 84 * mm],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2efed")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbc5c2")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#ded9d6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _signature_history(document, styles):
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    if not document:
        return []
    signatures = list(document.signatures.order_by("signed_at"))
    if not signatures:
        return []
    rows = [["Подписант", "Роль", "Дата", "Контрольная сумма"]]
    role_labels = {
        "buyer": "Покупатель",
        "seller": "Продавец",
        "operator": "Оператор",
        "operator_payment": "Финансовый оператор",
        "admin": "Администратор",
    }
    for signature in signatures:
        rows.append([
            _safe(signature.signer_name),
            role_labels.get(signature.signer_role, "Оператор"),
            signature.signed_at.strftime("%d.%m.%Y %H:%M"),
            (signature.doc_sha256 or "—")[:16],
        ])
    table = Table(rows, colWidths=[51 * mm, 35 * mm, 39 * mm, 43 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2efed")),
        ("FONTNAME", (0, 0), (-1, 0), styles["h2"].fontName),
        ("FONTNAME", (0, 1), (-1, -1), styles["small"].fontName),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#ded9d6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return [Paragraph("Электронные подписи", styles["h2"]), table]


def build_contract_pdf(contract) -> io.BytesIO:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    styles = _styles()
    document = _document(_page, title=contract.number)
    platform = contract.platform_snapshot or {}
    counterparty = contract.counterparty_snapshot or {}
    terms = contract.terms_snapshot or {}
    buyer_contract = contract.kind == "buyer_sale"
    title = "ДОГОВОР ПОСТАВКИ" if buyer_contract else "ЗАКУПОЧНЫЙ ДОГОВОР"
    counterparty_title = "Покупатель" if buyer_contract else "Поставщик"
    order = contract.order
    story = [
        Paragraph(title, styles["title"]),
        Paragraph(
            f"№ {contract.number} · от {contract.issued_at or contract.created_at:%d.%m.%Y}",
            styles["right"],
        ),
        Spacer(1, 4 * mm),
        _party_table("Consolidator Parts", platform, counterparty_title, counterparty, styles),
        Paragraph("1. Предмет договора", styles["h2"]),
        Paragraph(
            (
                "Consolidator Parts обязуется поставить запасные части "
                f"по заказу ORD-{order.id}, а Покупатель обязуется принять товар и "
                "оплатить его на условиях настоящего договора."
                if buyer_contract
                else
                "Поставщик обязуется передать Consolidator Parts запасные части "
                f"по заказу ORD-{order.id}, а Consolidator Parts обязуется принять "
                "товар и оплатить его на условиях настоящего договора."
            ),
            styles["body"],
        ),
        Paragraph("2. Цена и порядок расчётов", styles["h2"]),
        Paragraph(
            f"Цена договора: {_money(contract.amount, contract.currency)}. "
            f"Первый платёж составляет {terms.get('reserve_percent', '10')}% по отдельному счёту. "
            "Оставшаяся сумма оплачивается по окончательному счёту до отгрузки. "
            "Платёж считается совершённым после подтверждения поступления финансовым оператором.",
            styles["body"],
        ),
        Paragraph("3. Поставка", styles["h2"]),
        Paragraph(
            f"Базис поставки: {_safe(terms.get('incoterm'))}. "
            f"Способ доставки: {_safe(terms.get('shipping_mode'))}. "
            f"Адрес поставки: {_safe(terms.get('delivery_address'))}. "
            "Состав, количество и стоимость позиций определяются заказом и счетами.",
            styles["body"],
        ),
        Paragraph("4. Приёмка и документы", styles["h2"]),
        Paragraph(
            "Стороны фиксируют отгрузку, приёмку, замечания и подтверждающие документы "
            "в системе. Расхождения оформляются рекламацией с приложением доказательств.",
            styles["body"],
        ),
        Paragraph("5. Конфиденциальность", styles["h2"]),
        Paragraph(
            "Коммерческие условия и сведения о контрагентах доступны только соответствующей "
            "стороне и уполномоченным сотрудникам Consolidator Parts.",
            styles["body"],
        ),
        Paragraph("6. Электронное взаимодействие", styles["h2"]),
        Paragraph(
            "Документы и подтверждения, подписанные в учётной записи стороны, фиксируются "
            "с датой, пользователем и контрольной суммой файла. Электронная редакция хранится "
            "в системе и доступна только сторонам соответствующего договора.",
            styles["body"],
        ),
        Spacer(1, 6 * mm),
    ]
    signatures = Table(
        [
            ["Consolidator Parts", counterparty_title],
            [
                f"{_safe(platform.get('signatory'))}\n{_safe(platform.get('signatory_title'))}",
                f"{_safe(counterparty.get('signatory'))}\n{_safe(counterparty.get('signatory_title'))}",
            ],
            ["Подпись: __________________", "Подпись: __________________"],
        ],
        colWidths=[84 * mm, 84 * mm],
    )
    signatures.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), styles["h2"].fontName),
        ("FONTNAME", (0, 1), (-1, -1), styles["body"].fontName),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbc5c2")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#ded9d6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(signatures)
    story.extend(_signature_history(contract.document, styles))
    document.build(story, onFirstPage=_page, onLaterPages=_page)
    document._settlement_buffer.seek(0)
    return document._settlement_buffer


def build_invoice_pdf(invoice) -> io.BytesIO:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    styles = _styles()
    document = _document(_page, title=invoice.number)
    contract = invoice.contract
    platform = contract.platform_snapshot or {}
    counterparty = contract.counterparty_snapshot or {}
    incoming = invoice.direction == "receivable"
    issuer = platform if incoming else counterparty
    recipient = counterparty if incoming else platform
    order = invoice.order
    item_rows = []
    items = order.items.select_related("part", "part__seller").all()
    if invoice.seller_id:
        items = items.filter(part__seller_id=invoice.seller_id)
    for item in items:
        description = _safe(item.part.title if item.part else "Запасная часть")
        oem = _safe(item.part.oem_number if item.part else "")
        item_rows.append([
            f"{description}<br/><font size='7'>{oem}</font>",
            str(item.quantity),
            _money(item.unit_price, invoice.currency),
            _money(item.unit_price * item.quantity, invoice.currency),
        ])
    order_items_total = sum(
        (item.unit_price * item.quantity for item in items),
        Decimal("0.00"),
    )

    story = [
        Paragraph("СЧЁТ НА ОПЛАТУ", styles["title"]),
        Paragraph(
            f"№ {invoice.number} · договор {contract.number}", styles["right"]
        ),
        Spacer(1, 4 * mm),
        _party_table("Поставщик / получатель средств", issuer, "Плательщик", recipient, styles),
        Paragraph("Назначение", styles["h2"]),
        Paragraph(
            f"Заказ ORD-{order.id} · {invoice.get_stage_display()}. "
            f"Срок оплаты: до {invoice.due_date:%d.%m.%Y}. "
            f"Код для назначения платежа: <b>{invoice.reference_code}</b>.",
            styles["body"],
        ),
    ]
    table_data = [["Позиция", "Кол-во", "Цена", "Сумма"]]
    for description, quantity, price, total in item_rows:
        table_data.append([
            Paragraph(description, styles["body"]), quantity, price, total
        ])
    table = Table(table_data, colWidths=[82 * mm, 18 * mm, 34 * mm, 34 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#15121a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), styles["h2"].fontName),
        ("FONTNAME", (0, 1), (-1, -1), styles["body"].fontName),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbc5c2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([
        table,
        Spacer(1, 5 * mm),
        Paragraph(
            f"СТОИМОСТЬ ПОЗИЦИЙ ПО ДОГОВОРУ: {_money(order_items_total, invoice.currency)}",
            styles["right"],
        ),
        Paragraph(f"К ОПЛАТЕ: {_money(invoice.amount, invoice.currency)}", styles["right"]),
        Paragraph(
            "В назначении платежа обязательно укажите номер счёта и код платежа. "
            "Статус изменится после проверки банковского поступления финансовым оператором.",
            styles["small"],
        ),
    ])
    story.extend(_signature_history(invoice.document, styles))
    document.build(story, onFirstPage=_page, onLaterPages=_page)
    document._settlement_buffer.seek(0)
    return document._settlement_buffer


def save_contract_document(contract, actor=None):
    audience = "buyer" if contract.kind == "buyer_sale" else "seller"
    doc_type = "buyer_contract" if contract.kind == "buyer_sale" else "seller_contract"
    document = contract.document
    if not document:
        document = OrderDocument.objects.create(
            order=contract.order,
            doc_type=doc_type,
            audience=audience,
            seller=contract.seller,
            title=f"{contract.get_kind_display()} {contract.number}",
            uploaded_by=None,
        )
        contract.document = document
        contract.save(update_fields=["document", "updated_at"])
    elif document.uploaded_by_id:
        document.uploaded_by = None
        document.save(update_fields=["uploaded_by"])
    buffer = build_contract_pdf(contract)
    filename = f"{contract.number.replace('/', '-')}.pdf"
    document.file_obj.save(filename, ContentFile(buffer.read()), save=True)
    return document


def save_invoice_document(invoice, actor=None):
    audience = "buyer" if invoice.direction == "receivable" else "seller"
    document = invoice.document
    if not document:
        document = OrderDocument.objects.create(
            order=invoice.order,
            doc_type="settlement_invoice",
            audience=audience,
            seller=invoice.seller,
            title=f"Счёт {invoice.number} · {invoice.get_stage_display()}",
            uploaded_by=None,
        )
        invoice.document = document
        invoice.save(update_fields=["document", "updated_at"])
    elif document.uploaded_by_id:
        document.uploaded_by = None
        document.save(update_fields=["uploaded_by"])
    buffer = build_invoice_pdf(invoice)
    filename = f"{invoice.number.replace('/', '-')}.pdf"
    document.file_obj.save(filename, ContentFile(buffer.read()), save=True)
    return document
