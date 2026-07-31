import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase


class ContentSecurityPolicyTests(TestCase):
    def test_public_page_uses_strict_attribute_policy(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        policy = response.headers.get("Content-Security-Policy", "")
        self.assertTrue(policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertIn("script-src-attr 'none'", policy)
        self.assertIn("style-src-attr 'none'", policy)
        self.assertIn("object-src 'none'", policy)

    def test_templates_contain_no_inline_event_or_style_attributes(self):
        template_root = Path(settings.BASE_DIR) / "templates"
        attribute_pattern = re.compile(
            r"\s(?:style|on(?:click|change|input|submit|keydown|keyup|blur|focus|"
            r"mouseover|mouseout))\s*=",
            flags=re.IGNORECASE,
        )
        offenders = []
        for path in template_root.rglob("*.html"):
            if attribute_pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(settings.BASE_DIR)))

        self.assertEqual(offenders, [])

    def test_inline_script_and_style_blocks_have_nonce(self):
        template_root = Path(settings.BASE_DIR) / "templates"
        tag_pattern = re.compile(r"<(script|style)\b([^>]*)>", re.IGNORECASE)
        offenders = []
        for path in template_root.rglob("*.html"):
            source = path.read_text(encoding="utf-8")
            for match in tag_pattern.finditer(source):
                tag_name, attributes = match.groups()
                if tag_name.lower() == "script" and re.search(
                    r"\bsrc\s*=", attributes, re.IGNORECASE
                ):
                    continue
                if not re.search(r"\bnonce\s*=", attributes, re.IGNORECASE):
                    line = source.count("\n", 0, match.start()) + 1
                    offenders.append(
                        f"{path.relative_to(settings.BASE_DIR)}:{line}"
                    )

        self.assertEqual(offenders, [])
