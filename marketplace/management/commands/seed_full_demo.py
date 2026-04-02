"""Generate rich demo data for all admin panel tabs."""
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils.text import slugify
from django.core.management.base import BaseCommand
from django.utils import timezone

from marketplace.models import (
    Brand,
    Category,
    Order,
    OrderClaim,
    OrderDocument,
    OrderEvent,
    OrderItem,
    Part,
    RFQ,
    RFQItem,
    SupplierRatingEvent,
    UserProfile,
    WebhookDeliveryLog,
)


class Command(BaseCommand):
    help = "Seed database with rich demo data for all admin tabs"

    def handle(self, *args, **options):
        now = timezone.now()

        # ── Users ──────────────────────────────────────────────
        buyers = []
        buyer_names = [
            ("Алексей", "Петров", "ООО СтройМаш"),
            ("Мария", "Иванова", "АО ТехноПарк"),
            ("Дмитрий", "Козлов", "ИП Козлов"),
            ("Елена", "Сидорова", "ООО МинералТранс"),
            ("Игорь", "Васильев", "ЗАО Уралмаш-Сервис"),
            ("Ольга", "Николаева", "ООО Ромашка"),
            ("Сергей", "Морозов", "АО Горнодобыча"),
            ("Наталья", "Кузнецова", "ООО ТехЛайн"),
            ("Андрей", "Попов", "ИП Попов"),
            ("Татьяна", "Лебедева", "ООО ЭнергоМир"),
        ]
        for i, (first, last, company) in enumerate(buyer_names, start=1):
            u, _ = User.objects.get_or_create(
                username=f"buyer_{i}",
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "email": f"buyer{i}@example.com",
                    "date_joined": now - timedelta(days=random.randint(5, 180)),
                },
            )
            u.set_password("demo12345")
            u.save()
            profile, _ = UserProfile.objects.get_or_create(
                user=u, defaults={"role": "buyer", "company_name": company}
            )
            buyers.append(u)

        sellers = []
        seller_data = [
            ("Zhang", "Wei", "Shanghai Heavy Parts Co.", "trusted", 85),
            ("Li", "Ming", "Guangzhou Machinery Ltd.", "trusted", 78),
            ("Ahmed", "Hassan", "Dubai Parts Trading LLC", "sandbox", 65),
            ("Kemal", "Yilmaz", "Istanbul Spare Parts A.S.", "sandbox", 55),
            ("Pavel", "Novak", "Czech Machinery s.r.o.", "trusted", 90),
            ("Hans", "Mueller", "German Parts GmbH", "trusted", 88),
            ("Rajesh", "Kumar", "Mumbai Engineering Pvt.", "sandbox", 60),
            ("Carlos", "Silva", "Brazil Maquinas Ltda.", "risky", 40),
        ]
        for i, (first, last, company, status, score) in enumerate(seller_data, start=1):
            u, _ = User.objects.get_or_create(
                username=f"seller_{i}",
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "email": f"seller{i}@example.com",
                    "date_joined": now - timedelta(days=random.randint(30, 365)),
                },
            )
            u.set_password("demo12345")
            u.save()
            profile, _ = UserProfile.objects.get_or_create(
                user=u,
                defaults={
                    "role": "seller",
                    "company_name": company,
                    "external_score": Decimal(str(score)),
                    "behavioral_score": Decimal(str(max(score - 5, 30))),
                },
            )
            sellers.append(u)

        # ── Ensure categories & brands ─────────────────────────
        cat_names = [
            "Engine", "Filters", "Hydraulic", "Undercarriage",
            "Electrical", "Brakes", "Cooling", "Drivetrain",
            "Cabin", "Attachments",
        ]
        cats = []
        for name in cat_names:
            c, _ = Category.objects.get_or_create(name=name, defaults={"slug": slugify(name)})
            cats.append(c)

        brands = list(Brand.objects.all()[:15])
        if not brands:
            for bname in ["Caterpillar", "Komatsu", "Volvo", "Liebherr", "Hitachi",
                          "Sandvik", "Epiroc", "Atlas Copco", "Cummins", "Bosch"]:
                b, _ = Brand.objects.get_or_create(name=bname, defaults={"slug": slugify(bname), "region": "europe"})
                brands.append(b)

        # ── Parts ──────────────────────────────────────────────
        part_templates = [
            ("Hydraulic Pump Assembly", "hydraulic", 2500, 8500),
            ("Oil Filter Element", "filter", 15, 120),
            ("Fuel Injector Nozzle", "engine", 180, 650),
            ("Track Roller (Double)", "undercarriage", 350, 1200),
            ("Alternator 24V 80A", "electrical", 280, 900),
            ("Brake Disc Front", "brake", 120, 450),
            ("Radiator Assembly", "cooling", 800, 3200),
            ("Final Drive Motor", "drivetrain", 4500, 18000),
            ("Cabin Air Filter", "cabin", 8, 35),
            ("Bucket Teeth Set", "attachment", 45, 280),
            ("Turbocharger Core", "engine", 1200, 5500),
            ("Hydraulic Cylinder Seal Kit", "hydraulic", 25, 180),
            ("Starter Motor 24V", "electrical", 350, 1100),
            ("Idler Wheel Assembly", "undercarriage", 600, 2200),
            ("Coolant Thermostat", "cooling", 30, 150),
            ("Transmission Filter", "filter", 20, 90),
            ("Piston Ring Set", "engine", 80, 320),
            ("Track Chain Link", "undercarriage", 15, 65),
            ("Wiper Motor", "cabin", 60, 220),
            ("Quick Coupler Pin", "attachment", 35, 140),
            ("Water Pump Assembly", "cooling", 400, 1800),
            ("Air Compressor", "engine", 900, 4200),
            ("Brake Pad Set", "brake", 45, 190),
            ("Drive Shaft", "drivetrain", 1500, 6500),
            ("Voltage Regulator", "electrical", 55, 210),
        ]
        cat_map = {c.name.lower(): c for c in cats}
        part_cat_mapping = {
            "hydraulic": "Hydraulic", "filter": "Filters", "engine": "Engine",
            "undercarriage": "Undercarriage", "electrical": "Electrical",
            "brake": "Brakes", "cooling": "Cooling", "drivetrain": "Drivetrain",
            "cabin": "Cabin", "attachment": "Attachments",
        }
        parts = list(Part.objects.all())
        for idx, (title, ptype, min_p, max_p) in enumerate(part_templates):
            brand = random.choice(brands)
            seller = random.choice(sellers)
            price = Decimal(str(random.randint(min_p, max_p)))
            oem = f"{random.choice('ABCDEFGH')}{random.randint(100, 999)}-{random.randint(1000, 9999)}"
            cat = cat_map.get(part_cat_mapping[ptype].lower(), cats[0])
            slug = slugify(f"{title}-{brand.name}-{oem}")
            p, created = Part.objects.get_or_create(
                oem_number=oem,
                defaults={
                    "title": f"{title} ({brand.name})",
                    "slug": slug,
                    "brand": brand,
                    "category": cat,
                    "seller": seller,
                    "price": price,
                    "currency": random.choice(["USD", "EUR", "CNY"]),
                    "condition": random.choice(["oem", "aftermarket", "reman"]),
                    "stock_quantity": random.randint(0, 50),
                    "is_active": True,
                },
            )
            parts.append(p)

        # Block a few parts for moderation tab
        blocked_ids = list(Part.objects.filter(is_active=True).order_by("?").values_list("id", flat=True)[:3])
        Part.objects.filter(id__in=blocked_ids).update(availability_status="blocked", is_active=False, admin_note="Подозрение на контрафакт")

        # ── Orders ─────────────────────────────────────────────
        statuses = ["pending", "reserve_paid", "confirmed", "in_production",
                     "ready_to_ship", "shipped", "delivered", "completed", "cancelled"]
        payment_statuses = ["awaiting_reserve", "reserve_paid", "mid_paid", "paid"]
        sla_statuses = ["on_track", "on_track", "on_track", "at_risk", "breached"]

        orders = []
        for i in range(40):
            buyer = random.choice(buyers)
            status = random.choice(statuses)
            days_ago = random.randint(0, 90)
            total = Decimal(str(random.randint(500, 85000)))
            sla = random.choice(sla_statuses)

            o = Order.objects.create(
                customer_name=buyer.get_full_name() or buyer.username,
                customer_email=buyer.email,
                customer_phone=f"+7-{random.randint(900,999)}-{random.randint(100,999)}-{random.randint(10,99)}-{random.randint(10,99)}",
                delivery_address=random.choice([
                    "Москва, ул. Промышленная 12", "Екатеринбург, Заводской пр. 45",
                    "Новосибирск, ул. Технопарковая 8", "Казань, ул. Машиностроителей 3",
                    "Краснодар, Промзона Южная 15", "Владивосток, ул. Портовая 22",
                ]),
                buyer=buyer,
                status=status,
                total_amount=total,
                payment_status=random.choice(payment_statuses),
                sla_status=sla,
                sla_breaches_count=random.randint(0, 3) if sla == "breached" else 0,
                invoice_number=f"INV-2026-{random.randint(1000, 9999)}",
                supplier_confirm_deadline=now - timedelta(days=days_ago - 2) if status != "pending" else now + timedelta(days=1),
                ship_deadline=now - timedelta(days=days_ago - 10) if status in ("shipped", "delivered", "completed") else now + timedelta(days=random.randint(5, 30)),
                reserve_amount=total * Decimal("0.10"),
                logistics_cost=Decimal(str(random.randint(100, 5000))),
            )
            # Backdate created_at
            Order.objects.filter(id=o.id).update(created_at=now - timedelta(days=days_ago))

            # Order items
            n_items = random.randint(1, 4)
            for _ in range(n_items):
                part = random.choice(parts)
                qty = random.randint(1, 10)
                OrderItem.objects.create(
                    order=o, part=part, quantity=qty,
                    unit_price=part.price or Decimal("100"),
                )

            orders.append(o)

        # ── Order Events ───────────────────────────────────────
        event_types = ["order_created", "status_changed", "sla_status_changed",
                       "reserve_paid", "invoice_opened", "document_uploaded"]
        sources = ["system", "buyer", "seller", "operator"]
        for o in orders:
            n_events = random.randint(1, 5)
            for j in range(n_events):
                evt = OrderEvent.objects.create(
                    order=o,
                    event_type=random.choice(event_types),
                    source=random.choice(sources),
                    meta={"comment": random.choice([
                        "Заказ создан", "Статус изменён", "Оплата подтверждена",
                        "Документ загружен", "SLA обновлён", "Резерв оплачен",
                        "Отгрузка запланирована", "Рекламация открыта",
                    ])},
                )
                OrderEvent.objects.filter(id=evt.id).update(
                    created_at=now - timedelta(days=random.randint(0, 60), hours=random.randint(0, 23))
                )

        # ── RFQs ───────────────────────────────────────────────
        rfq_statuses = ["new", "quoted", "needs_review", "cancelled"]
        rfq_notes = [
            "Срочная поставка для Кузбасского карьера",
            "Замена вышедших из строя узлов на CAT 390F",
            "Плановое ТО парка Komatsu PC200",
            "Расширение склада запчастей",
            "Аварийная замена гидронасоса",
            "",
        ]
        for i in range(15):
            buyer = random.choice(buyers)
            rfq = RFQ.objects.create(
                created_by=buyer,
                customer_name=buyer.get_full_name() or buyer.username,
                customer_email=buyer.email,
                status=random.choice(rfq_statuses),
                notes=random.choice(rfq_notes),
            )
            RFQ.objects.filter(id=rfq.id).update(created_at=now - timedelta(days=random.randint(0, 45)))
            for _ in range(random.randint(1, 5)):
                part = random.choice(parts)
                RFQItem.objects.create(
                    rfq=rfq,
                    query=f"{part.oem_number} {part.brand.name if part.brand else ''}".strip(),
                    quantity=random.randint(1, 20),
                    matched_part=part if random.random() > 0.3 else None,
                )

        # ── Claims ─────────────────────────────────────────────
        claim_titles = [
            "Несоответствие OEM-номера",
            "Повреждение при транспортировке",
            "Неполная комплектация",
            "Дефект производства",
            "Просрочка поставки > 14 дней",
            "Не соответствует описанию",
            "Гарантийный случай — течь уплотнителя",
        ]
        for o in random.sample(orders, min(12, len(orders))):
            OrderClaim.objects.create(
                order=o,
                title=random.choice(claim_titles),
                description="Детали рекламации прилагаются в документах заказа.",
                status=random.choice(["open", "open", "in_review", "approved", "rejected"]),
                opened_by=o.buyer,
            )

        # ── Documents ──────────────────────────────────────────
        doc_types = ["invoice", "packing_list", "certificate", "quality_report", "customs"]
        for o in random.sample(orders, min(20, len(orders))):
            for _ in range(random.randint(1, 3)):
                OrderDocument.objects.create(
                    order=o,
                    doc_type=random.choice(doc_types),
                    title=f"DOC-{o.id}-{random.randint(100,999)}",
                    uploaded_by=random.choice(sellers),
                )

        # ── Supplier Rating Events ─────────────────────────────
        rating_event_types = [
            "rfq_response", "data_mismatch", "delivery_delay",
            "order_cancellation", "return", "sandbox_selected",
        ]
        for seller in sellers:
            for _ in range(random.randint(2, 8)):
                evt = SupplierRatingEvent.objects.create(
                    supplier=seller,
                    event_type=random.choice(rating_event_types),
                    impact_score=Decimal(str(random.randint(-10, 10))),
                    meta={"reason": random.choice([
                        "Быстрый ответ на RFQ",
                        "Несоответствие данных в каталоге",
                        "Задержка отгрузки на 5 дней",
                        "Отмена заказа поставщиком",
                        "Возврат бракованного товара",
                        "Модерация: sandbox → trusted",
                    ])},
                )
                SupplierRatingEvent.objects.filter(id=evt.id).update(
                    created_at=now - timedelta(days=random.randint(0, 90))
                )

        # ── Summary ────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(
            f"Done! Created:\n"
            f"  Buyers: {len(buyers)}\n"
            f"  Sellers: {len(sellers)}\n"
            f"  Parts: {Part.objects.count()}\n"
            f"  Orders: {Order.objects.count()}\n"
            f"  Order Events: {OrderEvent.objects.count()}\n"
            f"  RFQs: {RFQ.objects.count()}\n"
            f"  Claims: {OrderClaim.objects.count()}\n"
            f"  Documents: {OrderDocument.objects.count()}\n"
            f"  Rating Events: {SupplierRatingEvent.objects.count()}\n"
            f"  Blocked Parts: {Part.objects.filter(availability_status='blocked').count()}\n"
            f"  Sandbox Suppliers: {UserProfile.objects.filter(role='seller', supplier_status='sandbox').count()}\n"
        ))
