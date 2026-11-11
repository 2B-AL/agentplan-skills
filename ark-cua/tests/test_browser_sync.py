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
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cua  # noqa: E402


class BrowserSyncTests(unittest.TestCase):
    def args(self, action="credentials setup", **extra):
        values = dict(action=action, api_base_url="https://pre.example.test", desktop_id="desk-exact",
                      skip_browser=False, agent_path=None, timeout_seconds=60)
        values.update(extra)
        return argparse.Namespace(**values)

    def manifest(self):
        return {"capabilities": {"credentials": True},
                "tools": [{"name": name, "enabled": True} for name in cua.CREDENTIAL_GATEWAY_TOOLS]}

    def target(self):
        return {"adapter_protocol": cua.CREDENTIAL_TARGET_PROTOCOL, "transport": "access_hub_gateway",
                "environment_id": "prod", "features": list(cua.CREDENTIAL_DEVICE_FEATURES)}

    def test_browser_sync_has_one_entry_and_agent_setup_remains_explicit(self):
        parser = cua.build_parser()
        args = parser.parse_args(["browser-sync", "--desktop-id", "desk-exact",
                                  "--cdp-url", "http://127.0.0.1:9222", "--domain", "example.com"])
        self.assertIs(args.handler, cua.cmd_browser_sync)
        # Checking results after the first setup needs no Chrome debugging connection.
        args = parser.parse_args(["browser-sync", "--desktop-id", "desk-exact", "--domain", "example.com"])
        self.assertEqual((args.cdp_url, args.with_storage, args.wait_seconds), (None, False, 0))
        args = parser.parse_args(["credentials", "setup", "--skip-browser"])
        self.assertIs(args.handler, cua.cmd_credentials_setup)
        for command in [["credentials", "setup"],
                        ["credentials", "sync", "browser", "--desktop-id", "desk-exact", "example.com"]]:
            with self.subTest(command=command):
                with self.assertRaises(cua.SkillError) as failure:
                    parser.parse_args(command)
                self.assertEqual(failure.exception.code, "VALIDATION_ERROR")

    def test_device_setup_retains_agent_workflow(self):
        with patch.object(cua.cua_auth, "authorized_manifest", return_value=self.manifest()), \
                patch.object(cua, "_credential_tool", return_value=self.target()), \
                patch.object(cua, "_ensure_credential_agent_installed", return_value={"agent_path": "/fixture/agent"}), \
                patch.object(cua, "_safe_agent_path", return_value=Path("/fixture/agent")), \
                patch.object(cua, "_ensure_source_agent_profile", return_value=({}, {})), \
                patch.object(cua, "_run_credential_script", return_value={}) as prepare, \
                patch.object(cua, "_run_local_target", return_value={"workflow_id": "fixture", "device_ready": True}) as target:
            result = cua.cmd_credentials_setup(self.args("credentials setup", skip_browser=True), object(), object())
            self.assertEqual(result["data"]["status"], "succeeded")
            self.assertIn("--skip-browser", prepare.call_args.args[0])
            self.assertEqual(target.call_args_list[0].args[1][:3], ["begin", "--mode", "device"])
            self.assertEqual(target.call_args_list[1].args[1][:3], ["finish", "--workflow-id", "fixture"])

    def test_agent_status_and_authentication_errors_remain_read_only(self):
        with patch.object(cua.cua_auth, "authorized_manifest", return_value=self.manifest()), \
                patch.object(cua, "_credential_tool", return_value=self.target()), \
                patch.object(cua, "_safe_agent_path", side_effect=cua.SkillError("AGENT_MISSING", "missing")), \
                patch.object(cua, "_ensure_credential_agent_installed", side_effect=AssertionError("Agent installed")):
            result = cua.cmd_credentials_status(self.args("credentials status"), object(), object())
            self.assertFalse(result["data"]["source_agent"]["installed"])
        with patch.object(cua.cua_auth, "authorized_manifest", side_effect=cua.SkillError("AUTH_REQUIRED", "login")):
            with self.assertRaises(cua.SkillError) as failure:
                cua.cmd_credentials_status(self.args("credentials status"), object(), object())
            self.assertEqual(failure.exception.code, "AUTH_REQUIRED")

    def test_desktop_access_preserves_exact_selection_and_default(self):
        for desktop in [None, "desk-exact"]:
            command = ["--api-base-url", "https://pre.example.test", "desktop", "access"]
            if desktop:
                command.extend(["--desktop", desktop])
            args = cua.build_parser().parse_args(command)
            with self.subTest(desktop=desktop), \
                    patch.object(cua.cua_auth, "authorized_call", return_value={"access_url": "https://desktop.test/desktops/desk-exact/cua-app/?ticket=fixture"}) as call:
                result = cua.cmd_desktop_access(args, object(), object())
                self.assertEqual(call.call_args.args[2], "GET")
                expected = {"desktop": [desktop]} if desktop else {}
                self.assertEqual(parse_qs(urlsplit(call.call_args.args[3]).query), expected)
                self.assertIn("/desktops/desk-exact/cua-app/", result["data"]["full_interface_url"])


if __name__ == "__main__":
    unittest.main()
