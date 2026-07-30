from django.contrib.auth.models import User
from django.test import TestCase

from marketplace.models import UserProfile


class RemovedSellerPortalSmokeTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username="seller_portal_removed",
            password="strong-test-password",
        )
        UserProfile.objects.create(
            user=self.seller,
            role="seller",
            company_name="Removed portal supplier",
        )
        self.client.force_login(self.seller)

    def test_every_old_seller_page_returns_gone(self):
        for url in (
            "/seller/",
            "/seller/products/",
            "/seller/requests/",
            "/seller/orders/",
            "/seller/imports/1/",
            "/seller/parts/1/edit/",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response["Cache-Control"], "no-store")

    def test_seller_api_remains_available(self):
        response = self.client.get("/api/v1/seller/products/")
        self.assertEqual(response.status_code, 200)
