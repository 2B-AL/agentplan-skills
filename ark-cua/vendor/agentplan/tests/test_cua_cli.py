import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import cua  # noqa: E402


class FakeSession:
    def __init__(self):
        self.last_invocation_id = None

    def set_last_invocation_id(self, value):
        self.last_invocation_id = value


class CuaWaitBudgetTests(unittest.TestCase):
    def test_delegate_rejects_negative_budget_before_creating_task(self):
        with (
            mock.patch.object(cua, "resolve_base_url", return_value="http://gateway"),
            mock.patch.object(cua.cua_auth, "authorized_call") as call,
            self.assertRaises(cua.SkillError) as ctx,
        ):
            cua.cmd_delegate(
                Namespace(objective="test", wait_ms=-1),
                state=object(),
                session=FakeSession(),
            )

        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")
        call.assert_not_called()

    def test_watch_splits_total_budget_into_server_sized_chunks(self):
        responses = [
            {"invocation_id": "task-1", "outcome": "in_progress"},
            {"invocation_id": "task-1", "outcome": "in_progress"},
            {"invocation_id": "task-1", "outcome": "completed"},
        ]
        session = FakeSession()
        with (
            mock.patch.object(cua, "resolve_base_url", return_value="http://gateway"),
            mock.patch.object(cua.cua_auth, "authorized_call", side_effect=responses) as call,
        ):
            result = cua.cmd_watch(
                Namespace(invocation_id="task-1", last=False, wait_ms=125000),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["outcome"], "completed")
        self.assertEqual([item.kwargs["body"]["wait_ms"] for item in call.call_args_list], [60000, 60000, 5000])
        self.assertEqual([item.kwargs["timeout"] for item in call.call_args_list], [90, 90, 35])

    def test_zero_budget_checks_without_server_long_poll(self):
        session = FakeSession()
        with (
            mock.patch.object(cua, "resolve_base_url", return_value="http://gateway"),
            mock.patch.object(
                cua.cua_auth,
                "authorized_call",
                return_value={"invocation_id": "task-1", "outcome": "in_progress"},
            ) as call,
        ):
            cua.cmd_watch(
                Namespace(invocation_id="task-1", last=False, wait_ms=0),
                state=object(),
                session=session,
            )

        self.assertEqual(call.call_args.args[2:4], ("GET", "/v1/invocations/task-1"))

    def test_delegate_creates_once_then_uses_watch_budget(self):
        responses = [
            {"invocation_id": "task-1", "outcome": "in_progress"},
            {"invocation_id": "task-1", "outcome": "completed"},
        ]
        session = FakeSession()
        with (
            mock.patch.object(cua, "resolve_base_url", return_value="http://gateway"),
            mock.patch.object(cua.cua_auth, "authorized_call", side_effect=responses) as call,
        ):
            result = cua.cmd_delegate(
                Namespace(objective="test", wait_ms=900000),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["outcome"], "completed")
        self.assertEqual(call.call_args_list[0].args[2:4], ("POST", "/v1/invocations"))
        self.assertEqual(call.call_args_list[0].kwargs["body"]["wait_ms"], 0)
        self.assertEqual(call.call_args_list[1].args[2:4], ("POST", "/v1/invocations/task-1/watch"))
        self.assertEqual(call.call_args_list[1].kwargs["body"]["wait_ms"], 60000)


class CuaDesktopURLTests(unittest.TestCase):
    def test_legacy_bare_url_derives_full_interface(self):
        access_url = "https://desktop.example/win10-spice-1?ticket=dt_1"

        desktop_url, full_url = cua._derive_desktop_urls(access_url)

        self.assertEqual(desktop_url, access_url)
        self.assertEqual(
            full_url,
            "https://desktop.example/cua-app/win10-spice-1?ticket=dt_1",
        )

    def test_legacy_full_interface_url_recovers_bare_view(self):
        access_url = "https://desktop.example/cua-app/win10-spice-1?ticket=dt_1"

        desktop_url, full_url = cua._derive_desktop_urls(access_url)

        self.assertEqual(
            desktop_url,
            "https://desktop.example/win10-spice-1?ticket=dt_1",
        )
        self.assertEqual(full_url, access_url)

    def test_namespaced_app_url_is_already_the_full_interface(self):
        access_url = (
            "https://desktop.example/desktops/desk-1/cua-app/"
            "?ticket=dt_1#timeline"
        )

        desktop_url, full_url = cua._derive_desktop_urls(access_url)

        self.assertIsNone(desktop_url)
        self.assertEqual(full_url, access_url)
        self.assertNotIn("/cua-app/desktops/", full_url)

    def test_namespaced_desktop_view_derives_sibling_app_route(self):
        access_url = (
            "https://desktop.example/desktops/desk-1/connect/spice"
            "?ticket=dt_1"
        )

        desktop_url, full_url = cua._derive_desktop_urls(access_url)

        self.assertEqual(desktop_url, access_url)
        self.assertEqual(
            full_url,
            "https://desktop.example/desktops/desk-1/cua-app/?ticket=dt_1",
        )


if __name__ == "__main__":
    unittest.main()
