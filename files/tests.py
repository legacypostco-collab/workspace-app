from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .storage import _save_bytes_to_storage, read_stored_file_bytes


class StorageFallbackSecurityTests(SimpleTestCase):
    @override_settings(DEBUG=False, TESTING=False)
    @patch(
        "files.storage.default_storage.save",
        side_effect=PermissionError("denied"),
    )
    def test_production_does_not_silently_write_to_temp(self, _save):
        with self.assertRaises(PermissionError):
            _save_bytes_to_storage("imports/source/file.csv", b"data")

    @override_settings(DEBUG=True, TESTING=False)
    @patch(
        "files.storage.default_storage.open",
        side_effect=FileNotFoundError("missing"),
    )
    def test_local_fallback_rejects_path_traversal(self, _open):
        with self.assertRaises(ValueError):
            read_stored_file_bytes("../../etc/passwd")
