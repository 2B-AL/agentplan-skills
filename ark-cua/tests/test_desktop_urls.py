import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cua  # noqa: E402


class DesktopURLTests(unittest.TestCase):
    def test_canonical_cua_app_url_is_not_rewritten(self):
        for path in (
            "/desktops/desk-1/cua-app/",
            "/pre/desktops/desk-1/cua-app/",
        ):
            with self.subTest(path=path):
                access_url = f"https://desktop.example.test{path}?ticket=redacted"
                desktop_view_url, full_interface_url = cua._derive_desktop_urls(access_url)
                self.assertIsNone(desktop_view_url)
                self.assertEqual(full_interface_url, access_url)

    def test_derived_cua_app_url_preserves_gateway_base_prefix(self):
        access_url = "https://desktop.example.test/pre/desktops/desk-1/api/status?ticket=redacted"
        desktop_view_url, full_interface_url = cua._derive_desktop_urls(access_url)
        self.assertEqual(desktop_view_url, access_url)
        self.assertEqual(
            full_interface_url,
            "https://desktop.example.test/pre/desktops/desk-1/cua-app/?ticket=redacted",
        )

    def test_legacy_url_conversion_is_unchanged(self):
        access_url = "https://desktop.example.test/legacy-desktop?ticket=redacted"
        desktop_view_url, full_interface_url = cua._derive_desktop_urls(access_url)
        self.assertEqual(desktop_view_url, access_url)
        self.assertEqual(
            full_interface_url,
            "https://desktop.example.test/cua-app/legacy-desktop?ticket=redacted",
        )


if __name__ == "__main__":
    unittest.main()
