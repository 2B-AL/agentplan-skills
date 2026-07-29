import sys
import unittest
from pathlib import Path
from urllib.error import URLError
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import cua_http  # noqa: E402
from cua_util import SkillError  # noqa: E402


class CuaHttpTests(unittest.TestCase):
    def test_structured_desktop_busy_error_keeps_diagnostics(self):
        envelope = {
            "ok": False,
            "request_id": "req-1",
            "error": {
                "code": "DESKTOP_BUSY",
                "message": "The desktop already has an active run.",
                "reason": "active desktop execution",
                "upstream_code": "active_run_conflict",
                "upstream_status": 409,
                "retryable": False,
                "context": {"active_run_id": "run-1", "desktop_id": "desk-1"},
            },
        }

        with self.assertRaises(SkillError) as ctx:
            cua_http._tool_result(envelope)

        self.assertEqual(ctx.exception.code, "DESKTOP_BUSY")
        self.assertEqual(ctx.exception.extra["request_id"], "req-1")
        self.assertEqual(ctx.exception.extra["upstream_code"], "active_run_conflict")
        self.assertEqual(ctx.exception.extra["upstream_status"], 409)
        self.assertEqual(ctx.exception.extra["context"]["active_run_id"], "run-1")

    def test_unknown_http_conflict_is_not_misreported_as_unbound_desktop(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._raise_mapped_error(None, 409, "conflict")

        self.assertEqual(ctx.exception.code, "CONFLICT")

    def test_success_result_carries_gateway_request_id(self):
        result = cua_http._tool_result({"ok": True, "request_id": "req-1", "result": {"status": "running"}})

        self.assertEqual(result["request_id"], "req-1")

    def test_operation_not_owned_maps_to_forbidden(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._tool_result(
                {
                    "ok": False,
                    "error": {
                        "code": "OperationNotOwned",
                        "message": "operation is not owned by caller",
                    },
                }
            )

        self.assertEqual(ctx.exception.code, "FORBIDDEN")

    def test_service_timeout_keeps_stage_source_and_unknown_acceptance(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._tool_result(
                {
                    "ok": False,
                    "error": {
                        "code": "UPSTREAM_TIMEOUT",
                        "message": "my-cua request timed out",
                        "source": "my_cua",
                        "stage": "run_create",
                        "accepted": "unknown",
                        "request_id": "req-timeout",
                        "upstream_code": "UpstreamTimeout",
                        "upstream_status": 504,
                        "retryable": True,
                        "context": {"task_id": "task-1", "session_id": "session-1"},
                    },
                },
                tool_name="cua_run_task",
            )

        self.assertEqual(ctx.exception.code, "UPSTREAM_TIMEOUT")
        self.assertEqual(ctx.exception.extra["source"], "my_cua")
        self.assertEqual(ctx.exception.extra["stage"], "run_create")
        self.assertEqual(ctx.exception.extra["accepted"], "unknown")
        self.assertEqual(ctx.exception.extra["context"]["task_id"], "task-1")

    def test_rate_limit_keeps_retry_after(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._tool_result(
                {
                    "ok": False,
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "model provider rate limited the request",
                        "source": "model_provider",
                        "stage": "model_call",
                        "accepted": True,
                        "retryable": True,
                        "retry_after_ms": 2500,
                    },
                },
                tool_name="cua_get_task_result",
            )

        self.assertEqual(ctx.exception.code, "RATE_LIMITED")
        self.assertEqual(ctx.exception.extra["retry_after_ms"], 2500)
        self.assertEqual(ctx.exception.extra["source"], "model_provider")

    def test_unknown_upstream_code_is_not_exposed_as_public_code(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._raise_mapped_error(
                "provider_internal_4297",
                502,
                "upstream failed",
                tool_name="cua_run_task",
            )

        self.assertEqual(ctx.exception.code, "UPSTREAM_FAILURE")
        self.assertEqual(ctx.exception.extra["upstream_code"], "provider_internal_4297")

    def test_gateway_timeout_on_task_submission_has_unknown_acceptance(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._raise_http_error(504, b"", tool_name="cua_run_task")

        self.assertEqual(ctx.exception.code, "GATEWAY_TIMEOUT")
        self.assertEqual(ctx.exception.extra["source"], "skill_gateway")
        self.assertEqual(ctx.exception.extra["stage"], "run_create")
        self.assertEqual(ctx.exception.extra["accepted"], "unknown")

    def test_bad_request_maps_to_validation_error(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._raise_mapped_error("BadRequest", 400, "input is required", tool_name="cua_run_task")

        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")
        self.assertEqual(ctx.exception.extra["accepted"], False)

    def test_active_task_maps_to_desktop_busy(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._raise_mapped_error(
                "ActiveTaskRunning",
                409,
                "desktop has an active task",
                tool_name="cua_run_task",
            )

        self.assertEqual(ctx.exception.code, "DESKTOP_BUSY")
        self.assertEqual(ctx.exception.extra["upstream_code"], "ActiveTaskRunning")

    def test_non_json_gateway_response_maps_to_protocol_error_without_raw_body(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._decode_json(b"<html>internal proxy page</html>", tool_name="cua_run_task")

        self.assertEqual(ctx.exception.code, "UPSTREAM_PROTOCOL_ERROR")
        self.assertNotIn("internal proxy page", ctx.exception.message)
        self.assertEqual(ctx.exception.extra["accepted"], "unknown")

    def test_url_error_timeout_maps_to_gateway_timeout(self):
        with (
            mock.patch.object(cua_http, "urlopen", side_effect=URLError(TimeoutError("timed out"))),
            self.assertRaises(SkillError) as ctx,
        ):
            cua_http.gateway_tool_call(
                "https://skill.example",
                "token",
                "cua_run_task",
                {"input": "do work"},
            )

        self.assertEqual(ctx.exception.code, "GATEWAY_TIMEOUT")
        self.assertEqual(ctx.exception.extra["accepted"], "unknown")

    def test_unknown_auth_code_uses_http_status_and_keeps_upstream_code(self):
        with self.assertRaises(SkillError) as ctx:
            cua_http._raise_mapped_error(
                "invalid_subject_token",
                401,
                "invalid token",
                tool_name="cua_run_task",
            )

        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
        self.assertEqual(ctx.exception.extra["upstream_code"], "invalid_subject_token")


if __name__ == "__main__":
    unittest.main()
