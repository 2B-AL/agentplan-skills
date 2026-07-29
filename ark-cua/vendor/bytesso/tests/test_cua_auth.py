import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import cua_auth  # noqa: E402
from cua_state import AuthState  # noqa: E402
from cua_util import SkillError  # noqa: E402


class CuaAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.auth_file = Path(self.tmpdir.name) / "auth.json"
        self.env = mock.patch.dict(
            os.environ,
            {
                "CUA_SKILL_AUTH_FILE": str(self.auth_file),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmpdir.cleanup()

    def test_auth_status_without_key_is_logged_out(self):
        state = AuthState.load()

        result = cua_auth.auth_status(state, "http://hub", "http://gateway", online=False)

        self.assertEqual(result["status"], "logged_out")
        self.assertNotIn("login_url", result)
        self.assertIn("Run retry_command", result["agent_hint"])
        self.assertIn("auth login", result["retry_command"])

    def test_missing_key_error_does_not_expose_machine_start_endpoint(self):
        state = AuthState.load()

        with self.assertRaises(SkillError) as ctx:
            cua_auth.ensure_bearer_key(state, "http://hub")

        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
        self.assertNotIn("login_url", ctx.exception.extra)
        self.assertIn("auth login", ctx.exception.extra["retry_command"])

    def test_login_stores_skill_api_key_after_desktop_access_validation(self):
        state = AuthState.load()
        access = {
            "desktop": {"id": "vm-1", "name": "desk-1"},
            "access": {"desktop_login_url": "https://desktop.example"},
        }

        with mock.patch.object(cua_auth, "_login_with_skill_auth_flow", return_value="cua_api_test"), \
                mock.patch.object(cua_auth, "gateway_tool_call", return_value=access) as call, \
                mock.patch.object(cua_auth.webbrowser, "open"):
            result = cua_auth.login(state, "http://hub", "http://gateway")

        self.assertEqual(result["status"], "logged_in")
        self.assertEqual(result["mcp_url"], "http://gateway/skill/mcp")
        self.assertEqual(state.bearer_key, "cua_api_test")
        self.assertEqual(state.credential_type, "access_hub_skill_api_key")
        self.assertEqual(state.user["desktop_id"], "vm-1")
        call.assert_called_once_with("http://gateway", "cua_api_test", "cua_get_desktop_access", {}, timeout=30)

    def test_login_still_accepts_legacy_bearer_key_from_stdin(self):
        state = AuthState.load()
        access = {"desktop": {"id": "vm-1", "name": "desk-1"}}

        with mock.patch.object(cua_auth, "_read_login_token", return_value="cua_mcp_test"), \
                mock.patch.object(cua_auth, "gateway_tool_call", return_value=access):
            result = cua_auth.login(state, "http://hub", "http://gateway", bearer_key_stdin=True)

        self.assertEqual(result["status"], "logged_in")
        self.assertEqual(state.bearer_key, "cua_mcp_test")
        self.assertEqual(state.credential_type, "access_hub_bearer_key")

    def test_login_rejects_non_access_hub_key(self):
        state = AuthState.load()

        with mock.patch.object(cua_auth, "_read_login_token", return_value="not-a-cua-key"), \
                self.assertRaises(SkillError) as ctx:
            cua_auth.login(state, "http://hub", "http://gateway", open_browser=False, bearer_key_stdin=True)

        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

    def test_skill_auth_flow_polls_until_completed(self):
        responses = [
            {
                "flow_id": "flow_1",
                "poll_token": "poll_1",
                "login_url": "http://hub/auth/sso/start",
                "poll_interval_seconds": 1,
                "expires_in": 60,
            },
            {"status": "pending"},
            {
                "status": "completed",
                "credential": {"bearer_token": "cua_api_test", "key_id": "key_1"},
            },
        ]

        with mock.patch.object(cua_auth, "_access_hub_json", side_effect=responses) as call, \
                mock.patch.object(cua_auth.time, "sleep") as sleep, \
                mock.patch.object(cua_auth.webbrowser, "open") as open_browser, \
                mock.patch.object(cua_auth, "_show_login_url"):
            token = cua_auth._login_with_skill_auth_flow("http://hub", open_browser=True)

        self.assertEqual(token, "cua_api_test")
        self.assertEqual(call.call_count, 3)
        sleep.assert_called()
        open_browser.assert_called_once_with("http://hub/auth/sso/start")


if __name__ == "__main__":
    unittest.main()
