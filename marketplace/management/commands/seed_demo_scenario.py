from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from marketplace.models import Brand, Category, Part, RFQ, RFQItem, UserProfile


class Command(BaseCommand):
    help = "Create realistic demo data for proposal/logistics testing."

    def handle(self, *args, **options):
        # Users
        buyer, _ = User.objects.get_or_create(
            username="demo_buyer",
            defaults={"email": "buyer@demo.com", "first_name": "Demo", "last_name": "Buyer"},
        )
        buyer.set_password("demo12345")
        buyer.save(update_fields=["password"])
        UserProfile.objects.get_or_create(
            user=buyer,
            defaults={"role": "buyer", "company_name": "Demo Fleet LLC"},
        )

        seller, _ = User.objects.get_or_create(
            username="demo_seller",
            defaults={"email": "seller@demo.com", "first_name": "Demo", "last_name": "Seller"},
        )
        seller.set_password("demo12345")
        seller.save(update_fields=["password"])
        seller_profile, _ = UserProfile.objects.get_or_create(
            user=seller,
            defaults={"role": "seller", "company_name": "Consolidator Supplier"},
        )
        seller_profile.role = "seller"
        seller_profile.company_name = "Consolidator Supplier"
        seller_profile.external_score = Decimal("86.00")
        seller_profile.behavioral_score = Decimal("82.00")
        seller_profile.can_manage_assortment = True
        seller_profile.can_manage_pricing = True
        seller_profile.can_manage_orders = True
        seller_profile.save()

        # Operator
        operator, _ = User.objects.get_or_create(
            username="demo_operator",
            defaults={"email": "operator@demo.com", "first_name": "Demo", "last_name": "Operator"},
        )
        operator.set_password("demo12345")
        operator.save(update_fields=["password"])
        UserProfile.objects.get_or_create(
            user=operator,
            defaults={"role": "operator", "company_name": "Consolidator Ops"},
        )

        # Taxonomy
        brand = Brand.objects.filter(name__iexact="Komatsu").first()
        if not brand:
            brand, _ = Brand.objects.get_or_create(
                slug="komatsu",
                defaults={"name": "Komatsu", "region": "europe"},
            )

        category = Category.objects.filter(name="Гидравлическая система").first()
        if not category:
            category, _ = Category.objects.get_or_create(
                slug="hydraulic-system",
                defaults={"name": "Гидравлическая система"},
            )

        demo_parts = [
            {
                "title": "MAIN SWITCH",
                "oem_number": "KM-RE48786",
                "price": Decimal("295.00"),
                "stock": 16,
                "weight": Decimal("1.20"),
                "l": Decimal("24.00"),
                "w": Decimal("18.00"),
                "h": Decimal("12.00"),
            },
            {
                "title": "HYDRAULIC CONTROL VALVE",
                "oem_number": "KM-21W-60-41112",
                "price": Decimal("1280.00"),
                "stock": 5,
                "weight": Decimal("8.70"),
                "l": Decimal("40.00"),
                "w": Decimal("29.00"),
                "h": Decimal("21.00"),
            },
            {
                "title": "PRESSURE SENSOR",
                "oem_number": "KM-7861-93-3320",
                "price": Decimal("180.00"),
                "stock": 30,
                "weight": Decimal("0.15"),
                "l": Decimal("10.00"),
                "w": Decimal("6.00"),
                "h": Decimal("4.00"),
            },
        ]

        created_parts = []
        for row in demo_parts:
            slug = f"{slugify(row['title'])[:200]}-{uuid4().hex[:8]}"
            part, _ = Part.objects.update_or_create(
                seller=seller,
                oem_number=row["oem_number"],
                defaults={
                    "title": row["title"],
                    "slug": slug,
                    "description": f"{row['title']} for Komatsu equipment.",
                    "price": row["price"],
                    "stock_quantity": row["stock"],
                    "condition": "oem",
                    "image_url": "/static/marketplace/komatsu-logo.svg",
                    "brand": brand,
                    "category": category,
                    "is_active": True,
                    "availability": "in_stock",
                    "availability_status": "active",
                    "currency": "USD",
                    "incoterm": "FOB",
                    "moq": 1,
                    "production_lead_days": 7,
                    "prep_to_ship_days": 2,
                    "shipping_lead_days": 12,
                    "gross_weight_kg": row["weight"],
                    "length_cm": row["l"],
                    "width_cm": row["w"],
                    "height_cm": row["h"],
                    "country_of_origin": "China",
                    "mapping_status": "confirmed",
                },
            )
            created_parts.append(part)

        # RFQ with matched items
        rfq = RFQ.objects.create(
            created_by=buyer,
            customer_name="Demo Buyer",
            customer_email="buyer@demo.com",
            company_name="Demo Fleet LLC",
            mode="auto",
            urgency="standard",
            status="quoted",
            notes="Demo RFQ for logistics proposal testing.",
        )

        qtys = [2, 1, 4]
        for idx, part in enumerate(created_parts):
            RFQItem.objects.create(
                rfq=rfq,
                query=part.oem_number,
                quantity=qtys[idx],
                matched_part=part,
                state="auto_matched",
                confidence=Decimal("92.00"),
                decision_reason="demo_seed_auto_matched",
                recommended_supplier_status="trusted",
            )

        self.stdout.write(self.style.SUCCESS("Demo scenario created successfully."))
        self.stdout.write("Credentials:")
        self.stdout.write("  buyer: demo_buyer / demo12345")
        self.stdout.write("  seller: demo_seller / demo12345")
        self.stdout.write("  operator: demo_operator / demo12345")
        self.stdout.write(f"Open RFQ: /rfq/{rfq.id}/")
        self.stdout.write(f"Open Proposal: /rfq/{rfq.id}/proposal/")
