from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from marketplace.models import Brand, Category, Order, OrderClaim, OrderDocument, OrderEvent, OrderItem, Part, RFQ, RFQItem, UserProfile


class Command(BaseCommand):
    help = "Builds a complete demo product dataset: users, brands, parts, RFQ, invoices, and orders in multiple stages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete previous demo entities for demo_buyer/demo_seller and recreate from scratch.",
        )

    def _ensure_user(self, username: str, email: str, first_name: str, last_name: str, role: str, company: str) -> User:
        user, _ = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "first_name": first_name, "last_name": last_name},
        )
        user.email = email
        user.first_name = first_name
        user.last_name = last_name
        user.set_password("demo12345")
        user.save()

        profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"role": role, "company_name": company})
        profile.role = role
        profile.company_name = company
        if role == "seller":
            profile.external_score = Decimal("86.00")
            profile.behavioral_score = Decimal("84.00")
            profile.can_manage_assortment = True
            profile.can_manage_pricing = True
            profile.can_manage_orders = True
            profile.can_manage_drawings = True
            profile.can_view_analytics = True
            profile.can_manage_team = True
        profile.save()
        return user

    def _ensure_taxonomy(self):
        categories = [
            ("engines-power-units", "Двигатели и силовые агрегаты"),
            ("hydraulic-system", "Гидравлическая система"),
            ("electrical", "Электрооборудование"),
            ("working-equipment", "Рабочее оборудование"),
        ]
        category_map: dict[str, Category] = {}
        for slug, name in categories:
            cat = Category.objects.filter(name=name).first()
            if not cat:
                cat = Category.objects.filter(slug=slug).first()
            if not cat:
                cat = Category.objects.create(name=name, slug=slug)
            else:
                changed = False
                if cat.slug != slug and not Category.objects.filter(slug=slug).exclude(id=cat.id).exists():
                    cat.slug = slug
                    changed = True
                if cat.name != name:
                    cat.name = name
                    changed = True
                if changed:
                    cat.save(update_fields=["name", "slug"])
            category_map[name] = cat

        brands = [
            ("epiroc", "Epiroc", "europe"),
            ("liebherr", "Liebherr", "europe"),
            ("komatsu", "Komatsu", "europe"),
            ("sany", "Sany", "china"),
        ]
        brand_map: dict[str, Brand] = {}
        for slug, name, region in brands:
            brand, _ = Brand.objects.get_or_create(slug=slug, defaults={"name": name, "region": region})
            brand.name = name
            brand.region = region
            brand.save(update_fields=["name", "region"])
            brand_map[name] = brand
        return category_map, brand_map

    def _upsert_demo_parts(self, seller: User, category_map: dict[str, Category], brand_map: dict[str, Brand]):
        rows = [
            ("MAIN SWITCH", "RE48786", Decimal("295.00"), 30, "Электрооборудование", "Epiroc", "/static/marketplace/epiroc-logo.webp"),
            ("HYDRAULIC PUMP", "LH-9989363", Decimal("1480.00"), 8, "Гидравлическая система", "Liebherr", "/static/marketplace/liebherr-logo.svg"),
            ("SEAL KIT", "KM-21W-60-41112", Decimal("180.00"), 55, "Гидравлическая система", "Komatsu", "/static/marketplace/komatsu-logo.svg"),
            ("CONTROL VALVE", "SN-783920", Decimal("860.00"), 15, "Рабочее оборудование", "Sany", "/static/marketplace/consolidator-logo.svg"),
            ("PRESSURE SENSOR", "KM-7861-93-3320", Decimal("210.00"), 45, "Электрооборудование", "Komatsu", "/static/marketplace/komatsu-logo.svg"),
            ("CYLINDER ASSY", "EP-552031", Decimal("2290.00"), 5, "Рабочее оборудование", "Epiroc", "/static/marketplace/epiroc-logo.webp"),
        ]
        created: list[Part] = []
        for title, oem, price, stock, category_name, brand_name, image in rows:
            slug_base = slugify(f"{title}-{oem}")[:200] or "part"
            part, _ = Part.objects.update_or_create(
                seller=seller,
                oem_number=oem,
                defaults={
                    "title": title,
                    "slug": f"{slug_base}-{uuid4().hex[:6]}",
                    "description": f"{title} for heavy equipment.",
                    "price": price,
                    "stock_quantity": stock,
                    "condition": "oem",
                    "image_url": image,
                    "category": category_map[category_name],
                    "brand": brand_map[brand_name],
                    "is_active": True,
                    "availability": "in_stock",
                    "availability_status": "active",
                    "currency": "USD",
                    "incoterm": "FOB",
                    "moq": 1,
                    "production_lead_days": 5,
                    "prep_to_ship_days": 2,
                    "shipping_lead_days": 10,
                    "gross_weight_kg": Decimal("4.5"),
                    "length_cm": Decimal("28"),
                    "width_cm": Decimal("18"),
                    "height_cm": Decimal("14"),
                    "country_of_origin": "China",
                    "mapping_status": "confirmed",
                },
            )
            created.append(part)
        return created

    def _create_demo_rfq(self, buyer: User, parts: list[Part]) -> RFQ:
        rfq = RFQ.objects.create(
            created_by=buyer,
            customer_name="Demo Buyer",
            customer_email=buyer.email,
            company_name="Demo Fleet LLC",
            mode="auto",
            urgency="standard",
            status="quoted",
            notes="Bootstrap demo RFQ for full product test.",
        )
        for idx, part in enumerate(parts[:3]):
            RFQItem.objects.create(
                rfq=rfq,
                query=part.oem_number,
                quantity=idx + 1,
                matched_part=part,
                state="auto_matched",
                confidence=Decimal("92.00"),
                decision_reason="bootstrap_demo_auto_match",
                recommended_supplier_status="trusted",
            )
        return rfq

    def _create_order(
        self,
        buyer: User,
        parts: list[Part],
        status: str,
        payment_status: str,
        delivery_address: str,
        reserve_paid: bool,
        final_paid: bool,
    ) -> Order:
        subtotal = Decimal("0.00")
        item_data = []
        for idx, part in enumerate(parts[:3]):
            qty = idx + 1
            line = (part.price * qty).quantize(Decimal("0.01"))
            subtotal += line
            item_data.append((part, qty))

        logistics_cost = Decimal("190.00")
        total_amount = (subtotal + logistics_cost).quantize(Decimal("0.01"))
        reserve_amount = ((total_amount * Decimal("10.00")) / Decimal("100")).quantize(Decimal("0.01"))
        now = timezone.now()

        order = Order.objects.create(
            customer_name="Demo Buyer",
            customer_email=buyer.email,
            customer_phone="+7 999 000 00 00",
            delivery_address=delivery_address,
            buyer=buyer,
            status=status,
            payment_status=payment_status,
            reserve_percent=Decimal("10.00"),
            reserve_amount=reserve_amount,
            reserve_paid_at=now if reserve_paid else None,
            final_paid_at=now if final_paid else None,
            supplier_confirm_deadline=now + timedelta(hours=24),
            ship_deadline=now + timedelta(days=6),
            logistics_cost=logistics_cost,
            logistics_currency="USD",
            logistics_provider="internal_fallback",
            logistics_meta={"ok": True, "provider": "internal_fallback", "cost": str(logistics_cost)},
            total_amount=total_amount,
            invoice_number="",
            sla_status="on_track",
        )
        order.invoice_number = f"INV-{order.created_at:%Y%m%d}-{order.id}"
        order.save(update_fields=["invoice_number"])

        for part, qty in item_data:
            OrderItem.objects.create(order=order, part=part, quantity=qty, unit_price=part.price)

        OrderEvent.objects.create(
            order=order,
            event_type="order_created",
            source="buyer",
            actor=buyer,
            meta={"bootstrap": True, "status": status, "payment_status": payment_status},
        )
        return order

    def handle(self, *args, **options):
        with transaction.atomic():
            buyer = self._ensure_user(
                username="demo_buyer",
                email="buyer@demo.com",
                first_name="Demo",
                last_name="Buyer",
                role="buyer",
                company="Demo Fleet LLC",
            )
            seller = self._ensure_user(
                username="demo_seller",
                email="seller@demo.com",
                first_name="Demo",
                last_name="Seller",
                role="seller",
                company="Consolidator Supplier",
            )
            op = self._ensure_user(
                username="demo_operator",
                email="operator@demo.com",
                first_name="Demo",
                last_name="Operator",
                role="seller",
                company="Consolidator Ops",
            )
            # Operator needs is_staff so operator_required decorator passes
            if not op.is_staff:
                op.is_staff = True
                op.save(update_fields=["is_staff"])

            if options["reset"]:
                OrderEvent.objects.filter(order__buyer=buyer).delete()
                OrderClaim.objects.filter(order__buyer=buyer).delete()
                OrderDocument.objects.filter(order__buyer=buyer).delete()
                OrderItem.objects.filter(order__buyer=buyer).delete()
                Order.objects.filter(buyer=buyer).delete()
                RFQItem.objects.filter(rfq__created_by=buyer).delete()
                RFQ.objects.filter(created_by=buyer).delete()
                Part.objects.filter(seller=seller).delete()

            category_map, brand_map = self._ensure_taxonomy()
            parts = self._upsert_demo_parts(seller, category_map, brand_map)
            rfq = self._create_demo_rfq(buyer, parts)

            order_pending = self._create_order(
                buyer=buyer,
                parts=parts,
                status="pending",
                payment_status="awaiting_reserve",
                delivery_address="Moscow, Leninskiy 1",
                reserve_paid=False,
                final_paid=False,
            )
            order_progress = self._create_order(
                buyer=buyer,
                parts=parts,
                status="in_production",
                payment_status="paid",
                delivery_address="Moscow, Leninskiy 1",
                reserve_paid=True,
                final_paid=True,
            )
            order_delivery = self._create_order(
                buyer=buyer,
                parts=parts,
                status="delivered",
                payment_status="paid",
                delivery_address="Moscow, Leninskiy 1",
                reserve_paid=True,
                final_paid=True,
            )

            OrderDocument.objects.create(
                order=order_progress,
                doc_type="invoice",
                title=f"Invoice {order_progress.invoice_number}",
                file_url="https://example.com/demo/invoice.pdf",
                uploaded_by=seller,
            )
            OrderDocument.objects.create(
                order=order_progress,
                doc_type="packing_list",
                title="Packing List #PL-DEMO-001",
                file_url="https://example.com/demo/packing-list.pdf",
                uploaded_by=seller,
            )
            OrderClaim.objects.create(
                order=order_delivery,
                title="Повреждение упаковки",
                description="При приемке обнаружены повреждения внешней упаковки. Требуется проверка.",
                status="open",
                opened_by=buyer,
            )

        self.stdout.write(self.style.SUCCESS("Bootstrap complete: product demo is ready."))
        self.stdout.write("Credentials:")
        self.stdout.write("  buyer: demo_buyer / demo12345")
        self.stdout.write("  seller: demo_seller / demo12345")
        self.stdout.write("  operator: demo_operator / demo12345")
        self.stdout.write("Open pages:")
        self.stdout.write("  /demo-center/")
        self.stdout.write(f"  /rfq/{rfq.id}/proposal/")
        self.stdout.write(f"  /orders/{order_pending.id}/invoice/")
        self.stdout.write(f"  /orders/{order_progress.id}/")
        self.stdout.write(f"  /orders/{order_delivery.id}/")
