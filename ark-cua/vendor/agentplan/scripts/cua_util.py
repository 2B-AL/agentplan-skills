"""Shared helpers for the AgentPlan CUA Skill CLI.

Stdlib only. Provides the unified JSON output contract and a structured error
type. The CLI NEVER prints AgentPlan API keys, desktop access tokens,
the user's objective, the user's answers, CUA's final result text, or screenshot
bytes onto stdout/stderr; only invocation metadata, outcome, user email, and
login status.
"""

import json
import os
import sys
from datetime import datetime, timezone

# Errors that are safe to retry transparently: gateway/upstream timeouts,
# backend hiccups, rate limits, and local network blips. The CLI keeps polling
# on these instead of failing a whole task on a single 504.
RETRYABLE_ERROR_CODES = frozenset(
    {"GATEWAY_TIMEOUT", "CUA_BACKEND_UNAVAILABLE", "RATE_LIMITED", "NETWORK"}
)


class SkillError(Exception):
    """An error that maps to the unified JSON error envelope.

    `code` follows the gateway error codes (AUTH_REQUIRED, TOKEN_EXPIRED,
    REFRESH_FAILED, FORBIDDEN, DESKTOP_NOT_BOUND, INVOCATION_NOT_FOUND,
    INVOCATION_NOT_WAITING_INPUT, ACTIVE_RUN_CONFLICT, MODEL_TIMEOUT,
    DESKTOP_UNHEALTHY, SESSION_CLEANUP, UPSTREAM_FAILURE,
    CUA_BACKEND_UNAVAILABLE, RATE_LIMITED, VALIDATION_ERROR, NETWORK, INTERNAL).
    """

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}


def emit_success(action, data=None):
    """Print a single-line JSON success envelope and exit 0."""
    payload = {"ok": True, "action": action}
    if data:
        payload.update(data)
    _print(payload)
    sys.exit(0)


def emit_error(action, error):
    """Print a single-line JSON error envelope and exit non-zero."""
    if isinstance(error, SkillError):
        body = {"code": error.code, "message": error.message}
        body.update(error.extra)
    else:
        body = {"code": "INTERNAL", "message": str(error)}
    payload = {"ok": False, "action": action, "error": body}
    next_hint = _next_for_error(body)
    if next_hint:
        payload["next"] = next_hint
    _print(payload)
    sys.exit(1)


def _next_for_error(body):
    code = body.get("code")
    if _is_active_run_conflict(body):
        return {
            "agent_hint": "The new CUA task was not started because the cloud desktop already has an active task/run. "
            "Stop here for this user request: do not retry, do not call delegate/task run/task continue again, "
            "and do not probe with observe/diagnose/watch --last unless the user explicitly asks to inspect or cancel "
            "the existing task. Tell the user to wait until the current desktop task finishes, then try again.",
        }
    setup = body.get("setup_command")
    if code in ("AUTH_REQUIRED", "REFRESH_FAILED", "TOKEN_EXPIRED") and setup:
        return {
            "setup_command": setup,
            "agent_hint": "Do not run setup_command yourself. Ask the user to open a local terminal, "
            "run setup_command there, enter their AgentPlan API key in that terminal prompt, "
            "then retry the original command. Never ask the user to paste the API key into chat.",
        }
    retry = body.get("retry_command")
    if code in ("AUTH_REQUIRED", "REFRESH_FAILED") and retry:
        return {
            "setup_command": retry,
            "agent_hint": "Do not run retry_command yourself. Ask the user to open a local terminal, "
            "run it there, enter their AgentPlan API key in that terminal prompt, "
            "then retry the original command. Never ask the user to paste the API key into chat.",
        }
    if code == "TOKEN_EXPIRED" and retry:
        return {
            "setup_command": retry,
            "agent_hint": "Do not run retry_command yourself. Ask the user to run it in a local terminal, then retry the original command.",
        }
    if code == "SCHEDULING_UNAVAILABLE":
        return {
            "agent_hint": "This CUA backend does not support scheduled tasks. Do NOT retry with different "
            "arguments and do NOT fall back to any external scheduler or host automation. Tell the user "
            "scheduling is unavailable; if they want it now, run the goal once with `task run`/`delegate`.",
        }
    if code == "MODEL_TIMEOUT":
        return {
            "agent_hint": "The model provider timed out. Report error.reason and error.request_id. "
            "Retry only when the operation is safe and the user wants to try again.",
        }
    if code in ("DESKTOP_UNHEALTHY", "SESSION_CLEANUP", "UPSTREAM_FAILURE"):
        return {
            "agent_hint": "Do not retry blindly. Report error.reason, error.request_id, and any safe context "
            "so the owning service can diagnose the failure.",
        }
    if code in RETRYABLE_ERROR_CODES:
        return {
            "agent_hint": "Transient gateway/backend timeout — this is not a real failure. "
            "The task is likely still running. Just re-run the same command (for a long task, "
            "prefer `watch --last` or `result --last`).",
        }
    return None


def _is_active_run_conflict(body):
    code = str(body.get("code") or "").strip()
    if code in ("ACTIVE_RUN_CONFLICT", "active_run_conflict", "ActiveTaskRunning"):
        return True
    message = str(body.get("message") or "").lower()
    return code == "UpstreamError" and (
        "already active" in message
        or "active task" in message
        or "active run" in message
        or "desktop run is already active" in message
    )


def _print(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def now_epoch():
    return datetime.now(timezone.utc).timestamp()


def iso_to_epoch(value):
    """Parse an ISO-8601 timestamp (with optional trailing Z) to epoch seconds."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def epoch_to_iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def script_path():
    entrypoint = os.environ.get("ARK_CUA_ENTRYPOINT")
    if entrypoint:
        scheme = os.environ.get("ARK_CUA_ACTIVE_SCHEME")
        suffix = f" --auth-scheme {scheme}" if scheme else ""
        return os.path.abspath(entrypoint) + suffix
    return os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else "scripts/cua.py"


def login_retry_command():
    """Compatibility alias for older callers; agents should prefer setup_command."""
    return f"python3 {script_path()} auth login"


def login_setup_command():
    """The command the user should run in their own local terminal to login."""
    return f"python3 {script_path()} auth login"


# MIME type -> file extension, for artifact downloads that don't specify a name.
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/json": ".json",
    "application/zip": ".zip",
    "application/octet-stream": ".bin",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/markdown": ".md",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def ext_for_mime(mime_type):
    """Best-effort file extension for a MIME type. Defaults to .bin."""
    if not mime_type:
        return ".bin"
    base = mime_type.split(";", 1)[0].strip().lower()
    if base in _EXT_BY_MIME:
        return _EXT_BY_MIME[base]
    # Fall back to the subtype for unknown but well-formed types (e.g. image/heic -> .heic).
    if "/" in base:
        subtype = base.split("/", 1)[1]
        subtype = subtype.split("+", 1)[0]
        if subtype.isalnum():
            return "." + subtype
    return ".bin"


def validate_iso8601(value, field):
    """Validate an ISO-8601 timestamp string, returning it unchanged.

    Raises SkillError(VALIDATION_ERROR) on a malformed value so the agent gets a
    clear, actionable message instead of a backend rejection.
    """
    if not value:
        raise SkillError("VALIDATION_ERROR", f"{field} is required and must be an ISO-8601 timestamp.")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise SkillError(
            "VALIDATION_ERROR",
            f"{field}={value!r} is not a valid ISO-8601 timestamp "
            "(expected e.g. 2026-06-25T20:00:00Z or 2026-06-25T20:00:00+08:00).",
        )
    return value
