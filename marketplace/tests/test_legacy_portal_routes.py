from django.contrib.auth.models import User
from django.test import TestCase

from marketplace.models import UserProfile


class RemovedLegacyPortalRoutesTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("legacy_route_buyer")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.client.force_login(self.buyer)

    def test_old_role_portals_are_gone_for_authenticated_users(self):
        for url in (
            "/buyer/",
            "/buyer/orders/",
            "/seller/",
            "/seller/products/",
            "/operator/",
            "/operator/logist/",
            "/operator/payments/escrow/",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response["Cache-Control"], "no-store")

    def test_chat_workspace_remains_available(self):
        response = self.client.get("/chat/?workspace=1")
        self.assertEqual(response.status_code, 200)

    def test_non_admin_still_cannot_open_admin_panel(self):
        response = self.client.get("/admin-panel/")
        self.assertEqual(response.status_code, 403)
