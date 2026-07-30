from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class NginxPrivateMediaTests(SimpleTestCase):
    def test_deploy_configs_do_not_serve_media_directly(self):
        root = Path(settings.BASE_DIR)
        for relative_path in ("deploy/nginx.conf", "deploy/nginx-prod.conf"):
            with self.subTest(config=relative_path):
                config = (root / relative_path).read_text(encoding="utf-8")
                self.assertIn("location ^~ /media/", config)
                self.assertNotIn("alias /var/www/workspace-app/media/", config)
                self.assertIn("server_tokens off;", config)
