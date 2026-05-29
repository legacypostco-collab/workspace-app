import unittest
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from marketplace.models import Category, Part, UserProfile


# См. test_seller_portal_smoke — production STORAGES требует collectstatic.
@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ApiHardeningTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="api_user", password="test12345")
        self.seller = User.objects.create_user(username="seller_user", password="test12345")
        seller_profile, _ = UserProfile.objects.get_or_create(user=self.seller)
        seller_profile.role = "seller"
        seller_profile.can_manage_assortment = True
        seller_profile.can_manage_pricing = True
        seller_profile.save()

        self.category = Category.objects.create(name="Engine", slug="engine")
        self.part = Part.objects.create(
            seller=self.seller,
            category=self.category,
            title="Main Switch",
            slug="main-switch",
            oem_number="RE48786",
            price=Decimal("295.00"),
            stock_quantity=10,
            condition="oem",
            is_active=True,
        )

    # NOTE: тесты test_quote_preview_* и test_update_template_*
    # удалены — они проверяли эндпоинты /api/v1/quote/preview/ и
    # /api/v1/template/update/, которые были удалены при переходе на
    # AI-чат-интерфейс. 404 + рендер error-page триггерит баг
    # Python 3.14 × Django (super()/copy в Context.__copy__), что
    # делало failure похожим на ошибку нашего кода — на самом деле
    # тесты обращались к мёртвым URL-ам.

    @unittest.skip("PIVOT chat-first: /seller/upload/ удалён, файлы грузятся через "
                    "/api/assistant/upload-pricelist/ — лимиты проверяются на новом endpoint, "
                    "тесты надо переписать под него.")
    @override_settings(MAX_IMPORT_FILE_BYTES=100)
    def test_import_oversize_returns_413(self):
        pass

    @unittest.skip("PIVOT chat-first: /seller/upload/ удалён, см. test_import_oversize_returns_413.")
    @override_settings(MAX_IMPORT_ROWS=1)
    def test_import_too_many_rows_returns_413(self):
        pass
