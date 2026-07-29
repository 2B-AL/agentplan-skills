import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "cua.py"


class UnifiedCliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = os.environ.copy()
        self.env["ARK_CUA_STATE_DIR"] = self.temp_dir.name
        self.env.pop("ARK_CUA_AUTH_SCHEME", None)

    def run_cli(self, *args, env=None):
        process = subprocess.run(
            [sys.executable, str(CLI), *args],
            text=True,
            capture_output=True,
            env=env or self.env,
            check=False,
        )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        payload = (
            json.loads(lines[-1])
            if lines and lines[-1].lstrip().startswith("{")
            else None
        )
        return process, payload

    def test_default_scheme_is_agentplan(self):
        process, payload = self.run_cli("auth-scheme", "status")
        self.assertEqual(process.returncode, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["auth_scheme"], "agentplan")
        self.assertEqual(payload["data"]["source"], "default")

    def test_cli_scheme_has_highest_precedence(self):
        env = dict(self.env)
        env["ARK_CUA_AUTH_SCHEME"] = "bytesso"
        process, payload = self.run_cli(
            "--auth-scheme", "agentplan", "auth-scheme", "status", env=env
        )
        self.assertEqual(process.returncode, 0)
        self.assertEqual(payload["data"]["auth_scheme"], "agentplan")
        self.assertEqual(payload["data"]["source"], "cli")

    def test_persisted_selection_is_secure_and_reused(self):
        process, payload = self.run_cli("auth-scheme", "use", "bytesso")
        self.assertEqual(process.returncode, 0)
        self.assertEqual(payload["data"]["auth_scheme"], "bytesso")
        selection = Path(self.temp_dir.name) / "selection.json"
        self.assertEqual(selection.stat().st_mode & 0o777, 0o600)

        process, payload = self.run_cli("auth-scheme", "status")
        self.assertEqual(process.returncode, 0)
        self.assertEqual(payload["data"]["auth_scheme"], "bytesso")
        self.assertEqual(payload["data"]["source"], "persisted")

    def test_reset_restores_agentplan_default(self):
        self.run_cli("auth-scheme", "use", "bytesso")
        process, payload = self.run_cli("auth-scheme", "reset")
        self.assertEqual(process.returncode, 0)
        self.assertEqual(payload["data"]["auth_scheme"], "agentplan")
        self.assertFalse((Path(self.temp_dir.name) / "selection.json").exists())

    def test_invalid_scheme_fails_before_adapter(self):
        process, payload = self.run_cli("--auth-scheme", "unknown", "ping")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(payload["error"]["code"], "INVALID_AUTH_SCHEME")
        self.assertFalse(payload["error"]["accepted"])

    def test_agentplan_rejects_bytesso_only_capability(self):
        process, payload = self.run_cli(
            "--auth-scheme", "agentplan", "credentials", "status"
        )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(payload["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(payload["error"]["scheme"], "agentplan")
        self.assertFalse(payload["error"]["accepted"])

    def test_bytesso_rejects_agentplan_only_capability(self):
        process, payload = self.run_cli(
            "--auth-scheme", "bytesso", "schedule", "list"
        )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(payload["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(payload["error"]["scheme"], "bytesso")

    def test_agentplan_adapter_uses_new_state_root(self):
        process, payload = self.run_cli(
            "--auth-scheme", "agentplan", "self-test"
        )
        self.assertEqual(process.returncode, 0)
        self.assertTrue(payload["ok"])
        expected = Path(self.temp_dir.name) / "auth-schemes" / "agentplan" / "auth.json"
        self.assertEqual(payload["data"]["auth_file"], str(expected))

    def test_agentplan_setup_command_points_to_unified_launcher(self):
        env = dict(self.env)
        for key in (
            "AP_CUA_AGENTPLAN_API_KEY",
            "AGENTPLAN_API_KEY",
            "ARK_API_KEY",
        ):
            env.pop(key, None)
        process, payload = self.run_cli(
            "--auth-scheme",
            "agentplan",
            "auth",
            "login",
            "--no-prompt",
            env=env,
        )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(payload["error"]["code"], "AUTH_REQUIRED")
        self.assertIn(str(CLI), payload["error"]["setup_command"])
        self.assertIn("--auth-scheme agentplan", payload["error"]["setup_command"])
        self.assertNotIn("/vendor/agentplan/", payload["error"]["setup_command"])

    def test_bytesso_adapter_self_test_is_complete(self):
        process, payload = self.run_cli(
            "--auth-scheme", "bytesso", "self-test"
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["auth"]["status"], "logged_out")
        self.assertIn(
            "--auth-scheme bytesso",
            payload["data"]["auth"]["retry_command"],
        )

    def test_desktop_alias_reaches_selected_parser(self):
        process, _payload = self.run_cli(
            "--auth-scheme", "bytesso", "desktop", "list", "--help"
        )
        self.assertEqual(process.returncode, 0)
        self.assertIn("usage:", process.stdout)


if __name__ == "__main__":
    unittest.main()
