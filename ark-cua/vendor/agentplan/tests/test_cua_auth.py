import os
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import cua_auth  # noqa: E402
from cua_util import SkillError, _next_for_error  # noqa: E402


class FakeState:
    def __init__(self):
        self.data = {}

    @property
    def access_token(self):
        return self.data.get("api_key")

    @property
    def user(self):
        return self.data.get("user") or {}

    @property
    def desktop_bound(self):
        return bool(self.data.get("desktop_bound"))

    def set_api_key(self, **kwargs):
        self.data.update({
            "api_base_url": kwargs["api_base_url"],
            "api_key": kwargs["api_key"],
            "user": kwargs.get("user") or {},
            "desktop_bound": bool(kwargs.get("desktop_bound")),
        })

    def clear_tokens(self):
        self.data.clear()


class CuaAuthLoginTests(unittest.TestCase):
    def test_login_validates_and_caches_agentplan_api_key(self):
        state = FakeState()

        def fake_gateway_call(method, base_url, path, token=None, **_kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(base_url, "http://gateway")
            self.assertEqual(path, "/v1/auth/me")
            self.assertEqual(token, "ark-key")
            return {
                "auth_type": "agentplan_api_key",
                "scope": "cua:read cua:invoke",
                "desktop_bound": True,
                "user": {
                    "account_id": "2100",
                    "project_name": "agentplan",
                    "apikey_id": "ak_1",
                },
            }

        with mock.patch.object(cua_auth, "gateway_call", side_effect=fake_gateway_call):
            result = cua_auth.login(state, "http://gateway", api_key="ark-key")

        self.assertEqual(result["status"], "logged_in")
        self.assertEqual(result["auth_type"], "agentplan_api_key")
        self.assertEqual(result["scopes"], ["cua:read", "cua:invoke"])
        self.assertTrue(result["desktop_bound"])
        self.assertEqual(state.data["api_key"], "ark-key")
        self.assertEqual(state.data["user"]["account_id"], "2100")

    def test_status_uses_env_api_key_without_prompting(self):
        state = FakeState()

        def fake_gateway_call(method, base_url, path, token=None, **_kwargs):
            self.assertEqual(token, "env-key")
            return {"user": {"account_id": "env-account"}, "desktop_bound": False}

        with mock.patch.dict(os.environ, {"AP_CUA_AGENTPLAN_API_KEY": "env-key"}, clear=False), \
                mock.patch.object(cua_auth, "gateway_call", side_effect=fake_gateway_call):
            result = cua_auth.auth_status(state, "http://gateway")

        self.assertEqual(result["status"], "logged_in")
        self.assertEqual(result["user"]["account_id"], "env-account")
        self.assertFalse(result["desktop_bound"])
        self.assertEqual(state.data, {})

    def test_cached_agentplan_key_outranks_generic_ark_api_key(self):
        state = FakeState()
        state.data["api_key"] = "cached-agentplan-key"
        environment = {
            "AP_CUA_AGENTPLAN_API_KEY": "",
            "AGENTPLAN_API_KEY": "",
            "ARK_API_KEY": "unrelated-ark-key",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            self.assertEqual(
                cua_auth.ensure_access_token(state, "http://gateway"),
                "cached-agentplan-key",
            )

    def test_interactive_login_prompts_instead_of_using_generic_ark_api_key(self):
        state = FakeState()
        environment = {
            "AP_CUA_AGENTPLAN_API_KEY": "",
            "AGENTPLAN_API_KEY": "",
            "ARK_API_KEY": "unrelated-ark-key",
        }

        def fake_gateway_call(method, base_url, path, token=None, **_kwargs):
            self.assertEqual(token, "typed-agentplan-key")
            return {"user": {"account_id": "interactive"}, "desktop_bound": True}

        with mock.patch.dict(os.environ, environment, clear=False), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch.object(cua_auth.getpass, "getpass", return_value="typed-agentplan-key") as getpass_mock, \
                mock.patch.object(cua_auth, "gateway_call", side_effect=fake_gateway_call):
            result = cua_auth.login(state, "http://gateway")

        getpass_mock.assert_called_once()
        self.assertEqual(result["status"], "logged_in")
        self.assertEqual(state.data["api_key"], "typed-agentplan-key")

    def test_missing_api_key_returns_auth_required(self):
        state = FakeState()
        with mock.patch.dict(os.environ, {name: "" for name in cua_auth.API_KEY_ENV_VARS}, clear=False), \
                self.assertRaises(SkillError) as ctx:
            cua_auth.ensure_access_token(state, "http://gateway")
        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
        self.assertIn("auth login", ctx.exception.extra.get("setup_command", ""))

    def test_login_non_interactive_returns_setup_command_without_prompting(self):
        state = FakeState()
        with mock.patch.dict(os.environ, {name: "" for name in cua_auth.API_KEY_ENV_VARS}, clear=False), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch.object(cua_auth.getpass, "getpass") as getpass_mock, \
                self.assertRaises(SkillError) as ctx:
            cua_auth.login(state, "http://gateway", prompt=True)

        getpass_mock.assert_not_called()
        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
        self.assertIn("auth login", ctx.exception.extra.get("setup_command", ""))

    def test_login_invalid_agentplan_key_returns_actionable_message(self):
        state = FakeState()

        def fake_gateway_call(*_args, **_kwargs):
            raise SkillError("AUTH_REQUIRED", "ark acquire returned status 401", auth_type="agentplan_bearer")

        with mock.patch.object(cua_auth, "gateway_call", side_effect=fake_gateway_call), \
                self.assertRaises(SkillError) as ctx:
            cua_auth.login(state, "http://gateway", api_key="bad-key", prompt=False)

        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
        self.assertEqual(ctx.exception.message, "AgentPlan APIKey 不合法，请输入正确的 APIKey。")
        self.assertIn("auth login", ctx.exception.extra.get("setup_command", ""))
        self.assertEqual(ctx.exception.extra.get("auth_type"), "agentplan_bearer")


class CuaErrorHintTests(unittest.TestCase):
    def test_active_run_conflict_stops_without_followup_command(self):
        hint = _next_for_error({
            "code": "ACTIVE_RUN_CONFLICT",
            "message": "A run is already active for this desktop.",
        })

        self.assertIsNotNone(hint)
        self.assertNotIn("command", hint)
        self.assertIn("wait until the current desktop task finishes", hint["agent_hint"])
        self.assertIn("do not retry", hint["agent_hint"])

    def test_legacy_upstream_active_message_is_treated_as_conflict(self):
        hint = _next_for_error({
            "code": "UpstreamError",
            "message": "A desktop run is already active for this session.",
        })

        self.assertIsNotNone(hint)
        self.assertNotIn("command", hint)


if __name__ == "__main__":
    unittest.main()
