import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import cua  # noqa: E402
import cua_util  # noqa: E402


class CuaCliTests(unittest.TestCase):
    class Session:
        default_desktop_id = None

        def __init__(self):
            self.last = None
            self.last_desktop = None
            self.desktops = []

        def set_last_invocation_id(self, invocation_id):
            self.last = invocation_id

        @property
        def last_invocation_id(self):
            return self.last

        def set_last_task_desktop_id(self, desktop_id):
            self.last_desktop = desktop_id

        def remember_desktops(self, desktops):
            self.desktops = desktops

    def test_emit_error_mirrors_structured_json_to_stderr(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        error = cua_util.SkillError(
            "UPSTREAM_TIMEOUT",
            "my-cua request timed out",
            source="my_cua",
            stage="run_create",
            accepted="unknown",
            request_id="req-1",
        )

        with (
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cua_util.emit_error("delegate", error)

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(stdout.getvalue(), stderr.getvalue())
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error"]["code"], "UPSTREAM_TIMEOUT")
        self.assertEqual(payload["error"]["stage"], "run_create")
        self.assertEqual(payload["error"]["accepted"], "unknown")
        self.assertIn("Do not submit", payload["next"]["agent_hint"])

    def test_desktops_allocate_calls_gateway_tool(self):
        session = self.Session()
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua.cua_auth, "ensure_bearer_key", return_value="token"),
            mock.patch.object(cua, "gateway_tool_call", return_value={"desktop": {"desktop_id": "vm-2"}}) as call,
        ):
            result = cua.cmd_desktops_allocate(
                Namespace(spec_code="s80", label="qa-run"),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["desktop"]["desktop_id"], "vm-2")
        self.assertEqual(session.desktops, [{"desktop_id": "vm-2"}])
        call.assert_called_once_with(
            "http://gateway",
            "token",
            "cua_allocate_desktop",
            {"spec_code": "s80", "label": "qa-run"},
            timeout=60,
        )

    def test_desktops_reboot_waits_for_success_without_confirmation(self):
        session = self.Session()
        responses = [
            {
                "desktop": {"desktop_id": "vm-2"},
                "operation": {"operation_id": "op-1", "desktop_id": "vm-2", "status": "running"},
            },
            {"operation": {"operation_id": "op-1", "desktop_id": "vm-2", "status": "succeeded"}},
        ]
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua.uuid, "uuid4", return_value=mock.Mock(hex="request-1")),
            mock.patch.object(cua.time, "sleep"),
            mock.patch.object(cua.cua_auth, "authorized_tool_call", side_effect=responses) as call,
        ):
            result = cua.cmd_desktops_reboot(
                Namespace(desktop_id="vm-2", wait_ms=600000),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["operation"]["status"], "succeeded")
        self.assertEqual(call.call_args_list[0].args[3], "cua_reboot_desktop")
        self.assertEqual(
            call.call_args_list[0].args[4],
            {"desktop_id": "vm-2", "idempotency_key": "cua-skill-reboot-request-1"},
        )
        self.assertNotIn("confirm", call.call_args_list[0].args[4])
        self.assertEqual(call.call_args_list[1].args[3], "cua_get_desktop_operation")
        self.assertEqual(call.call_args_list[1].args[4], {"operation_id": "op-1"})

    def test_desktops_reboot_timeout_blocks_new_tasks(self):
        session = self.Session()
        session.default_desktop_id = "vm-default"
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua.uuid, "uuid4", return_value=mock.Mock(hex="request-2")),
            mock.patch.object(
                cua.cua_auth,
                "authorized_tool_call",
                return_value={"operation": {"operation_id": "op-2", "status": "running"}},
            ) as call,
        ):
            with self.assertRaises(cua.SkillError) as raised:
                cua.cmd_desktops_reboot(
                    Namespace(desktop_id=None, wait_ms=0),
                    state=object(),
                    session=session,
                )

        self.assertEqual(raised.exception.code, "DESKTOP_REBOOT_IN_PROGRESS")
        self.assertIn("desktops operation op-2", raised.exception.extra["retry_command"])
        self.assertEqual(call.call_args.args[4]["desktop_id"], "vm-default")

    def test_desktops_operation_surfaces_reboot_failure(self):
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(
                cua.cua_auth,
                "authorized_tool_call",
                return_value={
                    "operation": {
                        "operation_id": "op-3",
                        "status": "failed",
                        "error": {"code": "UIANotReady", "message": "desktop UI automation did not become ready"},
                    }
                },
            ),
        ):
            with self.assertRaises(cua.SkillError) as raised:
                cua.cmd_desktops_operation(
                    Namespace(operation_id="op-3", wait_ms=600000),
                    state=object(),
                    session=self.Session(),
                )

        self.assertEqual(raised.exception.code, "DESKTOP_UNHEALTHY")
        self.assertEqual(raised.exception.extra["upstream_code"], "UIANotReady")
        self.assertEqual(raised.exception.extra["source"], "desktop_runtime")
        self.assertEqual(raised.exception.extra["operation"]["status"], "failed")

    def test_desktops_reboot_parser_has_no_confirmation(self):
        args = cua.build_parser().parse_args(["desktops", "reboot", "vm-2"])

        self.assertEqual(args.desktop_id, "vm-2")
        self.assertEqual(args.wait_ms, 600000)
        self.assertFalse(hasattr(args, "confirm"))

    def test_delegate_passes_desktop_id(self):
        session = self.Session()
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(
                cua.cua_auth,
                "authorized_tool_call",
                return_value={"task_id": "task-1", "status": "running", "upstream": {}},
            ) as call,
        ):
            result = cua.cmd_delegate(
                Namespace(objective="do work", desktop_id="vm-2", session_id=None, auto=False, wait_ms=None),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["invocation_id"], "task-1")
        self.assertEqual(session.last_desktop, "vm-2")
        call.assert_called_once()
        self.assertEqual(call.call_args.args[3], "cua_run_task")
        self.assertEqual(call.call_args.args[4], {"input": "do work", "desktop_id": "vm-2"})

    def test_credential_target_capabilities_are_gateway_only_and_bounded(self):
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
        self.assertEqual(payload["data"]["transport"], "access_hub_gateway")
        self.assertNotIn("capability", stdout.getvalue())
        self.assertNotIn("my-cua-dev", stdout.getvalue())

    def test_credentials_setup_prepares_source_then_exact_target_and_finishes(self):
        runtime = Path("/runtime")
        args = Namespace(
            desktop_id="desk-1",
            agent_path=None,
            skip_browser=False,
            timeout_seconds=300,
            access_hub_base_url=None,
            gateway_url=None,
        )
        calls = []

        def target(_args, command, _timeout):
            calls.append(command)
            if command[0] == "begin":
                return {
                    "workflow_id": "workflow-1",
                    "desktop_id": "desk-1",
                    "device_id": "device-1",
                    "device_ready": True,
                    "browser_extension_ready": True,
                    "browser_connected": True,
                }
            return {"workflow_id": "workflow-1", "finished": True}

        with (
            mock.patch.object(cua, "_credential_gateway_preflight", return_value={}),
            mock.patch.object(cua.cua_dependency, "ensure", return_value=runtime),
            mock.patch.object(cua, "_run_credential_script", return_value={
                "status": "succeeded",
                "agent_path": "/safe/credential-agent",
                "agent_installed": True,
            }) as prepare,
            mock.patch.object(cua, "_safe_agent_path", return_value=Path("/safe/credential-agent")),
            mock.patch.object(cua, "_run_local_target", side_effect=target),
        ):
            result = cua.cmd_credentials_setup(args, object(), object())

        self.assertIn("prepare-source.py", prepare.call_args.args[0][1])
        self.assertEqual(calls[0][:5], [
            "begin", "--mode", "browser", "--agent-path", "/safe/credential-agent",
        ])
        self.assertIn("desk-1", calls[0])
        self.assertEqual(calls[1][:3], ["finish", "--workflow-id", "workflow-1"])
        self.assertEqual(result["data"]["device_id"], "device-1")
        self.assertTrue(result["data"]["browser_ready"])

    def test_credential_preflight_rejects_disabled_gateway_before_dependency_install(self):
        args = Namespace(desktop_id="desk-1")
        manifest = {
            "capabilities": {"credentials": False},
            "tools": [],
        }
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua, "gateway_manifest", return_value=manifest),
            mock.patch.object(cua.cua_dependency, "ensure") as ensure,
            self.assertRaises(cua_util.SkillError) as raised,
        ):
            cua._credential_gateway_preflight(args, object(), browser=True)
        self.assertEqual(raised.exception.code, "TARGET_CAPABILITY_UNAVAILABLE")
        ensure.assert_not_called()

    def test_browser_sync_prepares_source_automatically(self):
        runtime = Path("/runtime")
        args = Namespace(
            desktop_id="desk-1", agent_path=None, timeout_seconds=420,
            site=["github", "volcengine"],
        )
        calls = []

        def run_script(command, _timeout):
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
        self.assertNotIn("--skip-browser", calls[0])
        self.assertIn("sync-cua.py", calls[1][1])
        self.assertEqual(calls[1][-2:], ["github", "volcengine"])
        self.assertEqual(result["data"]["status"], "succeeded")

    def test_credential_target_begin_ready_path_hides_gateway_internals(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = Path(tmp) / "credential-agent"
            agent.write_text("#!/bin/sh\nexit 0\n")
            agent.chmod(0o700)
            session = mock.Mock()
            session.credential_begin_request.return_value = "cred-request-1"
            stdout = io.StringIO()
            ready = {
                "workflow_id": "cw_1234567890abcdef",
                "desktop_id": "desk-1",
                "desktop_name": "win-prod-1",
                "device_id": "device-1",
                "status": "browser_ready",
                "device_ready": True,
                "browser_extension_ready": True,
                "browser_connected": True,
                "expires_at": "2026-07-25T00:00:00Z",
                "mycua_session_id": "must-not-leak",
            }
            args = Namespace(
                agent_path=str(agent), desktop_id="desk-1", mode="browser", timeout_seconds=10,
            )
            with (
                mock.patch.object(cua, "_credential_begin_once", return_value=ready),
                mock.patch.object(sys, "stdout", stdout),
                self.assertRaises(SystemExit) as raised,
            ):
                cua.cmd_credential_target_begin(args, object(), session)
        self.assertEqual(raised.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["data"]["device_id"], "device-1")
        self.assertNotIn("session", stdout.getvalue().lower())
        session.complete_credential_begin.assert_called_once_with(
            "desk-1", "browser", "cw_1234567890abcdef", "device-1"
        )

    def test_delegate_passes_existing_session_id(self):
        session = self.Session()
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(
                cua.cua_auth,
                "authorized_tool_call",
                return_value={
                    "task_id": "task-2",
                    "mycua_session_id": "sess-1",
                    "mycua_run_id": "run-2",
                    "status": "running",
                    "upstream": {},
                },
            ) as call,
        ):
            result = cua.cmd_delegate(
                Namespace(
                    objective="continue the work",
                    desktop_id="vm-2",
                    session_id="sess-1",
                    auto=False,
                    wait_ms=None,
                ),
                state=object(),
                session=session,
            )

        self.assertEqual(
            call.call_args.args[4],
            {"input": "continue the work", "session_id": "sess-1", "desktop_id": "vm-2"},
        )
        self.assertEqual(result["data"]["session_id"], "sess-1")
        self.assertEqual(result["data"]["diagnostics"]["mycua_session_id"], "sess-1")

    def test_delegate_parser_accepts_session_id(self):
        args = cua.build_parser().parse_args(
            ["delegate", "--objective", "continue", "--session-id", "sess-1"]
        )

        self.assertEqual(args.session_id, "sess-1")

    def test_delegate_rejects_auto_with_existing_session(self):
        with mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")):
            with self.assertRaises(cua.SkillError) as raised:
                cua.cmd_delegate(
                    Namespace(
                        objective="continue",
                        desktop_id=None,
                        session_id="sess-1",
                        auto=True,
                        wait_ms=None,
                    ),
                    state=object(),
                    session=self.Session(),
                )

        self.assertEqual(raised.exception.code, "VALIDATION_ERROR")
        self.assertIn("--session-id cannot be combined with --auto", raised.exception.message)

    def test_auto_delegate_selects_idle_desktop(self):
        session = self.Session()
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua.cua_auth, "ensure_bearer_key", return_value="token"),
            mock.patch.object(
                cua,
                "gateway_tool_call",
                return_value={
                    "quota": {"max_active_cuas": 5, "active_count": 2},
                    "desktops": [
                        {"desktop_id": "vm-1", "busy": True, "current_task_id": "task-busy"},
                        {"desktop_id": "vm-2", "busy": False},
                    ],
                },
            ),
            mock.patch.object(
                cua.cua_auth,
                "authorized_tool_call",
                return_value={"task_id": "task-1", "status": "running", "upstream": {}},
            ) as call,
        ):
            result = cua.cmd_delegate(
                Namespace(objective="do work", desktop_id=None, session_id=None, auto=True, wait_ms=None),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["invocation_id"], "task-1")
        self.assertEqual(call.call_args.args[4], {"input": "do work", "desktop_id": "vm-2"})

    def test_auto_delegate_with_only_busy_desktops_uses_stable_error_code(self):
        session = self.Session()
        with (
            mock.patch.object(cua.cua_auth, "ensure_bearer_key", return_value="token"),
            mock.patch.object(
                cua,
                "gateway_tool_call",
                return_value={
                    "quota": {"max_active_cuas": 1, "active_count": 1},
                    "desktops": [{"desktop_id": "vm-1", "busy": True}],
                },
            ),
        ):
            with self.assertRaises(cua.SkillError) as raised:
                cua._resolve_delegate_desktop(
                    Namespace(desktop_id=None, auto=True),
                    state=object(),
                    session=session,
                    access_hub="http://hub",
                    gateway_url="http://gateway",
                )

        self.assertEqual(raised.exception.code, "DESKTOP_BUSY")
        self.assertEqual(raised.exception.extra["upstream_code"], "NO_IDLE_DESKTOP")

    def test_tasks_list_calls_gateway_tool(self):
        session = self.Session()
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua.cua_auth, "ensure_bearer_key", return_value="token"),
            mock.patch.object(cua, "gateway_tool_call", return_value={"tasks": [], "count": 0}) as call,
        ):
            result = cua.cmd_tasks_list(
                Namespace(status="active", limit=20),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["count"], 0)
        call.assert_called_once_with(
            "http://gateway",
            "token",
            "cua_list_tasks",
            {"status": "active", "limit": 20},
            timeout=30,
        )

    def test_tasks_watch_normalizes_multiple_results(self):
        session = self.Session()
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua.cua_auth, "ensure_bearer_key", return_value="token"),
            mock.patch.object(
                cua,
                "gateway_tool_call",
                return_value={
                    "tasks": [
                        {
                            "task": {
                                "task_id": "task-1",
                                "status": "succeeded",
                                "mycua_session_id": "sess-1",
                                "mycua_run_id": "run-1",
                            },
                            "upstream": {"status": "succeeded", "text": "done 1"},
                        },
                        {
                            "task": {"task_id": "task-2", "status": "running", "mycua_run_id": "run-2"},
                            "upstream": {"run": {"status": "running"}},
                        },
                    ],
                    "count": 2,
                    "completed_count": 1,
                    "failed_count": 0,
                    "cancelled_count": 0,
                    "needs_input_count": 0,
                    "pending_count": 1,
                    "terminal_count": 1,
                    "settled_count": 1,
                },
            ) as call,
        ):
            result = cua.cmd_tasks_watch(
                Namespace(task_id=["task-1", "task-2"], last=False, wait_ms=1000, include_upstream=False),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["count"], 2)
        self.assertEqual(result["data"]["completed_count"], 1)
        self.assertEqual(result["data"]["pending_count"], 1)
        self.assertEqual(result["data"]["terminal_count"], 1)
        self.assertEqual(result["data"]["settled_count"], 1)
        self.assertEqual(result["data"]["tasks"][0]["outcome"], "completed")
        self.assertEqual(result["data"]["tasks"][0]["session_id"], "sess-1")
        self.assertEqual(result["data"]["tasks"][0]["result"]["text"], "done 1")
        self.assertEqual(result["data"]["tasks"][1]["outcome"], "in_progress")
        self.assertEqual(session.last, "task-2")
        call.assert_called_once_with(
            "http://gateway",
            "token",
            "cua_watch_tasks",
            {"task_ids": ["task-1", "task-2"], "include_upstream": False, "timeout_seconds": 1},
            timeout=31,
        )

    def test_watch_uses_total_wait_budget_in_server_sized_chunks(self):
        session = self.Session()
        responses = [
            {"task_id": "task-1", "status": "running", "upstream": {}},
            {"task_id": "task-1", "status": "running", "upstream": {}},
            {"task_id": "task-1", "status": "succeeded", "upstream": {"status": "succeeded", "text": "done"}},
        ]
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua.cua_auth, "authorized_tool_call", side_effect=responses) as call,
        ):
            result = cua.cmd_watch(
                Namespace(invocation_id="task-1", last=False, wait_ms=125000),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["outcome"], "completed")
        self.assertEqual([item.args[4]["timeout_seconds"] for item in call.call_args_list], [60, 60, 5])
        self.assertEqual([item.kwargs["timeout"] for item in call.call_args_list], [90, 90, 35])

    def test_tasks_watch_uses_total_wait_budget_in_server_sized_chunks(self):
        session = self.Session()
        responses = [
            {
                "tasks": [{"task": {"task_id": "task-1", "status": "running"}}],
                "count": 1,
                "pending_count": 1,
            },
            {
                "tasks": [{"task": {"task_id": "task-1", "status": "succeeded"}, "upstream": {"text": "done"}}],
                "count": 1,
                "completed_count": 1,
                "pending_count": 0,
                "terminal_count": 1,
                "settled_count": 1,
            },
        ]
        with (
            mock.patch.object(cua, "resolve_urls", return_value=("http://hub", "http://gateway")),
            mock.patch.object(cua.cua_auth, "ensure_bearer_key", return_value="token"),
            mock.patch.object(cua, "gateway_tool_call", side_effect=responses) as call,
        ):
            result = cua.cmd_tasks_watch(
                Namespace(task_id=["task-1"], last=False, wait_ms=61000, include_upstream=False),
                state=object(),
                session=session,
            )

        self.assertEqual(result["data"]["tasks"][0]["outcome"], "completed")
        self.assertEqual([item.args[3]["timeout_seconds"] for item in call.call_args_list], [60, 1])
        self.assertEqual([item.kwargs["timeout"] for item in call.call_args_list], [90, 31])

    def test_task_envelope_reads_mycua_final_text(self):
        payload = {
            "task_id": "cua_task_1",
            "status": "succeeded",
            "upstream": {
                "runId": "run_1",
                "status": "succeeded",
                "finalText": "finished answer",
                "artifacts": [],
            },
        }

        envelope = cua._task_envelope(payload)

        self.assertEqual(envelope["outcome"], "completed")
        self.assertEqual(envelope["result"]["text"], "finished answer")

    def test_task_envelope_exposes_mycua_session_id(self):
        envelope = cua._task_envelope(
            {
                "task_id": "cua_task_1",
                "mycua_session_id": "sess-1",
                "mycua_run_id": "run-1",
                "status": "running",
                "upstream": {},
            }
        )

        self.assertEqual(envelope["session_id"], "sess-1")
        self.assertEqual(envelope["diagnostics"]["mycua_session_id"], "sess-1")

    def test_task_envelope_includes_upstream_error_diagnostics(self):
        payload = {
            "task": {"task_id": "cua_task_1", "status": "error", "mycua_run_id": "run_1"},
            "upstream": {
                "status": "error",
                "error": "my-cua run_status request failed",
                "upstream_status": 404,
            },
        }

        envelope = cua._task_envelope(payload)

        self.assertEqual(envelope["outcome"], "failed")
        self.assertIsNone(envelope["result"]["text"])
        self.assertEqual(envelope["diagnostics"]["error"], "my-cua run_status request failed")
        self.assertEqual(envelope["diagnostics"]["upstream_status"], 404)

    def test_task_envelope_preserves_structured_error_diagnostics(self):
        payload = {
            "request_id": "req-1",
            "task": {"task_id": "task-1", "status": "error", "mycua_run_id": "run-1"},
            "upstream": {
                "status": "error",
                "error": "run start failed",
                "reason": "A run is already active for this desktop.",
                "upstream_code": "active_run_conflict",
                "upstream_status": 409,
                "context": {"active_run_id": "run-active", "desktop_id": "desk-1"},
            },
        }

        diagnostics = cua._task_envelope(payload)["diagnostics"]

        self.assertEqual(diagnostics["error"], "run start failed")
        self.assertEqual(diagnostics["reason"], "A run is already active for this desktop.")
        self.assertEqual(diagnostics["upstream_code"], "active_run_conflict")
        self.assertEqual(diagnostics["upstream_status"], 409)
        self.assertEqual(diagnostics["request_id"], "req-1")
        self.assertEqual(diagnostics["context"]["active_run_id"], "run-active")

    def test_task_envelope_maps_terminal_model_rate_limit(self):
        payload = {
            "task": {"task_id": "task-1", "status": "failed", "mycua_run_id": "run-1"},
            "upstream": {
                "status": "failed",
                "error": {
                    "code": "model_rate_limited",
                    "message": "model provider rate limited the request",
                    "source": "model_provider",
                    "stage": "model_execute",
                    "accepted": True,
                    "retryable": True,
                    "upstream_status": 429,
                    "upstream_code": "ModelAccountTpmRateLimitExceeded",
                    "retry_after_ms": 3000,
                },
            },
        }

        envelope = cua._task_envelope(payload)
        failure = envelope["result"]["error"]

        self.assertEqual(envelope["outcome"], "failed")
        self.assertEqual(failure["code"], "RATE_LIMITED")
        self.assertEqual(failure["source"], "model_provider")
        self.assertEqual(failure["stage"], "model_execute")
        self.assertTrue(failure["accepted"])
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["upstream_status"], 429)
        self.assertEqual(failure["upstream_code"], "ModelAccountTpmRateLimitExceeded")
        self.assertEqual(failure["retry_after_ms"], 3000)
        self.assertEqual(envelope["diagnostics"]["code"], "RATE_LIMITED")

    def test_task_envelope_contains_unknown_terminal_internal_code(self):
        payload = {
            "task": {"task_id": "task-1", "status": "failed", "mycua_run_id": "run-1"},
            "upstream": {
                "status": "failed",
                "error": {
                    "code": "v6_new_internal_failure",
                    "message": "request failed for https://internal.example?token=secret",
                },
            },
        }

        failure = cua._task_envelope(payload)["result"]["error"]

        self.assertEqual(failure["code"], "UPSTREAM_FAILURE")
        self.assertEqual(failure["upstream_code"], "v6_new_internal_failure")
        self.assertEqual(failure["source"], "my_cua")
        self.assertEqual(failure["stage"], "run_execute")
        self.assertTrue(failure["accepted"])
        self.assertFalse(failure["retryable"])
        self.assertEqual(failure["message"], "CUA failed with an internal upstream error")
        self.assertNotIn("secret", json.dumps(failure))

    def test_task_envelope_does_not_treat_model_auth_as_bytesso_auth(self):
        payload = {
            "task": {"task_id": "task-1", "status": "failed", "mycua_run_id": "run-1"},
            "upstream": {
                "status": "failed",
                "error": {
                    "code": "model_auth_failed",
                    "message": "model provider rejected the credential",
                    "source": "model_provider",
                    "upstream_status": 401,
                    "upstream_code": "InvalidAuthentication",
                },
            },
        }

        failure = cua._task_envelope(payload)["result"]["error"]

        self.assertEqual(failure["code"], "UPSTREAM_FAILURE")
        self.assertEqual(failure["source"], "model_provider")
        self.assertEqual(failure["upstream_code"], "InvalidAuthentication")
        self.assertEqual(failure["message"], "CUA failed with an internal upstream error")

    def test_task_envelope_normalizes_artifacts(self):
        payload = {
            "task_id": "cua_task_1",
            "status": "succeeded",
            "upstream": {
                "finalText": "done",
                "artifacts": [
                    {
                        "id": "art_image",
                        "kind": "screenshot",
                        "mimeType": "image/png",
                        "url": "/api/artifacts/art_image",
                        "width": 100,
                        "height": 80,
                        "sizeBytes": 1234,
                        "storageStatus": "ready",
                        "meta": {"title": "screen"},
                    },
                    {
                        "id": "art_text",
                        "kind": "file",
                        "mimeType": "text/markdown",
                        "filePath": "C:/Users/user/Desktop/result.md",
                        "contentText": "# Result",
                    },
                ],
                "output": {
                    "artifacts": [
                        {
                            "id": "art_file",
                            "kind": "file",
                            "mimeType": "application/pdf",
                            "name": "report.pdf",
                            "downloadUrl": "/api/artifacts/art_file",
                        }
                    ]
                },
            },
        }

        artifacts = cua._task_envelope(payload)["result"]["artifacts"]

        self.assertEqual(len(artifacts), 3)
        self.assertEqual(artifacts[0]["type"], "image")
        self.assertEqual(artifacts[0]["mime_type"], "image/png")
        self.assertEqual(artifacts[0]["size_bytes"], 1234)
        self.assertEqual(artifacts[0]["title"], "screen")
        self.assertEqual(artifacts[1]["type"], "text")
        self.assertEqual(artifacts[1]["path"], "C:/Users/user/Desktop/result.md")
        self.assertEqual(artifacts[1]["text"], "# Result")
        self.assertEqual(artifacts[2]["type"], "file")
        self.assertEqual(artifacts[2]["name"], "report.pdf")
        self.assertEqual(artifacts[2]["url"], "/api/artifacts/art_file")


if __name__ == "__main__":
    unittest.main()
