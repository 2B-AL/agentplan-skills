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

"""A credential is sent only to the gateway it belongs to (AgentPlan or a development environment)."""

import json
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cua  # noqa: E402
import cua_auth  # noqa: E402
from cua_state import AuthState  # noqa: E402
from cua_util import SkillError, bundled_base_url, login_setup_command  # noqa: E402

AGENTPLAN = bundled_base_url().rstrip("/")
DEV = "http://127.0.0.1:28444"
SAVED_KEY = secrets.token_urlsafe(24)
ARKCLI_KEY = secrets.token_urlsafe(24)


class GatewayCredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sent, self.prompts = [], []

    def state(self, **data):
        return AuthState(Path(self.directory.name) / "auth.json", dict(data))

    def gateway(self, method, base_url, path, token=None, **unused):
        self.sent.append((base_url, token))
        return {"user": {"id": "fixture"}}

    def answer(self, label):
        """What the user enters at the hidden prompt."""
        self.prompts.append(label)
        return SAVED_KEY

    def arkcli(self):
        return cua_auth.CredentialHandle(ARKCLI_KEY, "arkcli", "fixture"), None

    def call(self, state, base_url):
        with patch.object(cua_auth, "gateway_call", side_effect=self.gateway), \
                patch.object(cua_auth, "_arkcli_credential", side_effect=self.arkcli) as arkcli:
            try:
                cua_auth.authorized_call(state, base_url, "GET", "/v1/ping")
                error = None
            except SkillError as exc:
                error = exc
        return error, arkcli.call_count

    def test_an_agentplan_key_is_never_sent_to_another_gateway(self):
        state = self.state(api_base_url=AGENTPLAN, api_key=SAVED_KEY, credential_source="manual")
        error, arkcli = self.call(state, DEV)
        self.assertEqual((error.code, error.extra.get("gateway_login_required"), arkcli, self.sent), ("AUTH_REQUIRED", True, 0, []))
        self.assertIn("--api-base-url " + DEV, error.extra["setup_command"])
        self.assertNotIn(SAVED_KEY, json.dumps({"message": str(error), "extra": error.extra}))
        error, _ = self.call(state, AGENTPLAN + "/")
        self.assertIsNone(error)
        self.assertEqual(self.sent, [(AGENTPLAN + "/", SAVED_KEY)])

    def test_a_development_login_is_used_only_for_its_gateway(self):
        state = self.state(api_base_url=DEV, api_key=SAVED_KEY, credential_source="manual")
        self.assertIsNone(self.call(state, DEV)[0])
        self.assertIsNone(self.call(state, AGENTPLAN)[0])
        # The AgentPlan gateway gets arkcli's key, never the development login.
        self.assertEqual(self.sent, [(DEV, SAVED_KEY), (AGENTPLAN, ARKCLI_KEY)])

    def test_arkcli_supplies_only_the_agentplan_gateway(self):
        state = self.state()
        error, arkcli = self.call(state, DEV)
        self.assertEqual((error.code, arkcli, self.sent), ("AUTH_REQUIRED", 0, []))
        self.assertIsNone(self.call(state, AGENTPLAN)[0])
        self.assertEqual(self.sent, [(AGENTPLAN, ARKCLI_KEY)])

    def test_a_login_saved_before_gateways_were_recorded_belongs_to_agentplan(self):
        state = self.state(api_key=SAVED_KEY, credential_source="manual")
        self.assertIsNone(self.call(state, AGENTPLAN)[0])
        error, _ = self.call(state, DEV)
        self.assertEqual(error.code, "AUTH_REQUIRED")
        self.assertEqual(self.sent, [(AGENTPLAN, SAVED_KEY)])

    def test_a_rejected_development_login_never_falls_back_to_arkcli(self):
        state = self.state(api_base_url=DEV, api_key=SAVED_KEY, credential_source="cache")
        rejection = SkillError("AUTH_REQUIRED", "invalid", auth_type="agentplan_bearer")
        with patch.object(cua_auth, "gateway_call", side_effect=rejection), \
                patch.object(cua_auth, "_arkcli_credential", side_effect=AssertionError("arkcli must not be used")):
            with self.assertRaises(SkillError) as failure:
                cua_auth.authorized_call(state, DEV, "GET", "/v1/ping")
        self.assertEqual((failure.exception.code, failure.exception.extra.get("gateway_login_required")), ("AUTH_REQUIRED", True))
        self.assertIn("--api-base-url " + DEV, failure.exception.extra["setup_command"])

    def test_a_development_login_skips_arkcli_and_is_saved_for_that_gateway(self):
        state = self.state()
        with patch.object(cua_auth, "gateway_call", side_effect=self.gateway), \
                patch.object(cua_auth, "_arkcli_credential", side_effect=AssertionError("arkcli must not be used")), \
                patch.object(cua_auth.sys.stdin, "isatty", return_value=True), \
                patch.object(cua_auth.getpass, "getpass", side_effect=self.answer):
            result = cua_auth.login(state, DEV)
        self.assertEqual((result["status"], state.api_base_url, self.sent), ("logged_in", DEV, [(DEV, SAVED_KEY)]))
        self.assertIn("127.0.0.1:28444", self.prompts[0])
        self.assertTrue(cua_auth.saved_for_gateway(state, DEV))
        self.assertFalse(cua_auth.saved_for_gateway(state, AGENTPLAN))
        self.assertNotIn(SAVED_KEY, json.dumps(result))

    def test_logout_returns_to_the_bundled_gateway(self):
        state = self.state(api_base_url=DEV, api_key=SAVED_KEY, credential_source="manual")
        cua_auth.logout(state, DEV)
        self.assertEqual((state.api_base_url, state.access_token), (None, None))
        args = cua.build_parser().parse_args(["auth", "status"])
        with patch.dict(cua.os.environ, {}, clear=True):
            self.assertEqual(cua.resolve_base_url(args, state), AGENTPLAN)

    def test_browser_sync_and_the_error_envelope_keep_the_gateway_login_signal(self):
        import cua_cookiecloud
        import cua_util
        state = self.state(api_base_url=AGENTPLAN, api_key=SAVED_KEY, credential_source="manual")
        api = lambda method, path, body: cua_auth.authorized_call(state, DEV, method, path, body=body)  # noqa: E731
        with patch.object(cua_auth, "gateway_call", side_effect=self.gateway):
            with self.assertRaises(SkillError) as failure:
                cua_cookiecloud._status(api, "desk-a")
        self.assertEqual((failure.exception.code, failure.exception.extra.get("gateway_login_required"), self.sent), ("AUTH_REQUIRED", True, []))
        hint = cua_util._next_for_error({"code": "AUTH_REQUIRED", **failure.exception.extra})
        self.assertIn("not the bundled AgentPlan gateway", hint["agent_hint"])
        self.assertIn("--api-base-url " + DEV, hint["setup_command"])

    def test_agentplan_users_see_the_same_login_command_and_text_as_before(self):
        for base_url in (None, AGENTPLAN, AGENTPLAN + "/"):
            self.assertNotIn("--api-base-url", login_setup_command(base_url=base_url))
        self.assertTrue(login_setup_command(base_url=AGENTPLAN).endswith(" auth login"))
        manual = cua_auth._auth_required(manual=True, base_url=AGENTPLAN)
        self.assertEqual((str(manual), manual.extra["setup_command"].endswith(" auth login --manual")),
                         ("Manual AgentPlan API key login requires a local hidden prompt.", True))
        # Another gateway is named, so the login is saved for it.
        self.assertIn(" --api-base-url " + DEV + " auth login --manual", login_setup_command(manual=True, base_url=DEV))
        self.assertNotIn("AgentPlan", str(cua_auth._auth_required(manual=True, base_url=DEV)))

    def test_a_rejected_login_or_expired_token_names_the_development_gateway(self):
        state = self.state()
        rejection = SkillError("AUTH_REQUIRED", "invalid", auth_type="agentplan_bearer")
        with patch.object(cua_auth, "gateway_call", side_effect=rejection), \
                patch.object(cua_auth, "_arkcli_credential", side_effect=AssertionError("arkcli must not be used")), \
                patch.object(cua_auth.sys.stdin, "isatty", return_value=True), \
                patch.object(cua_auth.getpass, "getpass", side_effect=self.answer):
            with self.assertRaises(SkillError) as failure:
                cua_auth.login(state, DEV)
        extra = failure.exception.extra
        self.assertEqual((failure.exception.code, extra.get("gateway_login_required"), state.access_token), ("AUTH_REQUIRED", True, None))
        self.assertIn("--api-base-url " + DEV, extra["setup_command"])
        self.assertNotIn("AgentPlan", str(failure.exception))
        state = self.state(api_base_url=DEV, api_key=SAVED_KEY, credential_source="cache")
        with patch.object(cua_auth, "gateway_call", side_effect=SkillError("TOKEN_EXPIRED", "expired")):
            with self.assertRaises(SkillError) as failure:
                cua_auth.authorized_call(state, DEV, "GET", "/v1/ping")
        self.assertEqual(failure.exception.code, "TOKEN_EXPIRED")
        self.assertIn("--api-base-url " + DEV, failure.exception.extra["setup_command"])

    def test_logging_in_to_another_gateway_removes_the_previous_login(self):
        state = self.state(api_base_url=AGENTPLAN, api_key=SAVED_KEY, credential_source="manual")
        args = cua.build_parser().parse_args(["--api-base-url", DEV, "auth", "login", "--no-prompt"])
        with patch.object(cua_auth, "gateway_call", side_effect=self.gateway), \
                patch.object(cua_auth, "_arkcli_credential", side_effect=AssertionError("arkcli must not be used")):
            with self.assertRaises(SkillError) as failure:
                cua.cmd_auth_login(args, state, None)
        self.assertEqual(failure.exception.extra.get("gateway_login_required"), True)
        # Even this failed login removed the AgentPlan key: the development gateway is now selected.
        self.assertEqual((state.api_base_url, state.access_token, self.sent), (DEV, None, []))
        self.assertEqual(self.call(state, DEV)[0].code, "AUTH_REQUIRED")
        self.assertEqual(self.sent, [])
        # Selecting the gateway a login belongs to keeps it, including a file that recorded none.
        for data, gateway in [({"api_base_url": DEV}, DEV + "/"), ({}, AGENTPLAN)]:
            state = self.state(api_key=SAVED_KEY, credential_source="manual", **data)
            cua.resolve_base_url(cua.build_parser().parse_args(["--api-base-url", gateway, "auth", "status"]), state, persist=True)
            self.assertEqual(state.access_token, SAVED_KEY)


if __name__ == "__main__":
    unittest.main()
