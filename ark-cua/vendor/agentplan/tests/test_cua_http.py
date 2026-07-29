import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import cua_http  # noqa: E402
from cua_util import SkillError  # noqa: E402


class CuaHttpTests(unittest.TestCase):
    def test_success_envelope_preserves_request_id_in_diagnostics(self):
        with mock.patch.object(
            cua_http,
            "request",
            return_value=(200, {"ok": True, "request_id": "req-1", "data": {"outcome": "failed"}}),
        ):
            data = cua_http.gateway_call("GET", "http://gateway", "/v1/invocations/task-1")

        self.assertEqual(data["request_id"], "req-1")
        self.assertEqual(data["diagnostics"]["request_id"], "req-1")

    def test_gateway_error_preserves_request_and_upstream_diagnostics(self):
        payload = {
            "ok": False,
            "request_id": "req-1",
            "error": {
                "code": "MODEL_TIMEOUT",
                "message": "model provider timed out",
                "reason": "provider deadline exceeded",
                "upstream_code": "provider_timeout",
                "upstream_status": 504,
                "retryable": True,
            },
        }

        with self.assertRaises(SkillError) as ctx:
            cua_http._raise_gateway_error(504, payload)

        self.assertEqual(ctx.exception.code, "MODEL_TIMEOUT")
        self.assertEqual(ctx.exception.extra["request_id"], "req-1")
        self.assertEqual(ctx.exception.extra["upstream_code"], "provider_timeout")
        self.assertEqual(ctx.exception.extra["upstream_status"], 504)


if __name__ == "__main__":
    unittest.main()
