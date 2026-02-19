from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from marketplace.models import UserProfile


class SupplierRatingModelTests(TestCase):
    def test_rating_formula_and_status_trusted(self):
        user = User.objects.create_user(username="supplier1", password="x")
        profile = UserProfile.objects.create(
            user=user,
            role="seller",
            external_score=Decimal("90"),
            behavioral_score=Decimal("80"),
        )
        profile.refresh_from_db()
        self.assertEqual(profile.rating_score, Decimal("86.00"))  # 90*0.6 + 80*0.4
        self.assertEqual(profile.supplier_status, "trusted")

    def test_status_sandbox(self):
        user = User.objects.create_user(username="supplier2", password="x")
        profile = UserProfile.objects.create(
            user=user,
            role="seller",
            external_score=Decimal("70"),
            behavioral_score=Decimal("65"),
        )
        profile.refresh_from_db()
        self.assertEqual(profile.supplier_status, "sandbox")

    def test_status_risky(self):
        user = User.objects.create_user(username="supplier3", password="x")
        profile = UserProfile.objects.create(
            user=user,
            role="seller",
            external_score=Decimal("40"),
            behavioral_score=Decimal("30"),
        )
        profile.refresh_from_db()
        self.assertEqual(profile.supplier_status, "risky")

    def test_bankruptcy_forces_rejected(self):
        user = User.objects.create_user(username="supplier4", password="x")
        profile = UserProfile.objects.create(
            user=user,
            role="seller",
            external_score=Decimal("95"),
            behavioral_score=Decimal("95"),
            bankruptcy_flag=True,
        )
        profile.refresh_from_db()
        self.assertEqual(profile.supplier_status, "rejected")
        self.assertEqual(profile.rating_score, Decimal("0.00"))
