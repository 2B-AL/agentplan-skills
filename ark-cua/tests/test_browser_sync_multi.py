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

"""One connection, several desktops: joining without Chrome, leaving and revoking."""

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cua  # noqa: E402
import cua_cookiecloud as sync  # noqa: E402
from cua_state import SessionState  # noqa: E402
from cua_util import SkillError  # noqa: E402

FIXTURE_PASSWORD = secrets.token_urlsafe(24)
FIXTURE_TOKEN = secrets.token_urlsafe(24)
# Placeholder desktop ids, built at run time like the fixture secrets.
A, B, C, X, W, OFF = ("desk-" + name for name in ("a", "b", "c", "x", "w", "off"))


def status(desktop_id, **changes):
    value = {"desktop_id": desktop_id, "connected": True, "received_at": "2026-11-10T10:00:00Z", "import_pending": False,
             "last_rejection": None, "connection_id": "bsc_shared", "target_status": "imported",
             "sites": [{"domain": "jd.com", "status": "succeeded", "last_synced_at": "2026-11-10T10:00:10Z"}],
             "targets": [{"desktop_id": A, "status": "imported", "last_import_at": "2026-11-10T10:00:10Z"},
                         {"desktop_id": B, "status": "imported", "last_import_at": "2026-11-10T10:00:12Z"}]}
    value.update(changes)
    return value


def configuration(desktop_id, connection="bsc_shared", uuid="shared-uuid", received=None):
    return {"desktop_id": desktop_id, "extension_id": sync.EXTENSION_ID, "storage_key": sync.STORAGE_KEY,
            "received_at": received, "connection_id": connection,
            "settings": {"endpoint": "https://receiver.test/api/cookiecloud/" + connection, "uuid": uuid, "password": FIXTURE_PASSWORD,
                         "headers": "X-CookieCloud-Token:" + FIXTURE_TOKEN, "type": "up", "with_storage": 0, "interval": 10, "crypto_type": "legacy"}}


class Memory:
    """The connection id this computer's extension uploads to, as cua._ExtensionRecord keeps it."""

    def __init__(self, connection=None):
        self.record, self.remembered = connection, []

    def get(self):
        return self.record

    def remember(self, connection):
        self.remembered.append(connection)
        self.record = connection


def saved_record(test, connection):
    """A real record of this computer, kept in a temporary session file."""
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    record = cua._ExtensionRecord(SessionState(Path(directory.name) / "session.json", {}), "https://gateway.test")
    record.remember(connection)
    return record


class Api:
    """Answers each (method, path) from a queue and records the calls."""

    def __init__(self, replies):
        self.replies = {key: list(values) for key, values in replies.items()}
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        reply = self.replies[(method, path)].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def path(desktop_id, suffix=""):
    return "/v1/desktops/" + desktop_id + "/browser-sync" + suffix


class MultiDesktopCheckTests(unittest.TestCase):
    def args(self, desktops, **changes):
        values = dict(desktop_id=desktops, cdp_url=None, domain=["jd.com"], confirm_domains=False, confirm_desktop_switch=False,
                      with_storage=False, wait_seconds=0, timeout_seconds=90)
        values.update(changes)
        return argparse.Namespace(**values)

    def run_check(self, desktops, replies, memory=None, **changes):
        api = Api(replies)
        with patch.object(sync, "_configure_extension", side_effect=AssertionError("Chrome must not be used")):
            result = sync.synchronize(self.args(desktops, **changes), api, memory)
        return result, api

    def test_a_new_desktop_joins_the_extension_connection_without_chrome(self):
        not_joined = status(B, connected=False, received_at=None, connection_id=None, target_status=None, sites=[], targets=[])
        result, api = self.run_check([B], {
            ("GET", path(B, "/status")): [not_joined, status(B, import_pending=True, target_status="import_pending", sites=[])],
            ("GET", path(B, "/config")): [SkillError("cookiecloud_not_connected", "not connected")],
            ("POST", path(B, "/config")): [configuration(B)],
        }, Memory("bsc_shared"))
        self.assertEqual((result["data"]["status"], result["data"]["joined"]), ("importing", True))
        self.assertEqual(result["data"]["connection_id"], "bsc_shared")
        self.assertIn("no Chrome change", result["next"]["agent_hint"])
        self.assertEqual([call[0] for call in api.calls], ["GET", "GET", "POST", "GET"])
        self.assertEqual(api.calls[2][2], {})
        self.assertNotIn(FIXTURE_PASSWORD, json.dumps(result))

    def test_a_desktop_joined_to_another_connection_is_not_switched_silently(self):
        not_joined = status(C, connected=False, received_at=None, connection_id=None, target_status=None, sites=[], targets=[])
        result, _ = self.run_check([C], {
            ("GET", path(C, "/status")): [not_joined, status(C, connection_id="bsc_other", received_at=None, target_status="waiting_upload",
                                                                    sites=[], targets=[{"desktop_id": C, "status": "waiting_upload"}])],
            ("GET", path(C, "/config")): [configuration(C, connection="bsc_other", uuid="other-uuid")],
        }, Memory("bsc_shared"))
        data = result["data"]
        self.assertEqual((data["status"], data["reason"], data["configured"], data["joined"]), ("user_action_required", "connection_changed", False, True))
        self.assertEqual((data["connection_id"], data["extension_connection_id"]), ("bsc_other", "bsc_shared"))
        self.assertIn("stops uploads to the extension's current one", result["next"]["agent_hint"])
        self.assertIn("--cdp-url", result["next"]["agent_hint"])
        self.assertNotIn(FIXTURE_PASSWORD, json.dumps(result))

    def test_a_desktop_still_in_another_connection_is_named_not_reported_synced(self):
        # Before the fix, its imports from the other connection were reported as this computer's sync.
        result, api = self.run_check([A], {("GET", path(A, "/status")): [status(A, connection_id="bsc_older")]}, Memory("bsc_shared"))
        data = result["data"]
        self.assertEqual((data["reason"], data["configured"], data["joined"], data["connection_id"], data["extension_connection_id"]),
                         ("connection_changed", False, False, "bsc_older", "bsc_shared"))
        self.assertIn("browser-sync-leave", result["next"]["agent_hint"])
        self.assertEqual(len(api.calls), 1)

    def test_without_a_record_an_unjoined_desktop_still_needs_setup_and_is_not_joined(self):
        not_joined = status(B, connected=False, received_at=None, connection_id=None, target_status=None, sites=[], targets=[])
        for memory in (None, Memory()):
            with self.subTest(memory=memory):
                result, api = self.run_check([B], {("GET", path(B, "/status")): [not_joined]}, memory)
                self.assertEqual(result["data"]["reason"], "setup_required")
                self.assertEqual(len(api.calls), 1)

    def test_a_held_connection_without_any_upload_waits_for_a_manual_sync(self):
        waiting = status(A, received_at=None, target_status="waiting_upload", sites=[])
        result, _ = self.run_check([A], {("GET", path(A, "/status")): [waiting]}, Memory("bsc_shared"))
        self.assertEqual((result["data"]["reason"], result["data"]["configured"]), ("waiting_upload", True))
        self.assertIn("手动同步", result["next"]["agent_hint"])
        # Without the record, the same reply still asks for the first setup.
        result, _ = self.run_check([A], {("GET", path(A, "/status")): [waiting]})
        self.assertEqual(result["data"]["reason"], "setup_required")

    def test_several_desktops_report_each_and_the_least_finished_overall(self):
        result, api = self.run_check([A, B, A], {
            ("GET", path(A, "/status")): [status(A)],
            ("GET", path(B, "/status")): [status(B, import_pending=True, target_status="import_pending")],
        })
        data = result["data"]
        self.assertEqual(data["desktop_ids"], [A, B])
        self.assertEqual([item["status"] for item in data["desktops"]], ["synced", "importing"])
        self.assertEqual((data["status"], data["connection_id"], data["different_connections"]), ("importing", "bsc_shared", False))
        self.assertIn(A + ": ", result["next"]["agent_hint"])
        self.assertIn(B + ": ", result["next"]["agent_hint"])
        self.assertEqual(len(api.calls), 2)

    def test_one_unavailable_desktop_does_not_hide_the_others(self):
        result, _ = self.run_check([A, B], {
            ("GET", path(A, "/status")): [status(A)],
            ("GET", path(B, "/status")): [SkillError("cookiecloud_unavailable", "busy")],
        })
        self.assertEqual([(item["status"], item.get("reason")) for item in result["data"]["desktops"]],
                         [("synced", None), ("user_action_required", "unavailable")])
        self.assertEqual(result["data"]["desktops"][1]["error_code"], "cookiecloud_unavailable")
        self.assertIn("Retry once in a minute", result["next"]["agent_hint"])
        # A transient failure asks for a later rerun, not for a Gateway or CookieCloud change.
        result, _ = self.run_check([A, B], {
            ("GET", path(A, "/status")): [status(A)],
            ("GET", path(B, "/status")): [SkillError("NETWORK", "temporary")],
        })
        self.assertEqual(result["data"]["desktops"][1]["error_code"], "NETWORK")
        self.assertIn(B + ": This desktop could not be read now", result["next"]["agent_hint"])
        self.assertNotIn("version", result["next"]["agent_hint"])
        with self.assertRaises(SkillError) as failure:
            self.run_check([A, B], {
                ("GET", path(A, "/status")): [status(A)],
                ("GET", path(B, "/status")): [SkillError("AUTH_REQUIRED", "login")],
            })
        self.assertEqual(failure.exception.code, "AUTH_REQUIRED")

    def test_an_upload_seen_while_waiting_for_one_desktop_counts_for_the_others(self):
        # Before the fix, desk-b was read after desk-a's wait, took the new upload
        # as its starting point and waited out the whole time for another one.
        clock, calls = [0.0], []
        newer = dict(received_at="2026-11-10T10:10:00Z", sites=[{"domain": "jd.com", "status": "succeeded", "last_synced_at": "2026-11-10T10:10:05Z"}])

        def api(method, url, body):
            calls.append(url)
            desktop_id = url.split("/")[3]
            return status(desktop_id, **newer) if clock[0] >= 5 else status(desktop_id)

        with patch.object(sync, "_sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(sync, "_clock", side_effect=lambda: clock[0]), \
                patch.object(sync, "_configure_extension", side_effect=AssertionError("Chrome must not be used")):
            result = sync.synchronize(self.args([A, B], wait_seconds=60), api, None)
        self.assertEqual([(item["status"], item["waited"]) for item in result["data"]["desktops"]], [("synced", "received")] * 2)
        self.assertEqual([item["sites"][0]["last_synced_at"] for item in result["data"]["desktops"]], ["2026-11-10T10:10:05Z"] * 2)
        self.assertEqual((clock[0], len(calls)), (5, 4))

    def test_several_desktops_without_a_new_upload_wait_once_in_all(self):
        clock = [0.0]
        with patch.object(sync, "_sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(sync, "_clock", side_effect=lambda: clock[0]):
            result, api = self.run_check([A, B], {("GET", path(A, "/status")): [status(A)] * 5,
                                                                ("GET", path(B, "/status")): [status(B)] * 5}, wait_seconds=12)
        self.assertEqual([(item["status"], item["waited"]) for item in result["data"]["desktops"]], [("synced", "timeout")] * 2)
        self.assertEqual((clock[0], len(api.calls)), (12, 8))
        self.assertIn("No newer upload arrived while waiting; these results are from the latest upload at 2026-11-10T10:00:00Z",
                      result["next"]["agent_hint"])

    def test_a_desktop_that_leaves_while_waiting_keeps_its_last_result(self):
        left = status(A, connected=False, received_at=None, connection_id=None, target_status=None, targets=[])
        clock = [0.0]
        with patch.object(sync, "_sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(sync, "_clock", side_effect=lambda: clock[0]):
            result, api = self.run_check([A], {("GET", path(A, "/status")): [status(A), left]}, wait_seconds=60)
        self.assertEqual((result["data"]["status"], result["data"]["waited"], result["data"]["received_at"]), ("synced", "timeout", "2026-11-10T10:00:00Z"))
        self.assertEqual(len(api.calls), 2)

    def test_desktops_of_different_connections_are_named(self):
        result, _ = self.run_check([A, X], {
            ("GET", path(A, "/status")): [status(A)],
            ("GET", path(X, "/status")): [status(X, connection_id="bsc_other", targets=[])],
        })
        self.assertTrue(result["data"]["different_connections"])
        self.assertEqual(result["data"]["connection_ids"], ["bsc_other", "bsc_shared"])
        self.assertIn("different browser sync connections", result["next"]["agent_hint"])

    def test_members_waiting_for_authorization_are_named_not_read(self):
        waiting = [{"desktop_id": A, "status": "imported", "last_import_at": None},
                   {"desktop_id": W, "status": "waiting_authorization", "last_import_at": None}]
        result, api = self.run_check([A], {("GET", path(A, "/status")): [status(A, targets=waiting)]})
        self.assertEqual((result["data"]["status"], len(api.calls)), ("synced", 1))
        self.assertIn(W + " of this connection wait for an authorization refresh", result["next"]["agent_hint"])
        # A desktop that was checked is not named: reading its status refreshed it.
        result, _ = self.run_check([A, W], {("GET", path(A, "/status")): [status(A, targets=waiting)],
                                                          ("GET", path(W, "/status")): [status(W, targets=waiting)]})
        self.assertNotIn("authorization refresh", result["next"]["agent_hint"])

    def test_unexpected_status_field_types_are_ignored(self):
        odd = status(A, target_status=["imported"], targets=[{"desktop_id": B, "status": {"state": "imported"}},
                                                                     {"desktop_id": A, "status": "imported"}])
        result, _ = self.run_check([A], {("GET", path(A, "/status")): [odd]})
        self.assertEqual((result["data"]["status"], result["data"]["target_status"]), ("synced", None))
        self.assertEqual([target["desktop_id"] for target in result["data"]["targets"]], [A])

    def test_invalid_desktop_selection_is_refused_before_any_call(self):
        for desktops in [[], ["desk a"], ["../desk"], ["d%d" % index for index in range(21)]]:
            with self.subTest(desktops=desktops), self.assertRaises(SkillError) as failure:
                self.run_check(desktops, {})
            self.assertEqual(failure.exception.code, "VALIDATION_ERROR")


class MultiDesktopSetupTests(unittest.TestCase):
    def args(self, desktops, **changes):
        values = dict(desktop_id=desktops, cdp_url="http://127.0.0.1:9222", domain=["jd.com"], confirm_domains=False,
                      confirm_desktop_switch=False, with_storage=False, wait_seconds=0, timeout_seconds=90)
        values.update(changes)
        return argparse.Namespace(**values)

    def test_desktops_of_one_connection_join_and_the_extension_is_written_once(self):
        api = Api({
            ("GET", path(A, "/config")): [configuration(A), configuration(A, received="2026-11-10T11:00:00Z")],
            ("GET", path(B, "/config")): [SkillError("cookiecloud_not_connected", "not connected")],
            ("POST", path(B, "/config")): [configuration(B)],
        })
        memory = Memory()
        with patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True,
                                                                       "domains": ["jd.com", "xiaohongshu.com"]}) as chrome:
            result = sync.synchronize(self.args([A, B]), api, memory)
        chrome.assert_called_once()
        self.assertEqual(result["data"]["status"], "uploaded")
        self.assertEqual((result["data"]["desktop_ids"], result["data"]["connection_id"]), ([A, B], "bsc_shared"))
        self.assertEqual(memory.remembered, ["bsc_shared"])
        self.assertNotIn(FIXTURE_PASSWORD, json.dumps(result))

    def test_desktops_of_different_connections_never_reach_chrome(self):
        api = Api({
            ("GET", path(A, "/config")): [configuration(A)],
            ("GET", path(X, "/config")): [configuration(X, connection="bsc_other", uuid="other-uuid")],
        })
        memory = Memory()
        with patch.object(sync, "_configure_extension") as chrome:
            result = sync.synchronize(self.args([A, X]), api, memory)
        chrome.assert_not_called()
        self.assertEqual((result["data"]["reason"], result["data"]["desktop_ids"]), ("different_connections", [A, X]))
        self.assertEqual([item["connection_id"] for item in result["data"]["desktops"]], ["bsc_shared", "bsc_other"])
        self.assertIn("browser-sync-leave", result["next"]["agent_hint"])
        self.assertEqual(memory.remembered, [])
        self.assertNotIn(FIXTURE_PASSWORD, json.dumps(result))

    def test_an_older_gateway_joins_only_the_first_of_several_desktops(self):
        old = configuration(A); old.pop("connection_id")
        api = Api({("GET", path(A, "/config")): [old]})
        with patch.object(sync, "_configure_extension") as chrome:
            result = sync.synchronize(self.args([A, B]), api, Memory())
        chrome.assert_not_called()
        self.assertEqual((result["data"]["reason"], result["data"]["desktop_ids"]), ("different_connections", [A, B]))
        self.assertEqual(api.calls, [("GET", path(A, "/config"), None)])
        self.assertIn("separate browser sync connection for each desktop", result["next"]["agent_hint"])

    def test_an_older_gateway_without_connection_ids_is_not_recorded(self):
        old = configuration(A); old.pop("connection_id")
        api = Api({("GET", path(A, "/config")): [old, old]})
        memory = Memory()
        with patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": False}):
            result = sync.synchronize(self.args([A]), api, memory)
        self.assertEqual((result["data"]["status"], result["data"]["connection_id"]), ("configured", None))
        self.assertEqual(memory.remembered, [])

    def test_a_declined_switch_keeps_the_record(self):
        api = Api({("GET", path(A, "/config")): [configuration(A)]})
        memory = Memory("bsc_old")
        with patch.object(sync, "_configure_extension", return_value={"confirmation_required": True, "other_desktop_configured": True}):
            result = sync.synchronize(self.args([A]), api, memory)
        self.assertEqual((result["data"]["reason"], result["data"]["desktop_ids"]), ("confirmation_required", [A]))
        self.assertIn("another connection", result["next"]["agent_hint"])
        self.assertEqual((memory.remembered, memory.record), ([], "bsc_old"))


class AllDesktopsTests(unittest.TestCase):
    """--all-desktops uses every ready desktop the login lists, instead of named ones."""

    def args(self, **changes):
        values = dict(desktop_id=None, all_desktops=True, cdp_url=None, domain=["jd.com"], confirm_domains=False,
                      confirm_desktop_switch=False, with_storage=False, wait_seconds=0, timeout_seconds=90)
        values.update(changes)
        return argparse.Namespace(**values)

    def options(self, *items):
        return {"items": [{"id": desktop_id, "name": desktop_id, "ready": ready, "vm_state": "running" if ready else "stopped"} for desktop_id, ready in items],
                "active_desktop_id": items[0][0] if items else None}

    def test_ready_desktops_are_checked_and_the_others_are_named(self):
        api = Api({("GET", "/v1/desktop-options"): [self.options((A, True), (B, True), (OFF, False))],
                   ("GET", path(A, "/status")): [status(A)],
                   ("GET", path(B, "/status")): [status(B)]})
        with patch.object(sync, "_configure_extension", side_effect=AssertionError("Chrome must not be used")):
            result = sync.synchronize(self.args(), api, Memory("bsc_shared"))
        data = result["data"]
        self.assertEqual((data["status"], data["desktop_ids"], data["all_desktops"], data["skipped_desktops"]),
                         ("synced", [A, B], True, [OFF]))
        self.assertIn(OFF + " are not ready", result["next"]["agent_hint"])
        self.assertNotIn(("GET", path(OFF, "/status"), None), api.calls)

    def test_setup_writes_the_extension_once_for_every_ready_desktop(self):
        api = Api({("GET", "/v1/desktop-options"): [self.options((A, True), (B, True))],
                   ("GET", path(A, "/config")): [configuration(A), configuration(A, received="2026-11-10T11:00:00Z")],
                   ("GET", path(B, "/config")): [configuration(B)]})
        with patch.object(sync, "_configure_extension", return_value={"configured": True, "readback_verified": True, "upload_attempted": True}) as chrome:
            result = sync.synchronize(self.args(cdp_url="http://127.0.0.1:9222"), api, Memory())
        chrome.assert_called_once()
        self.assertEqual((result["data"]["status"], result["data"]["desktop_ids"], result["data"]["skipped_desktops"]), ("uploaded", [A, B], []))
        self.assertNotIn(FIXTURE_PASSWORD, json.dumps(result))

    def test_a_single_ready_desktop_keeps_the_one_desktop_result_and_names_it(self):
        api = Api({("GET", "/v1/desktop-options"): [self.options((A, True))],
                   ("GET", path(A, "/status")): [status(A)]})
        result = sync.synchronize(self.args(), api, None)
        self.assertEqual((result["data"]["desktop_id"], result["data"]["desktop_ids"], result["data"]["status"]), (A, [A], "synced"))

    def test_no_ready_desktop_or_a_mixed_selection_does_nothing(self):
        api = Api({("GET", "/v1/desktop-options"): [self.options((OFF, False))]})
        result = sync.synchronize(self.args(), api, Memory("bsc_shared"))
        self.assertEqual((result["data"]["reason"], result["data"]["skipped_desktops"]), ("no_ready_desktops", [OFF]))
        self.assertEqual(len(api.calls), 1)
        for changes in [{"desktop_id": [A]}, {"all_desktops": False}]:
            with self.subTest(changes=changes), self.assertRaises(SkillError) as failure:
                sync.synchronize(self.args(**changes), Api({}), None)
            self.assertEqual(failure.exception.code, "VALIDATION_ERROR")
        too_many = Api({("GET", "/v1/desktop-options"): [self.options(*[("d%d" % index, True) for index in range(21)])]})
        with self.assertRaises(SkillError) as failure:
            sync.synchronize(self.args(), too_many, None)
        self.assertEqual(failure.exception.code, "VALIDATION_ERROR")

    def test_the_flag_parses_without_desktop_ids(self):
        args = cua.build_parser().parse_args(["browser-sync", "--all-desktops", "--domain", "jd.com"])
        self.assertEqual((args.all_desktops, args.desktop_id), (True, None))


class LeaveAndRevokeTests(unittest.TestCase):
    def args(self, **values):
        return argparse.Namespace(desktop_id=B, **values)

    def test_leave_removes_only_this_desktop(self):
        api = Api({("DELETE", path(B)): [status(B, connected=False, connection_id=None, target_status=None, targets=[])]})
        result = sync.leave(self.args(), api)
        self.assertEqual((result["data"]["status"], result["data"]["connected"]), ("left", False))
        self.assertEqual(api.calls, [("DELETE", path(B), None)])
        self.assertIn("other desktops keep receiving", result["next"]["agent_hint"])
        api = Api({("DELETE", path(B)): [SkillError("cookiecloud_not_connected", "not connected")]})
        self.assertEqual(sync.leave(self.args(), api)["data"]["status"], "left")
        api = Api({("DELETE", path(B)): [SkillError("NotFound", "older gateway")]})
        with self.assertRaises(SkillError) as failure:
            sync.leave(self.args(), api)
        self.assertEqual(failure.exception.code, "COOKIECLOUD_API_UNAVAILABLE")
        # A desktop busy importing refuses for now; the code says to retry, not that the Gateway lacks the feature.
        for code, text in [("cookiecloud_unavailable", "Retry once in a minute"), ("cookiecloud_connection_changed", "report its code")]:
            api = Api({("DELETE", path(B)): [SkillError(code, "refused", upstream_status=409)]})
            with self.subTest(code=code), self.assertRaises(SkillError) as failure:
                sync.leave(self.args(), api)
            self.assertEqual(failure.exception.code, code)
            self.assertIn(text, str(failure.exception))

    def test_revoke_ends_the_connection_and_its_record(self):
        api = Api({("GET", path(B, "/status")): [status(B)],
                   ("POST", path(B, "/revoke")): [status(B, connected=False, connection_id=None, target_status=None, targets=[])]})
        record = saved_record(self, "bsc_shared")
        result = sync.revoke(self.args(confirm=True), api, record)
        self.assertEqual((result["data"]["status"], result["data"]["connection_id"]), ("revoked", "bsc_shared"))
        self.assertEqual(api.calls[-1], ("POST", path(B, "/revoke"), {}))
        self.assertIsNone(record.get())
        # Another connection's revocation keeps this computer's record.
        api = Api({("GET", path(B, "/status")): [status(B, connection_id="bsc_other")],
                   ("POST", path(B, "/revoke")): [status(B, connected=False)]})
        record = saved_record(self, "bsc_shared")
        sync.revoke(self.args(confirm=True), api, record)
        self.assertEqual(record.get(), "bsc_shared")

    def test_revoke_needs_explicit_confirmation(self):
        api = Api({})
        with self.assertRaises(SkillError) as failure:
            sync.revoke(self.args(confirm=False), api, Memory("bsc_shared"))
        self.assertEqual((failure.exception.code, api.calls), ("VALIDATION_ERROR", []))

    def test_revoke_without_a_connection_changes_nothing(self):
        api = Api({("GET", path(B, "/status")): [status(B, connected=False, connection_id=None, targets=[])]})
        result = sync.revoke(self.args(confirm=True), api, Memory("bsc_shared"))
        self.assertEqual(result["data"]["status"], "not_connected")
        self.assertEqual(len(api.calls), 1)


class CommandAndRecordTests(unittest.TestCase):
    def test_commands_parse_and_revoke_needs_explicit_confirmation(self):
        parser = cua.build_parser()
        args = parser.parse_args(["browser-sync", "--desktop-id", A, "--desktop-id", B, "--domain", "jd.com"])
        self.assertEqual((args.handler, args.desktop_id), (cua.cmd_browser_sync, [A, B]))
        self.assertIs(parser.parse_args(["browser-sync-leave", "--desktop-id", A]).handler, cua.cmd_browser_sync_leave)
        self.assertIs(parser.parse_args(["browser-sync-revoke", "--desktop-id", A, "--confirm"]).handler, cua.cmd_browser_sync_revoke)
        with self.assertRaises(SkillError) as failure:
            parser.parse_args(["browser-sync-revoke", "--desktop-id", A])
        self.assertEqual(failure.exception.code, "VALIDATION_ERROR")

    def test_the_record_is_kept_per_gateway_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "session.json"
            store = SessionState(file, {})
            first = cua._ExtensionRecord(store, "https://gateway-a.test")
            other = cua._ExtensionRecord(store, "https://gateway-b.test")
            first.remember("bsc_a")
            self.assertEqual((first.get(), other.get()), ("bsc_a", None))
            self.assertEqual(stat.S_IMODE(os.stat(file).st_mode), 0o600)
            self.assertEqual(json.loads(file.read_text())["browser_sync_extension"], {"https://gateway-a.test": "bsc_a"})
            reloaded = cua._ExtensionRecord(SessionState(file, json.loads(file.read_text())), "https://gateway-a.test")
            self.assertEqual(reloaded.get(), "bsc_a")
            other.forget("bsc_a")
            first.forget("bsc_other")
            self.assertEqual(first.get(), "bsc_a")
            first.forget("bsc_a")
            self.assertIsNone(first.get())

    def test_cli_check_uses_the_record_of_the_current_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionState(Path(directory) / "session.json", {})
            cua._ExtensionRecord(store, "https://gateway.test/pre").remember("bsc_shared")
            args = cua.build_parser().parse_args(["--api-base-url", "https://gateway.test/pre", "browser-sync", "--desktop-id", A, "--domain", "jd.com"])
            waiting = status(A, received_at=None, target_status="waiting_upload", sites=[])
            with patch.object(cua.cua_auth, "authorized_call", return_value=waiting) as api:
                result = cua.cmd_browser_sync(args, object(), store)
            self.assertEqual(result["data"]["reason"], "waiting_upload")
            self.assertEqual(api.call_args.args[1:4], ("https://gateway.test/pre", "GET", path(A, "/status")))


if __name__ == "__main__":
    unittest.main()
