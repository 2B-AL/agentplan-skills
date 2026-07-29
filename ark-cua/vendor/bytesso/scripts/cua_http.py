"""AP-style HTTP client for the CUA Skill Gateway.

Stdlib only. The client talks to:

- GET  /skill/manifest
- POST /skill/tools/{tool}

Tool errors are converted into stable SkillError codes used by the CLI.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cua_util import SkillError, login_retry_command

DEFAULT_TIMEOUT_SEC = 120

ERROR_SCHEMA_VERSION = "cua.error.v1"

PUBLIC_ERROR_CODES = frozenset(
    {
        "AUTH_REQUIRED",
        "FORBIDDEN",
        "RATE_LIMITED",
        "SCHEDULING_UNAVAILABLE",
        "VALIDATION_ERROR",
        "TOOL_NOT_FOUND",
        "DESKTOP_NOT_BOUND",
        "DESKTOP_NOT_READY",
        "DESKTOP_BUSY",
        "DESKTOP_REBOOT_IN_PROGRESS",
        "DESKTOP_REBOOT_FAILED",
        "DESKTOP_UNHEALTHY",
        "QUOTA_EXCEEDED",
        "NO_CAPACITY",
        "INVOCATION_NOT_FOUND",
        "TASK_NOT_STARTED",
        "INVOCATION_NOT_WAITING_INPUT",
        "SESSION_NOT_FOUND",
        "SESSION_MISMATCH",
        "SESSION_CLEANUP",
        "REQUEST_CANCELLED",
        "NETWORK",
        "GATEWAY_TIMEOUT",
        "UPSTREAM_TIMEOUT",
        "MODEL_TIMEOUT",
        "CUA_BACKEND_UNAVAILABLE",
        "UPSTREAM_PROTOCOL_ERROR",
        "UPSTREAM_FAILURE",
        "CONFLICT",
        "INTERNAL",
    }
)


def gateway_manifest(gateway_url, timeout=30):
    req = Request(
        _join(gateway_url, "/skill/manifest"),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return _decode_json(resp.read(), tool_name="manifest")
    except HTTPError as exc:
        body = exc.read()
        _raise_http_error(exc.code, body, tool_name="manifest")
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise SkillError(
                "GATEWAY_TIMEOUT",
                f"Request to CUA Skill Gateway timed out: {gateway_url}",
                **_local_transport_details("manifest", accepted=False),
            )
        raise SkillError(
            "NETWORK",
            f"Cannot reach CUA Skill Gateway at {gateway_url}: {exc.reason}",
            **_local_transport_details("manifest", accepted=False),
        )
    except TimeoutError:
        raise SkillError(
            "GATEWAY_TIMEOUT",
            f"Request to CUA Skill Gateway timed out: {gateway_url}",
            **_local_transport_details("manifest", accepted=False),
        )


def gateway_tool_call(gateway_url, bearer_key, tool_name, arguments=None, timeout=DEFAULT_TIMEOUT_SEC):
    payload = arguments or {}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if bearer_key:
        headers["Authorization"] = "Bearer " + bearer_key
    req = Request(
        _join(gateway_url, f"/skill/tools/{tool_name}"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            envelope = _decode_json(resp.read(), tool_name=tool_name)
            return _tool_result(envelope, tool_name=tool_name)
    except HTTPError as exc:
        body = exc.read()
        _raise_http_error(exc.code, body, tool_name=tool_name)
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise SkillError(
                "GATEWAY_TIMEOUT",
                f"Request to CUA Skill Gateway timed out: {gateway_url}",
                **_local_transport_details(tool_name, accepted=_transport_acceptance(tool_name)),
            )
        raise SkillError(
            "NETWORK",
            f"Cannot reach CUA Skill Gateway at {gateway_url}: {exc.reason}",
            **_local_transport_details(tool_name, accepted=_transport_acceptance(tool_name)),
        )
    except TimeoutError:
        raise SkillError(
            "GATEWAY_TIMEOUT",
            f"Request to CUA Skill Gateway timed out: {gateway_url}",
            **_local_transport_details(tool_name, accepted=_transport_acceptance(tool_name)),
        )


def gateway_private_call(gateway_url, bearer_key, path, arguments=None, timeout=DEFAULT_TIMEOUT_SEC):
    """Call a fixed private CLI endpoint without exposing its response.

    This is intentionally separate from the public Tool surface.  The
    credential relay endpoints may carry an encrypted HPKE envelope that must
    flow only from the Gateway response body into credential-agent stdin.
    Callers must never return this value from the CLI.
    """
    if not isinstance(path, str) or not path.startswith("/skill/credential-relay/"):
        raise SkillError("VALIDATION_ERROR", "Unsupported private CUA Skill Gateway path.")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if bearer_key:
        headers["Authorization"] = "Bearer " + bearer_key
    req = Request(
        _join(gateway_url, path),
        data=json.dumps(arguments or {}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            envelope = _decode_json(resp.read(), tool_name="credential_relay")
    except HTTPError as exc:
        _raise_http_error(exc.code, exc.read(), tool_name="credential_relay")
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise SkillError("GATEWAY_TIMEOUT", "Credential relay request timed out.", accepted="unknown")
        raise SkillError("NETWORK", f"Cannot reach CUA Skill Gateway: {exc.reason}", accepted="unknown")
    except TimeoutError:
        raise SkillError("GATEWAY_TIMEOUT", "Credential relay request timed out.", accepted="unknown")
    if envelope.get("ok") is not True or not isinstance(envelope.get("result"), dict):
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        _raise_mapped_error(
            error.get("code") or "UPSTREAM_PROTOCOL_ERROR",
            error.get("upstream_status"),
            error.get("message") or "Credential relay failed.",
            tool_name="credential_relay",
            **_error_details(envelope, error),
        )
    return envelope["result"]


def _tool_result(envelope, tool_name=None):
    if not isinstance(envelope, dict):
        raise SkillError(
            "UPSTREAM_PROTOCOL_ERROR",
            "CUA Skill Gateway returned a non-object response.",
            error_schema_version=ERROR_SCHEMA_VERSION,
            source="skill_gateway",
            stage=_stage_for_tool(tool_name),
            accepted=_transport_acceptance(tool_name),
            retryable=True,
        )
    if envelope.get("ok") is True:
        result = envelope.get("result")
        if not isinstance(result, dict):
            return {}
        result = dict(result)
        if envelope.get("request_id") and not result.get("request_id"):
            result["request_id"] = envelope.get("request_id")
        return result
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    _raise_mapped_error(
        error.get("code"),
        error.get("upstream_status"),
        error.get("message"),
        tool_name=tool_name,
        **_error_details(envelope, error),
    )


def _raise_http_error(status, body, tool_name=None):
    text = body.decode("utf-8", errors="replace") if body else ""
    message = text[:300] or f"HTTP {status}"
    try:
        payload = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or message
            _raise_mapped_error(
                error.get("code"),
                error.get("upstream_status") or status,
                message,
                tool_name=tool_name,
                **_error_details(payload, error),
            )
        _raise_mapped_error(
            payload.get("code") or payload.get("error"),
            status,
            payload.get("message") or message,
            tool_name=tool_name,
            **_error_details(payload, {}),
        )
    _raise_mapped_error(None, status, message, tool_name=tool_name, upstream_status=status)


def _raise_mapped_error(code, status, message, tool_name=None, **details):
    stable = str(code or "").strip()
    msg = message or "CUA Skill Gateway request failed."
    if status is not None:
        details.setdefault("upstream_status", status)
    details.setdefault("error_schema_version", ERROR_SCHEMA_VERSION)
    details.setdefault("stage", _stage_for_tool(tool_name))
    details.setdefault("accepted", _default_acceptance(tool_name, status))
    if stable in ("Unauthorized", "AUTH_REQUIRED", "TOKEN_EXPIRED", "REFRESH_FAILED"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("AUTH_REQUIRED", msg, retry_command=login_retry_command(), **details)
    if stable in ("BadRequest", "VALIDATION_ERROR"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("VALIDATION_ERROR", msg, **details)
    if stable in ("ToolNotFound", "TOOL_NOT_FOUND"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("TOOL_NOT_FOUND", msg, **details)
    if stable == "TaskNotOwned":
        details.setdefault("source", "skill_gateway")
        raise SkillError("INVOCATION_NOT_FOUND", msg, **details)
    if stable == "TaskNotStarted":
        details.setdefault("source", "skill_gateway")
        raise SkillError("TASK_NOT_STARTED", msg, **details)
    if stable in ("DesktopNotOwned", "OperationNotOwned"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("FORBIDDEN", msg, **details)
    if stable in ("Forbidden", "FORBIDDEN"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("FORBIDDEN", msg, **details)
    if stable == "no_active_cua_allocation":
        details.setdefault("source", "access_hub")
        raise SkillError("DESKTOP_NOT_BOUND", msg, **details)
    if stable == "DesktopNotReady":
        details.setdefault("source", "desktop_runtime")
        raise SkillError("DESKTOP_NOT_READY", msg, **details)
    if stable in ("DESKTOP_BUSY", "active_run_conflict"):
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "desktop_runtime")
        raise SkillError("DESKTOP_BUSY", msg, **details)
    if stable == "ActiveTaskRunning":
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "desktop_runtime")
        raise SkillError("DESKTOP_BUSY", msg, **details)
    if stable in ("DesktopRestarting", "DESKTOP_REBOOT_IN_PROGRESS"):
        details.setdefault("source", "desktop_runtime")
        raise SkillError("DESKTOP_REBOOT_IN_PROGRESS", msg, **details)
    if stable in ("DesktopRebootFailed", "DESKTOP_REBOOT_FAILED"):
        details.setdefault("source", "desktop_runtime")
        raise SkillError("DESKTOP_REBOOT_FAILED", msg, **details)
    if stable in ("QuotaExceeded", "QUOTA_EXCEEDED"):
        details.setdefault("source", "access_hub")
        raise SkillError("QUOTA_EXCEEDED", msg, **details)
    if stable in ("NoCapacity", "NO_CAPACITY"):
        details.setdefault("source", "access_hub")
        raise SkillError("NO_CAPACITY", msg, **details)
    if stable in ("AccessHubUnavailable", "MyCUAUnavailable"):
        details.setdefault("source", "access_hub" if stable == "AccessHubUnavailable" else "my_cua")
        raise SkillError("CUA_BACKEND_UNAVAILABLE", msg, **details)
    if stable in ("GATEWAY_5XX", "CUA_BACKEND_UNAVAILABLE"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("CUA_BACKEND_UNAVAILABLE", msg, **details)
    if stable in ("UPSTREAM_TIMEOUT", "UpstreamTimeout"):
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "my_cua")
        raise SkillError("UPSTREAM_TIMEOUT", msg, **details)
    if stable in ("InvalidAccessHubResponse", "InvalidMyCUAResponse", "UPSTREAM_PROTOCOL_ERROR"):
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "access_hub" if stable == "InvalidAccessHubResponse" else "my_cua")
        raise SkillError("UPSTREAM_PROTOCOL_ERROR", msg, **details)
    if stable in ("MODEL_TIMEOUT", "model_timeout", "provider_timeout", "llm_timeout"):
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "model_provider")
        raise SkillError("MODEL_TIMEOUT", msg, **details)
    if stable in ("DESKTOP_UNHEALTHY", "desktop_unhealthy", "guest_unhealthy"):
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "desktop_runtime")
        raise SkillError("DESKTOP_UNHEALTHY", msg, **details)
    if stable in ("SESSION_CLEANUP", "session_cleanup", "session_cleanup_failed"):
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "my_cua")
        raise SkillError("SESSION_CLEANUP", msg, **details)
    if stable in ("RequestCancelled", "REQUEST_CANCELLED"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("REQUEST_CANCELLED", msg, **details)
    if stable in ("SessionNotFound", "SESSION_NOT_FOUND"):
        details.setdefault("source", "my_cua")
        raise SkillError("SESSION_NOT_FOUND", msg, **details)
    if stable in ("SessionMismatch", "SESSION_MISMATCH"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("SESSION_MISMATCH", msg, **details)
    if stable in ("TaskNotWaitingInput", "INVOCATION_NOT_WAITING_INPUT"):
        details.setdefault("source", "my_cua")
        raise SkillError("INVOCATION_NOT_WAITING_INPUT", msg, **details)
    if stable in ("RateLimited", "RATE_LIMITED"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("RATE_LIMITED", msg, **details)
    if stable in ("BrokerNotReady", "UIANotReady", "SpiceAgentNotReady"):
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "desktop_runtime")
        raise SkillError("DESKTOP_UNHEALTHY", msg, **details)
    if stable in PUBLIC_ERROR_CODES:
        details.setdefault("source", _source_for_public_code(stable))
        raise SkillError(stable, msg, **details)
    if stable and status in (400, "400", 401, "401", 403, "403", 404, "404", 409, "409", 429, "429"):
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "skill_gateway")
        if status in (400, "400"):
            raise SkillError("VALIDATION_ERROR", msg, **details)
        if status in (401, "401"):
            raise SkillError("AUTH_REQUIRED", msg, retry_command=login_retry_command(), **details)
        if status in (403, "403"):
            raise SkillError("FORBIDDEN", msg, **details)
        if status in (404, "404"):
            raise SkillError("INVOCATION_NOT_FOUND", msg, **details)
        if status in (409, "409"):
            raise SkillError("CONFLICT", msg, **details)
        raise SkillError("RATE_LIMITED", msg, **details)
    if stable:
        details.setdefault("upstream_code", stable)
        details.setdefault("source", "my_cua")
        raise SkillError("UPSTREAM_FAILURE", msg, **details)

    if status in (400, "400"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("VALIDATION_ERROR", msg, **details)
    if status in (401, "401"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("AUTH_REQUIRED", msg or "Login required for CUA Skill.", retry_command=login_retry_command(), **details)
    if status in (403, "403"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("FORBIDDEN", msg or "CUA Skill credential is forbidden.", **details)
    if status in (404, "404"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("INVOCATION_NOT_FOUND", msg or "CUA invocation was not found.", **details)
    if status in (409, "409"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("CONFLICT", msg, **details)
    if status in (429, "429"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("RATE_LIMITED", msg or "CUA Skill Gateway rate limited the request.", **details)
    if status in (502, 503, "502", "503"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("CUA_BACKEND_UNAVAILABLE", msg or "CUA backend is unavailable.", **details)
    if status in (504, "504"):
        details.setdefault("source", "skill_gateway")
        raise SkillError("GATEWAY_TIMEOUT", msg or "CUA Skill Gateway timed out; the task may still be running.", **details)
    details.setdefault("source", "skill_gateway")
    raise SkillError("INTERNAL", msg or "CUA Skill Gateway returned an unexpected error.", **details)


def _error_details(envelope, error):
    details = {}
    request_id = error.get("request_id") or envelope.get("request_id")
    if request_id:
        details["request_id"] = request_id
    for key in (
        "reason",
        "retryable",
        "retry_after_ms",
        "upstream_code",
        "upstream_status",
        "error_schema_version",
        "source",
        "stage",
        "accepted",
    ):
        value = error.get(key)
        if value is not None and value != "":
            details[key] = value
    context = error.get("context")
    if isinstance(context, dict) and context:
        details["context"] = context
    return details


def _stage_for_tool(tool_name):
    stages = {
        "manifest": "tool_discovery",
        "cua_list_desktops": "desktop_resolve",
        "cua_allocate_desktop": "desktop_allocate",
        "cua_get_desktop_access": "desktop_access",
        "cua_take_screenshot": "desktop_access",
        "cua_reboot_desktop": "desktop_reboot",
        "cua_reset_desktop": "desktop_reboot",
        "cua_get_desktop_operation": "desktop_reboot",
        "cua_run_task": "run_create",
        "cua_list_tasks": "task_watch",
        "cua_watch_tasks": "task_watch",
        "cua_wait_task": "task_watch",
        "cua_get_task": "task_watch",
        "cua_get_task_result": "result_fetch",
        "cua_resume_task": "task_resume",
        "cua_cancel_task": "task_cancel",
    }
    return stages.get(tool_name, "unknown")


def _transport_acceptance(tool_name):
    if tool_name in {
        "cua_allocate_desktop",
        "cua_reboot_desktop",
        "cua_reset_desktop",
        "cua_run_task",
        "cua_resume_task",
        "cua_cancel_task",
    }:
        return "unknown"
    return False


def _default_acceptance(tool_name, status):
    if status in (502, 503, 504, "502", "503", "504"):
        return _transport_acceptance(tool_name)
    return False


def _local_transport_details(tool_name, accepted):
    return {
        "error_schema_version": ERROR_SCHEMA_VERSION,
        "source": "network",
        "stage": _stage_for_tool(tool_name),
        "accepted": accepted,
        "retryable": True,
    }


def _source_for_public_code(code):
    if code == "MODEL_TIMEOUT":
        return "model_provider"
    if code.startswith("DESKTOP_"):
        return "desktop_runtime"
    if code.startswith("SESSION_"):
        return "my_cua"
    if code in ("NETWORK", "GATEWAY_TIMEOUT"):
        return "network"
    if code in ("UPSTREAM_TIMEOUT", "UPSTREAM_PROTOCOL_ERROR", "UPSTREAM_FAILURE"):
        return "my_cua"
    return "skill_gateway"


def _decode_json(raw, tool_name=None):
    text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise SkillError(
            "UPSTREAM_PROTOCOL_ERROR",
            "CUA Skill Gateway returned a non-JSON response.",
            error_schema_version=ERROR_SCHEMA_VERSION,
            source="skill_gateway",
            stage=_stage_for_tool(tool_name),
            accepted=_transport_acceptance(tool_name),
            retryable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise SkillError(
            "UPSTREAM_PROTOCOL_ERROR",
            "CUA Skill Gateway JSON response was not an object.",
            error_schema_version=ERROR_SCHEMA_VERSION,
            source="skill_gateway",
            stage=_stage_for_tool(tool_name),
            accepted=_transport_acceptance(tool_name),
            retryable=True,
        )
    return payload


def _join(base, path):
    return base.rstrip("/") + "/" + path.lstrip("/")
