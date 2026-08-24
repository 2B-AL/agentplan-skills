import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cua  # noqa: E402
import cua_http  # noqa: E402
from cua_state import SessionState  # noqa: E402
from cua_util import SkillError  # noqa: E402


class CredentialIntegrationTests(unittest.TestCase):
    def test_bundled_dependency_is_the_required_https_release(self):
        dependency = cua.bundled_config()["credential_skill"]
        self.assertEqual(dependency["adapter_protocol"], "cua-target/v1")
        self.assertEqual(dependency["repository"], "https://github.com/2B-AL/credential-skill")
        self.assertEqual(dependency["commit"], "f194661d62ccdbebec0ea0e4bee9c60459bed635")

    def test_parser_exposes_high_level_credentials_and_internal_adapter(self):
        parser = cua.build_parser()
        browser = parser.parse_args([
            "credentials", "sync", "browser", "--desktop-id", "desk-1", "github",
        ])
        target = parser.parse_args(["credential-target", "capabilities"])
        self.assertIs(browser.handler, cua.cmd_credentials_sync_browser)
        self.assertEqual(browser.site, ["github"])
        self.assertIs(target.handler, cua.cmd_credential_target_capabilities)

    def test_disabled_manifest_fails_before_dependency_download(self):
        args = Namespace(api_base_url=None, desktop_id="desk-1")
        manifest = {"capabilities": {"credentials": False}, "tools": []}
        with (
            mock.patch.object(cua, "resolve_base_url", return_value="https://gateway.test"),
            mock.patch.object(cua, "gateway_manifest", return_value=manifest),
            mock.patch.object(cua.cua_dependency, "ensure") as ensure,
            self.assertRaises(SkillError) as raised,
        ):
            cua._credential_gateway_preflight(args, object(), browser=True)
        self.assertEqual(raised.exception.code, "TARGET_CAPABILITY_UNAVAILABLE")
        ensure.assert_not_called()

    def test_browser_sync_uses_pinned_skill_without_permission_mutation(self):
        runtime = Path("/runtime")
        args = Namespace(
            api_base_url=None,
            desktop_id="desk-1",
            agent_path=None,
            timeout_seconds=420,
            site=["github", "volcengine"],
        )
        calls = []

        def run_script(command, _timeout, **_kwargs):
            calls.append(command)
            if "prepare-source.py" in command[1]:
                return {"status": "succeeded", "agent_path": "/safe/credential-agent"}
            return {"status": "succeeded", "details": {"job_id": "job-1"}}

        with (
            mock.patch.object(cua, "_credential_gateway_preflight", return_value={}),
            mock.patch.object(cua.cua_dependency, "ensure", return_value=runtime),
            mock.patch.object(cua, "_safe_agent_path", return_value=Path("/safe/credential-agent")),
            mock.patch.object(cua, "_run_credential_script", side_effect=run_script),
        ):
            result = cua.cmd_credentials_sync_browser(args, object(), object())

        self.assertIn("prepare-source.py", calls[0][1])
        self.assertIn("sync-cua.py", calls[1][1])
        self.assertEqual(calls[1][-2:], ["github", "volcengine"])
        command_text = " ".join(calls[1])
        self.assertNotIn("browser-authorize", command_text)
        self.assertNotIn("open-permissions", command_text)
        self.assertEqual(result["data"]["status"], "succeeded")

    def test_target_capabilities_output_is_bounded(self):
        stdout = io.StringIO()
        with (
            mock.patch.object(cua, "_credential_tool", return_value={
                "adapter_protocol": "cua-target/v1",
                "transport": "access_hub_gateway",
                "desktop": {"id": "desk-1", "name": "win-prod-1", "state": "ready"},
                "features": ["pair-relay-v1", "health-v1"],
                "capability": "must-not-leak",
            }),
            mock.patch.object(sys, "stdout", stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cua.cmd_credential_target_capabilities(Namespace(desktop_id="desk-1"), object(), object())
        self.assertEqual(raised.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["adapter_protocol"], "cua-target/v1")
        self.assertNotIn("must-not-leak", stdout.getvalue())

    def test_tool_client_parses_existing_result_envelope(self):
        with mock.patch.object(
            cua_http,
            "request",
            return_value=(200, {"ok": True, "result": {"adapter_protocol": "cua-target/v1"}}),
        ) as request:
            result = cua_http.gateway_tool_call(
                "https://gateway.test", "secret-agentplan-key", "cua_credential_capabilities", {}
            )
        self.assertEqual(result["adapter_protocol"], "cua-target/v1")
        self.assertEqual(request.call_args.args[2], "/skill/tools/cua_credential_capabilities")

    def test_withheld_https_capability_fails_closed(self):
        session = mock.Mock()
        session.workflow_for_credential_operation.return_value = "workflow-1"
        args = Namespace(
            operation_id="operation-1",
            timeout_seconds=10,
            poll_interval_ms=10,
        )
        with (
            mock.patch.object(cua, "_credential_tool", return_value={"status": "failed"}) as tool,
            self.assertRaises(SkillError) as raised,
        ):
            cua.cmd_credential_target_authorize_watch(args, object(), session)
        self.assertEqual(raised.exception.code, "BROWSER_PERMISSION_REQUIRED")
        tool.assert_called_once()

    def test_source_host_permission_error_is_preserved(self):
        completed = mock.Mock(
            returncode=1,
            stdout=json.dumps({
                "type": "result",
                "status": "failed",
                "error": {"code": "HOST_PERMISSION_REQUIRED", "message": "withheld"},
                "details": {"job_id": "job-1"},
            }) + "\n",
        )
        with (
            mock.patch.object(cua.subprocess, "run", return_value=completed),
            self.assertRaises(SkillError) as raised,
        ):
            cua._run_credential_script(["credential-agent"], 30)
        self.assertEqual(raised.exception.code, "HOST_PERMISSION_REQUIRED")
        self.assertEqual(raised.exception.extra["job_id"], "job-1")

    def test_session_reuses_bounded_begin_and_reset_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            state = SessionState(Path(temp) / "session.json", {})
            begin_one = state.credential_begin_request("desk-1", "browser")
            begin_two = state.credential_begin_request("desk-1", "browser")
            reset_one = state.credential_reset_request("desk-1", "device-1")
            reset_two = state.credential_reset_request("desk-1", "device-1")
        self.assertEqual(begin_one, begin_two)
        self.assertEqual(reset_one, reset_two)


if __name__ == "__main__":
    unittest.main()
