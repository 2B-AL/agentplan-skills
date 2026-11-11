# Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
import secrets
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cua
import cua_cookiecloud as sync
from cua_util import SkillError


# Generate test-only values rather than embedding credentials in fixtures.
FIXTURE_PASSWORD = secrets.token_urlsafe(24)
FIXTURE_TOKEN = secrets.token_urlsafe(24)


def configuration(received=None):
    return {
        "desktop_id": "desk-exact",
        "extension_id": sync.EXTENSION_ID,
        "storage_key": sync.STORAGE_KEY,
        "received_at": received,
        "settings": {
            "endpoint": "https://receiver.test/desk-exact",
            "uuid": "private-uuid",
            "password": FIXTURE_PASSWORD,
            "headers": "X-CookieCloud-Token:" + FIXTURE_TOKEN,
            "type": "up",
            "with_storage": 0,
            "interval": 10,
            "crypto_type": "legacy",
        },
    }


class CookieCloudAutoTests(unittest.TestCase):
    def args(self, **changes):
        values = dict(desktop_id="desk-exact", cdp_url="http://127.0.0.1:9222", domain=["jd.com"], confirm_domains=False,
                      confirm_desktop_switch=False, with_storage=False, wait_seconds=0, timeout_seconds=90)
        values.update(changes)
        return argparse.Namespace(**values)

    def test_cli_preserves_current_gateway_prefix_and_exposes_only_receipt(self):
        args = cua.build_parser().parse_args(["--api-base-url", "https://gateway.test/pre", "browser-sync", "--desktop-id", "desk-exact", "--cdp-url", "http://127.0.0.1:9222", "--domain", "jd.com"])
        with patch.object(cua.cua_auth, "authorized_call", side_effect=[configuration(), configuration("2026-10-26T12:00:00Z")]) as api, \
                patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True}) as chrome:
            result = cua.cmd_browser_sync(args, object(), object())
            self.assertTrue(result["data"]["uploaded"])
            self.assertTrue(all(call.args[1] == "https://gateway.test/pre" for call in api.call_args_list))
            self.assertTrue(all(call.args[3] == "/v1/desktops/desk-exact/browser-sync/config" for call in api.call_args_list))
            self.assertEqual(chrome.call_args.args[0]["settings"]["domains"], "jd.com")
            self.assertEqual(chrome.call_args.args[0]["settings"]["with_storage"], 0)
            self.assertFalse(result["data"]["local_storage"])
            serialized = json.dumps(result)
            for secret in [
                "private-uuid",
                FIXTURE_PASSWORD,
                FIXTURE_TOKEN,
                "settings",
                "headers",
            ]:
                self.assertNotIn(secret, serialized)

    def test_missing_connection_is_created_once_and_uncertain_create_is_read_back(self):
        for code in ["NETWORK", "cookiecloud_disconnect_first"]:
            calls = []
            def api(method, path, body):
                calls.append((method, body))
                if len(calls) == 1: raise SkillError("cookiecloud_not_connected", "not connected")
                if method == "POST": raise SkillError(code, "uncertain create")
                return configuration("2026-10-26T12:00:00Z" if len(calls) > 3 else None)
            with self.subTest(code=code), patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True}):
                result = sync.synchronize(self.args(), api)
            self.assertTrue(result["data"]["uploaded"])
            self.assertEqual([method for method, _ in calls], ["GET", "POST", "GET", "GET"])
            self.assertEqual(calls[1][1], {})

    def test_invalid_configuration_fails_before_any_chrome_write(self):
        cases = []
        for key in [
            "password",
            "headers",
        ]:
            invalid = configuration(); invalid["settings"].pop(key)
            cases.append((self.args(), invalid))
        invalid = configuration(); invalid["extension_id"] = "another-extension"
        cases.append((self.args(), invalid))
        for args, data in cases:
            with self.subTest(data=data["extension_id"]), patch.object(sync, "_configure_extension") as chrome:
                with self.assertRaises(SkillError): sync.synchronize(args, lambda *unused: data)
                chrome.assert_not_called()

    def test_new_websites_use_local_selection_without_a_server_domain_list(self):
        for legacy in (False, True):
            initial, later = configuration(), configuration("2026-10-29T12:00:00Z")
            if legacy:
                initial["settings"]["domains"] = later["settings"]["domains"] = "unselected.test"
            replies = iter([initial, later])
            with self.subTest(legacy=legacy), patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True}) as chrome:
                result = sync.synchronize(self.args(domain=["new.test"]), lambda *unused: next(replies))
            self.assertTrue(result["data"]["uploaded"])
            self.assertEqual(chrome.call_args.args[0]["settings"]["domains"], "new.test")

    def test_switch_or_narrowing_returns_confirmation_without_upload(self):
        for flags, flag in [({"other_desktop_configured": True}, "--confirm-desktop-switch"), ({"local_scope_all_websites": True}, "--confirm-domains")]:
            with self.subTest(flag=flag), patch.object(sync, "_configure_extension", return_value={"confirmation_required": True, **flags}) as chrome:
                calls = []
                result = sync.synchronize(self.args(), lambda *args: (calls.append(args) or configuration()))
                self.assertEqual(result["data"]["status"], "user_action_required")
                self.assertEqual(result["data"]["reason"], "confirmation_required")
                self.assertFalse(result["data"]["configured"])
                self.assertIn(flag, result["next"]["agent_hint"])
                self.assertEqual(len(calls), 1)
                self.assertFalse(chrome.call_args.args[0]["confirm_desktop_switch"])

    def test_local_storage_is_opt_in_and_older_gateway_value_is_accepted(self):
        for gateway_value, opt_in in [(1, False), (0, True)]:
            initial = configuration(); initial["settings"]["with_storage"] = gateway_value
            replies = iter([initial, configuration("2026-11-09T12:00:00Z")])
            with self.subTest(gateway=gateway_value, opt_in=opt_in), \
                    patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True, "domains": ["jd.com"]}) as chrome:
                result = sync.synchronize(self.args(with_storage=opt_in), lambda *unused: next(replies))
            self.assertEqual(chrome.call_args.args[0]["settings"]["with_storage"], 1 if opt_in else 0)
            self.assertEqual(result["data"]["local_storage"], opt_in)
        replies = iter([configuration(), configuration("2026-11-09T12:00:00Z")])
        with patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True,
                                                                       "domains": ["jd.com"], "local_storage_turned_off": True}):
            result = sync.synchronize(self.args(), lambda *unused: next(replies))
        self.assertTrue(result["data"]["local_storage_turned_off"])
        self.assertIn("--with-storage", result["next"]["agent_hint"])
        invalid = configuration(); invalid["settings"]["with_storage"] = 2
        with patch.object(sync, "_configure_extension") as chrome, self.assertRaises(SkillError):
            sync.synchronize(self.args(), lambda *unused: invalid)
        chrome.assert_not_called()

    def test_saved_settings_do_not_imply_upload_and_changed_connection_is_not_a_receipt(self):
        for rotated in [False, True]:
            later = configuration("2026-10-26T12:00:01Z" if rotated else "2026-10-26T12:00:00Z")
            if rotated: later["settings"]["uuid"] = "another-private-uuid"
            values = iter([configuration("2026-10-26T12:00:00Z"), later])
            with patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True}):
                result = sync.synchronize(self.args(), lambda *unused: next(values))
                self.assertEqual(result["data"]["status"], "configured")
                self.assertFalse(result["data"]["uploaded"])
        with patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True}):
            def unavailable_receipt(method, path, body):
                if not getattr(unavailable_receipt, "read", False):
                    unavailable_receipt.read = True
                    return configuration()
                raise SkillError("NETWORK", "receipt temporarily unavailable")
            result = sync.synchronize(self.args(), unavailable_receipt)
            self.assertEqual(result["data"]["status"], "configured")
            self.assertFalse(result["data"]["uploaded"])

    def test_api_errors_and_browser_failures_cannot_emit_private_payloads(self):
        with patch.object(sync, "_configure_extension") as chrome:
            def api(*unused): raise SkillError("Unknown-" + FIXTURE_PASSWORD, FIXTURE_PASSWORD, reason=FIXTURE_TOKEN)
            with self.assertRaises(SkillError) as failure: sync.synchronize(self.args(), api)
            serialized = json.dumps({"message": str(failure.exception), "extra": failure.exception.extra})
            self.assertNotIn(FIXTURE_PASSWORD, serialized)
            self.assertNotIn(FIXTURE_TOKEN, serialized)
            chrome.assert_not_called()
        for url in ["http://remote.test:9222", f"ws://user:{FIXTURE_PASSWORD}@localhost:9222/devtools/browser/x"]:
            with self.assertRaises(SkillError): sync.validate_cdp_url(url)


def status(**changes):
    value = {"desktop_id": "desk-exact", "connected": True, "received_at": "2026-11-09T10:00:00Z", "import_pending": False,
             "last_rejection": None, "sites": [{"domain": "jd.com", "status": "succeeded", "last_synced_at": "2026-11-09T10:00:10Z", "updated_at": "2026-11-09T10:00:10Z"}]}
    value.update(changes)
    return value


class BrowserSyncStatusTests(unittest.TestCase):
    """After setup, browser-sync reports results without a Chrome debugging connection."""

    def args(self, **changes):
        values = dict(desktop_id="desk-exact", cdp_url=None, domain=["jd.com"], confirm_domains=False,
                      confirm_desktop_switch=False, with_storage=False, wait_seconds=0, timeout_seconds=90)
        values.update(changes)
        return argparse.Namespace(**values)

    def run_status(self, replies, **changes):
        calls = []
        replies = iter(replies)
        def api(method, path, body):
            calls.append((method, path))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply
        with patch.object(sync, "_configure_extension", side_effect=AssertionError("Chrome must not be used")):
            result = sync.synchronize(self.args(**changes), api)
        self.assertTrue(all(call == ("GET", "/v1/desktops/desk-exact/browser-sync/status") for call in calls))
        return result, calls

    def test_imported_websites_are_reported_without_chrome(self):
        result, calls = self.run_status([status()], domain=["jd.com", "www.jd.com"])
        self.assertEqual(result["data"]["status"], "synced")
        self.assertEqual([site["state"] for site in result["data"]["sites"]], ["synced", "synced"])
        self.assertEqual(len(calls), 1)
        self.assertIn("verify login", result["next"]["agent_hint"])

    def test_setup_or_an_older_gateway_still_needs_the_chrome_connection(self):
        for reply, reason in [(status(connected=False, received_at=None, sites=[]), "setup_required"),
                              (status(received_at=None, sites=[]), "setup_required"),
                              (SkillError("INTERNAL", "Unexpected non-JSON gateway response (HTTP 404)"), "chrome_connection_required")]:
            with self.subTest(reason=reason):
                result, _ = self.run_status([reply])
                self.assertEqual((result["data"]["status"], result["data"]["reason"]), ("user_action_required", reason))
                self.assertIn("--cdp-url", result["next"]["agent_hint"])

    def test_missing_failed_and_rejected_results_guide_the_local_extension(self):
        failed = status(sites=[{"domain": "jd.com", "status": "failed", "last_synced_at": None, "error_code": "cookiecloud_browser_unavailable"}])
        rejected = status(received_at=None, sites=[], last_rejection={"code": "cookiecloud_payload_too_large", "at": "2026-11-09T10:05:00Z"})
        for reply, domain, reason, text in [(status(), "volcengine.com", "website_not_received", "同步域名关键词"),
                                            (failed, "jd.com", "import_failed", "error_code"),
                                            (rejected, "volcengine.com", "upload_rejected", "Local Storage")]:
            with self.subTest(reason=reason):
                result, _ = self.run_status([reply], domain=[domain])
                self.assertEqual(result["data"]["reason"], reason)
                self.assertTrue(result["data"]["configured"])
                self.assertIn(text, result["next"]["agent_hint"])
                self.assertIn("手动同步", result["next"]["agent_hint"])
        result, _ = self.run_status([failed], domain=["jd.com"])
        self.assertEqual(result["data"]["sites"][0]["error_code"], "cookiecloud_browser_unavailable")

    def test_wait_ends_on_an_imported_upload_or_a_rejection_and_never_uploads(self):
        clock = [0.0]
        with patch.object(sync, "_sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(sync, "_clock", side_effect=lambda: clock[0]):
            newer = status(received_at="2026-11-09T10:10:00Z", sites=[{"domain": "jd.com", "status": "succeeded", "last_synced_at": "2026-11-09T10:10:05Z"}])
            result, calls = self.run_status([status(), status(), status(received_at="2026-11-09T10:10:00Z", import_pending=True), newer], wait_seconds=60)
            self.assertEqual((result["data"]["status"], result["data"]["waited"]), ("synced", "received"))
            self.assertEqual(result["data"]["sites"][0]["last_synced_at"], "2026-11-09T10:10:05Z")
            self.assertEqual(len(calls), 4)
            clock[0] = 0.0
            rejection = {"code": "cookiecloud_payload_too_large", "at": "2026-11-09T10:11:00Z"}
            result, _ = self.run_status([status(), status(last_rejection=rejection)], wait_seconds=60)
            self.assertEqual((result["data"]["reason"], result["data"]["waited"]), ("upload_rejected", "rejected"))
            clock[0] = 0.0
            result, calls = self.run_status([status(), SkillError("NETWORK", "temporary"), newer], wait_seconds=60)
            self.assertEqual((result["data"]["status"], result["data"]["waited"]), ("synced", "received"))
            self.assertEqual(len(calls), 3)
            clock[0] = 0.0
            with self.assertRaises(SkillError) as failure:
                self.run_status([status(), SkillError("AUTH_REQUIRED", "login")], wait_seconds=60)
            self.assertEqual(failure.exception.code, "AUTH_REQUIRED")
            clock[0] = 0.0
            result, calls = self.run_status([status()] * 20, wait_seconds=12)
            self.assertEqual((result["data"]["status"], result["data"]["waited"]), ("synced", "timeout"))
            self.assertIn("No newer upload arrived while waiting; these results are from the latest upload at 2026-11-09T10:00:00Z", result["next"]["agent_hint"])
            self.assertEqual(len(calls), 4)

    def test_storage_changes_and_invalid_requests_do_not_reach_chrome(self):
        result, calls = self.run_status([], with_storage=True)
        self.assertEqual(result["data"]["reason"], "storage_change")
        self.assertEqual(calls, [])
        for changes in [{"wait_seconds": -1}, {"wait_seconds": 301}]:
            with self.subTest(changes=changes), self.assertRaises(SkillError) as failure:
                self.run_status([status()], **changes)
            self.assertEqual(failure.exception.code, "VALIDATION_ERROR")
        for reply in [status(desktop_id="another"), status(connected="yes"), status(last_rejection={"code": "private detail"})]:
            with self.subTest(reply=reply), self.assertRaises(SkillError) as failure:
                self.run_status([reply])
            self.assertEqual(failure.exception.code, "COOKIECLOUD_API_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
