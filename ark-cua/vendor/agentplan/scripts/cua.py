#!/usr/bin/env python3
"""AgentPlan CUA Skill CLI — the single entrypoint an agent calls to drive CUA.

    python3 <skill_dir>/scripts/cua.py <command> [options]

Every invocation prints exactly one JSON object:

    {"ok": true,  "action": "<command>", "data": {...}, "next": {...}}
    {"ok": false, "action": "<command>", "error": {"code": "...", "message": "..."}}

Stdlib only. API keys, the user's objective, the user's answers, CUA's final text,
and screenshot bytes are never printed. See references/ for full command and
error documentation.
"""

import argparse
import base64
import json
import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import cua_auth
from cua_state import AuthState, SessionState
from cua_util import (
    RETRYABLE_ERROR_CODES,
    SkillError,
    emit_error,
    emit_success,
    ext_for_mime,
    login_setup_command,
    now_epoch,
    script_path,
    validate_iso8601,
)

TERMINAL_OUTCOMES = ("completed", "failed", "cancelled")
# Long tasks use repeated waits. Each request stays within the gateway's 60-second
# server limit while the CLI tracks the user's larger total wait budget.
DEFAULT_WATCH_WAIT_MS = 20000
RESULT_POLL_WAIT_MS = 20000
SERVER_WAIT_CHUNK_MS = 60000
IDEMPOTENT_RETRIES = 2


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    action = getattr(args, "action", None)
    if not action:
        parser.print_help(sys.stderr)
        return 2
    try:
        state = AuthState.load()
        session = SessionState.load()
        data = args.handler(args, state, session)
        emit_success(action, data)
    except SkillError as exc:
        emit_error(action, exc)
    except BrokenPipeError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface unexpected errors as JSON, not tracebacks
        emit_error(action, SkillError("INTERNAL", str(exc)))


# -- base URL --------------------------------------------------------------


def resolve_base_url(args, state, persist=False):
    base_url = (
        args.api_base_url
        or os.environ.get("AP_CUA_SKILL_API_BASE_URL")
        or os.environ.get("CUA_SKILL_API_BASE_URL")
        or state.api_base_url
        or bundled_base_url()
    )
    if not base_url:
        raise SkillError(
            "VALIDATION_ERROR",
            "No CUA gateway configured. Set api_base_url in the skill's config.json, "
            "pass --api-base-url, or set AP_CUA_SKILL_API_BASE_URL.",
        )
    base_url = base_url.rstrip("/")
    if persist and state.api_base_url != base_url:
        state.set_api_base_url(base_url)
    return base_url


def bundled_base_url():
    """Gateway URL shipped with the skill in config.json (publisher-set, once)."""
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if not cfg_path.exists():
            return None
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("api_base_url") if isinstance(data, dict) else None
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or url.startswith("<") or "REPLACE" in url or "example.com" in url:
        return None
    return url


# -- auth commands ---------------------------------------------------------


def cmd_auth_status(args, state, session):
    base_url = resolve_base_url(args, state)
    return {"data": cua_auth.auth_status(state, base_url)}


def cmd_auth_login(args, state, session):
    base_url = resolve_base_url(args, state, persist=True)
    return {"data": cua_auth.login(
        state, base_url,
        api_key=args.api_key,
        prompt=not args.no_prompt,
    )}


def cmd_auth_logout(args, state, session):
    base_url = resolve_base_url(args, state)
    return {"data": cua_auth.logout(state, base_url)}


# -- CUA commands ----------------------------------------------------------


def cmd_ping(args, state, session):
    base_url = resolve_base_url(args, state)
    return {"data": cua_auth.authorized_call(state, base_url, "GET", "/v1/ping", retries=IDEMPOTENT_RETRIES)}


def cmd_delegate(args, state, session):
    base_url = resolve_base_url(args, state)
    _validate_wait_ms(args.wait_ms)
    # Always create without a synchronous wait so the invocation id is captured
    # before any long polling. This prevents a timeout from causing a duplicate
    # task submission.
    body = {"objective": args.objective, "wait_ms": 0}
    envelope = cua_auth.authorized_call(
        state, base_url, "POST", "/v1/invocations", body=body, timeout=_call_timeout(0)
    )
    envelope = _wait_invocation_with_budget(
        state, base_url, envelope.get("invocation_id"), args.wait_ms, initial_envelope=envelope
    )
    return _envelope_result("delegate", envelope, session)


def cmd_watch(args, state, session):
    base_url = resolve_base_url(args, state)
    invocation_id = _resolve_invocation_id(args, session)
    envelope = _wait_invocation_with_budget(state, base_url, invocation_id, args.wait_ms)
    return _envelope_result("watch", envelope, session)


def cmd_answer(args, state, session):
    base_url = resolve_base_url(args, state)
    _validate_wait_ms(args.wait_ms)
    invocation_id = _resolve_invocation_id(args, session)
    body = {"answer": args.answer, "wait_ms": 0}
    envelope = cua_auth.authorized_call(
        state, base_url, "POST", f"/v1/invocations/{invocation_id}/answer",
        body=body, timeout=_call_timeout(0)
    )
    envelope = _wait_invocation_with_budget(
        state, base_url, invocation_id, args.wait_ms, initial_envelope=envelope
    )
    return _envelope_result("answer", envelope, session)


def cmd_cancel(args, state, session):
    base_url = resolve_base_url(args, state)
    invocation_id = _resolve_invocation_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "POST", f"/v1/invocations/{invocation_id}/cancel", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_result(args, state, session):
    base_url = resolve_base_url(args, state)
    invocation_id = _resolve_invocation_id(args, session)
    deadline = now_epoch() + max(1, args.timeout)
    envelope = None
    while now_epoch() < deadline:
        try:
            if envelope is None:
                envelope = cua_auth.authorized_call(
                    state, base_url, "GET", f"/v1/invocations/{invocation_id}", retries=IDEMPOTENT_RETRIES
                )
            if envelope.get("outcome") != "in_progress":
                break
            envelope = cua_auth.authorized_call(
                state, base_url, "POST", f"/v1/invocations/{invocation_id}/watch",
                body={"wait_ms": RESULT_POLL_WAIT_MS}, timeout=_call_timeout(RESULT_POLL_WAIT_MS),
                retries=IDEMPOTENT_RETRIES
            )
        except SkillError as exc:
            # Transient gateway/backend timeout — the task is still running; keep polling.
            if exc.code in RETRYABLE_ERROR_CODES:
                envelope = None
                time.sleep(2)
                continue
            raise
    if envelope is None:
        # Could not reach a state read within the deadline; report in_progress.
        envelope = cua_auth.authorized_call(
            state, base_url, "GET", f"/v1/invocations/{invocation_id}", retries=IDEMPOTENT_RETRIES
        )
    return _envelope_result("result", envelope, session)


def cmd_observe(args, state, session):
    base_url = resolve_base_url(args, state)
    invocation_id = args.invocation_id or (session.last_invocation_id if args.last else None)
    leaf = "screenshot" if args.include_screenshot else "access"
    if invocation_id:
        path = f"/v1/invocations/{invocation_id}/desktop/{leaf}"
    else:
        path = f"/v1/desktop/{leaf}"
    data = cua_auth.authorized_call(state, base_url, "GET", path, timeout=120, retries=IDEMPOTENT_RETRIES)

    if args.include_screenshot:
        screenshot = data.get("screenshot") or {}
        b64 = screenshot.pop("base64", None)
        if b64:
            screenshot_file = _save_screenshot(b64, screenshot.get("mime_type"))
            data["screenshot_file"] = screenshot_file
            data["screenshot"] = screenshot

    # Older gateways return a bare cloud-desktop URL while newer multi-desktop
    # gateways return the canonical `/desktops/{id}/cua-app/` entry directly.
    # Decorate the response without changing the gateway-owned path namespace.
    access_url = data.get("access_url")
    if access_url:
        desktop_view_url, full_interface_url = _derive_desktop_urls(access_url)
        if desktop_view_url:
            data["desktop_view_url"] = desktop_view_url
        if full_interface_url:
            data["full_interface_url"] = full_interface_url

    if data.get("desktop_view_url"):
        link_hint = (
            "`desktop_view_url` shows just the desktop; `full_interface_url` "
            "shows the full CUA interface with the agent panel. "
        )
    else:
        link_hint = (
            "`access_url` and `full_interface_url` point to the gateway-owned "
            "full CUA interface. "
        )
    return {"data": data, "next": {
        "agent_hint": "Temporary cloud-desktop links; if one expires, run observe again. "
                      + link_hint
                      + "Offer `full_interface_url` when the user wants to watch what CUA "
                      "is doing. Do not use observe to decide whether the task is done — "
                      "use watch.",
    }}


# -- semantic commands -----------------------------------------------------


def cmd_diagnose(args, state, session):
    base_url = resolve_base_url(args, state)
    data = cua_auth.authorized_call(state, base_url, "GET", "/v1/diagnostics", retries=IDEMPOTENT_RETRIES)
    return {"data": data}


def cmd_desktop_list(args, state, session):
    base_url = resolve_base_url(args, state)
    data = cua_auth.authorized_call(state, base_url, "GET", "/v1/desktop-options", retries=IDEMPOTENT_RETRIES)
    return {"data": data}


def cmd_desktop_access(args, state, session):
    base_url = resolve_base_url(args, state)
    data = cua_auth.authorized_call(state, base_url, "GET", "/v1/desktop/access", timeout=120, retries=IDEMPOTENT_RETRIES)
    access_url = data.get("access_url")
    if access_url:
        desktop_view_url, full_interface_url = _derive_desktop_urls(access_url)
        if desktop_view_url:
            data["desktop_view_url"] = desktop_view_url
        if full_interface_url:
            data["full_interface_url"] = full_interface_url
    return {"data": data, "next": {
        "agent_hint": "Temporary desktop access URL returned. If it expires, run desktop access again.",
    }}


def cmd_desktop_revoke_access(args, state, session):
    base_url = resolve_base_url(args, state)
    body = {}
    if args.ticket:
        body["ticket"] = args.ticket
    if args.access_url:
        body["access_url"] = args.access_url
    if not body:
        raise SkillError("VALIDATION_ERROR", "Pass --ticket <ticket> or --access-url <url>.")
    data = cua_auth.authorized_call(
        state, base_url, "POST", "/v1/desktop/access/revoke", body=body, retries=IDEMPOTENT_RETRIES
    )
    return {"data": data, "next": {
        "agent_hint": "The old desktop access URL should no longer work. Run desktop access again if the user needs a fresh link.",
    }}


def cmd_desktop_lifecycle(args, state, session):
    base_url = resolve_base_url(args, state)
    body = {}
    if args.desktop:
        body["desktop_id"] = args.desktop
    if args.idempotency_key:
        body["idempotency_key"] = args.idempotency_key
    if getattr(args, "confirm", False):
        body["confirm"] = True
    data = cua_auth.authorized_call(state, base_url, "POST", f"/v1/desktop/{args.lifecycle_action}", body=body)
    operation_id = data.get("operation_id") or (data.get("operation") or {}).get("operation_id")
    if operation_id:
        session.set_last(last_operation_id=operation_id)
    return {"data": data, "next": {
        "command": f"python3 {script_path()} desktop operation get --operation-id {operation_id}" if operation_id else None,
        "agent_hint": "Lifecycle operation accepted. Poll desktop operation get until terminal=true.",
    }}


def cmd_desktop_operation_get(args, state, session):
    base_url = resolve_base_url(args, state)
    operation_id = args.operation_id or (session.last_operation_id if args.last else None)
    if not operation_id:
        raise SkillError("VALIDATION_ERROR", "operation_id is required. Pass --operation-id <id> or --last.")
    data = cua_auth.authorized_call(
        state, base_url, "GET", f"/v1/desktop/operations/{operation_id}", retries=IDEMPOTENT_RETRIES
    )
    session.set_last(last_operation_id=operation_id)
    return {"data": data}


def cmd_model_get(args, state, session):
    base_url = resolve_base_url(args, state)
    data = cua_auth.authorized_call(state, base_url, "GET", "/v1/model-config", retries=IDEMPOTENT_RETRIES)
    return {"data": data, "next": {
        "agent_hint": "This is the default model config for future CUA delegations on the bound desktop.",
    }}


def cmd_model_set(args, state, session):
    base_url = resolve_base_url(args, state)
    body = {
        "main_model": args.main_model,
        "reasoning_effort": args.reasoning_effort,
    }
    data = cua_auth.authorized_call(state, base_url, "POST", "/v1/model-config", body=body)
    return {"data": data, "next": {
        "agent_hint": "Model config updated. It affects future CUA delegations on the bound desktop.",
    }}


def cmd_task_run(args, state, session):
    base_url = resolve_base_url(args, state)
    _validate_wait_ms(args.wait_ms)
    body = {"objective": args.objective, "wait_ms": 0}
    if args.desktop:
        body["desktop"] = args.desktop
    if args.title:
        body["title"] = args.title
    if args.disable_ask_user:
        body["disable_ask_user"] = True
    envelope = cua_auth.authorized_call(
        state, base_url, "POST", "/v1/tasks", body=body, timeout=_call_timeout(0)
    )
    envelope = _wait_invocation_with_budget(
        state, base_url, envelope.get("invocation_id"), args.wait_ms, initial_envelope=envelope
    )
    return _task_result("task run", envelope, session)


def cmd_task_continue(args, state, session):
    base_url = resolve_base_url(args, state)
    _validate_wait_ms(args.wait_ms)
    context_id = _resolve_context_id(args, session)
    body = {"objective": args.objective, "wait_ms": 0}
    if args.disable_ask_user:
        body["disable_ask_user"] = True
    envelope = cua_auth.authorized_call(
        state, base_url, "POST", f"/v1/contexts/{context_id}/tasks", body=body, timeout=_call_timeout(0)
    )
    envelope = _wait_invocation_with_budget(
        state, base_url, envelope.get("invocation_id"), args.wait_ms, initial_envelope=envelope
    )
    return _task_result("task continue", envelope, session)


def cmd_task_status(args, state, session):
    base_url = resolve_base_url(args, state)
    task_id = _resolve_task_id(args, session)
    envelope = cua_auth.authorized_call(
        state, base_url, "GET", f"/v1/tasks/{task_id}", retries=IDEMPOTENT_RETRIES
    )
    return _task_result("task status", envelope, session)


def cmd_task_result(args, state, session):
    base_url = resolve_base_url(args, state)
    task_id = _resolve_task_id(args, session)
    deadline = now_epoch() + max(1, args.timeout)
    envelope = None
    while now_epoch() < deadline:
        try:
            envelope = cua_auth.authorized_call(
                state, base_url, "GET", f"/v1/tasks/{task_id}/result", retries=IDEMPOTENT_RETRIES
            )
            if envelope.get("outcome") != "in_progress":
                break
            time.sleep(3)
        except SkillError as exc:
            if exc.code in RETRYABLE_ERROR_CODES:
                time.sleep(2)
                continue
            raise
    if envelope is None:
        envelope = cua_auth.authorized_call(
            state, base_url, "GET", f"/v1/tasks/{task_id}/result", retries=IDEMPOTENT_RETRIES
        )
    return _task_result("task result", envelope, session)


def cmd_task_answer(args, state, session):
    base_url = resolve_base_url(args, state)
    _validate_wait_ms(args.wait_ms)
    task_id = _resolve_task_id(args, session)
    body = {"answer": args.answer, "wait_ms": 0}
    envelope = cua_auth.authorized_call(
        state, base_url, "POST", f"/v1/tasks/{task_id}/answer", body=body, timeout=_call_timeout(0)
    )
    envelope = _wait_invocation_with_budget(
        state, base_url, task_id, args.wait_ms, initial_envelope=envelope
    )
    return _task_result("task answer", envelope, session)


def cmd_task_cancel(args, state, session):
    base_url = resolve_base_url(args, state)
    task_id = _resolve_task_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "POST", f"/v1/tasks/{task_id}/cancel", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_context_list(args, state, session):
    base_url = resolve_base_url(args, state)
    data = cua_auth.authorized_call(state, base_url, "GET", "/v1/contexts", retries=IDEMPOTENT_RETRIES)
    return {"data": data}


def cmd_context_create(args, state, session):
    base_url = resolve_base_url(args, state)
    body = {}
    if args.title:
        body["title"] = args.title
    if args.desktop:
        body["desktop"] = args.desktop
    data = cua_auth.authorized_call(state, base_url, "POST", "/v1/contexts", body=body)
    context_id = data.get("context_id")
    if context_id:
        session.set_last(last_context_id=context_id)
    return {"data": data, "next": {
        "command": f"python3 {script_path()} task continue --context-id {context_id} --objective \"<TASK>\"",
        "agent_hint": "Context created. Add background with context add-note, or start work with task continue.",
    }}


def cmd_context_add_note(args, state, session):
    base_url = resolve_base_url(args, state)
    context_id = _resolve_context_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "POST", f"/v1/contexts/{context_id}/notes", body={"text": args.text}
    )
    return {"data": data}


def cmd_context_show(args, state, session):
    base_url = resolve_base_url(args, state)
    context_id = _resolve_context_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "GET", f"/v1/contexts/{context_id}", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_timeline_show(args, state, session):
    base_url = resolve_base_url(args, state)
    context_id = _resolve_context_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "GET", f"/v1/contexts/{context_id}/timeline", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_artifact_list(args, state, session):
    base_url = resolve_base_url(args, state)
    task_id = _resolve_task_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "GET", f"/v1/tasks/{task_id}/artifacts", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_artifact_save(args, state, session):
    base_url = resolve_base_url(args, state)
    artifact_id = args.artifact_id or (session.last_artifact_id if args.last else None)
    if not artifact_id:
        raise SkillError("VALIDATION_ERROR", "artifact_id is required. Pass --artifact-id <id> or --last.")
    task_id = args.task_id or session.last_task_id
    query = {"task_id": task_id} if task_id else None
    headers, raw = cua_auth.authorized_raw_call(
        state, base_url, "GET", f"/v1/artifacts/{artifact_id}/content",
        query=query, timeout=120, retries=IDEMPOTENT_RETRIES
    )
    session.set_last(last_artifact_id=artifact_id)
    data = _legacy_artifact_envelope(raw, headers)
    if data is None:
        mime_type = _content_type(headers)
        path = _write_artifact(raw, args.output, mime_type)
        result = {
            "source_artifact_id": artifact_id,
            "source_task_id": task_id,
            "file": path,
            "mime_type": mime_type,
            "bytes": len(raw),
            "transport": "raw",
        }
        if _looks_like_html(mime_type, raw):
            result["suspect_html"] = True
            return {"data": result, "next": {
                "agent_hint": "The downloaded bytes look like an HTML page, not the expected file. "
                "This is usually an error/login/interstitial page. Do not present it as the real document; "
                "ask CUA to re-export the artifact instead.",
            }}
        return {"data": result, "next": {
            "agent_hint": "Artifact saved to data.file from raw bytes. Share the path with the user; do not print the bytes.",
        }}

    if data.get("missing"):
        return {"data": {
            "source_artifact_id": artifact_id,
            "source_task_id": task_id,
            "file": None,
            "missing": True,
            "placeholder_text": data.get("placeholder_text"),
        }, "next": {
            "agent_hint": "The artifact has no downloadable bytes (placeholder/missing). "
            "Tell the user it is unavailable; do not claim a file was saved.",
        }}

    b64 = data.get("data")
    if not b64:
        raise SkillError("INTERNAL", "Artifact response contained no data and was not marked missing.")
    try:
        raw = base64.b64decode(b64)
    except (ValueError, TypeError) as exc:
        raise SkillError("INTERNAL", f"Artifact was not valid base64: {exc}")

    mime_type = data.get("mime_type")
    path = _write_artifact(raw, args.output, mime_type)
    result = {
        "source_artifact_id": artifact_id,
        "source_task_id": task_id,
        "file": path,
        "mime_type": mime_type,
        "bytes": len(raw),
        "transport": "legacy_base64",
    }
    # A surprise HTML payload usually means an error/interstitial page (e.g. a
    # Cloudflare challenge from an external share link), not the real file.
    if _looks_like_html(mime_type, raw):
        result["suspect_html"] = True
        return {"data": result, "next": {
            "agent_hint": "The downloaded bytes look like an HTML page, not the expected file. "
            "This is usually an error/login/interstitial page. Do not present it as the real document; "
            "ask CUA to re-export the artifact instead.",
        }}
    return {"data": result, "next": {
        "agent_hint": "Artifact saved to data.file. Share the path with the user; do not print the bytes.",
    }}


def cmd_schedule_create_once(args, state, session):
    base_url = resolve_base_url(args, state)
    run_at = validate_iso8601(args.run_at, "--run-at")
    body = {"goal": args.goal, "run_at": run_at}
    _augment_schedule_body(body, args)
    data = cua_auth.authorized_call(state, base_url, "POST", "/v1/schedules/once", body=body)
    return _schedule_result("schedule create-once", data, session)


def cmd_schedule_create_recurring(args, state, session):
    base_url = resolve_base_url(args, state)
    start_at = validate_iso8601(args.start_at, "--start-at")
    if args.interval_hours is None or args.interval_hours < 1:
        raise SkillError("VALIDATION_ERROR", "--interval-hours must be an integer >= 1.")
    body = {"goal": args.goal, "start_at": start_at, "interval_hours": args.interval_hours}
    if args.allowed_start_window_ms is not None:
        body["allowed_start_window_ms"] = args.allowed_start_window_ms
    _augment_schedule_body(body, args)
    data = cua_auth.authorized_call(state, base_url, "POST", "/v1/schedules/recurring", body=body)
    return _schedule_result("schedule create-recurring", data, session)


def cmd_schedule_list(args, state, session):
    base_url = resolve_base_url(args, state)
    data = cua_auth.authorized_call(state, base_url, "GET", "/v1/schedules", retries=IDEMPOTENT_RETRIES)
    return {"data": data}


def cmd_schedule_status(args, state, session):
    base_url = resolve_base_url(args, state)
    schedule_id = _resolve_schedule_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "GET", f"/v1/schedules/{schedule_id}", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_schedule_history(args, state, session):
    base_url = resolve_base_url(args, state)
    schedule_id = _resolve_schedule_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "GET", f"/v1/schedules/{schedule_id}/history", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_schedule_stop(args, state, session):
    base_url = resolve_base_url(args, state)
    schedule_id = _resolve_schedule_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "POST", f"/v1/schedules/{schedule_id}/stop", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_schedule_delete(args, state, session):
    base_url = resolve_base_url(args, state)
    schedule_id = _resolve_schedule_id(args, session)
    data = cua_auth.authorized_call(
        state, base_url, "DELETE", f"/v1/schedules/{schedule_id}", retries=IDEMPOTENT_RETRIES
    )
    return {"data": data}


def cmd_self_test(args, state, session):
    """Local-only checks. Does not create CUA tasks or call backends."""
    checks = {
        "python_version": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 8),
        "auth_file": str(state.path),
        "logged_in": bool(state.access_token or os.environ.get("AP_CUA_AGENTPLAN_API_KEY")
                          or os.environ.get("AGENTPLAN_API_KEY") or os.environ.get("ARK_API_KEY")),
        "api_base_url": resolve_base_url(args, state) if _has_base_url(args, state) else None,
        "last_invocation_id": session.last_invocation_id,
    }
    next_hint = None
    if not checks["logged_in"]:
        next_hint = {
            "setup_command": login_setup_command(),
            "agent_hint": "Not logged in yet. Do not run setup_command yourself; ask the user to run it in a local terminal before real work.",
        }
    return {"data": checks, "next": next_hint} if next_hint else {"data": checks}


# -- helpers ---------------------------------------------------------------


def _has_base_url(args, state):
    return bool(args.api_base_url or os.environ.get("AP_CUA_SKILL_API_BASE_URL")
                or os.environ.get("CUA_SKILL_API_BASE_URL")
                or state.api_base_url or bundled_base_url())


def _resolve_task_id(args, session):
    if getattr(args, "task_id", None):
        return args.task_id
    if getattr(args, "last", False) and session.last_task_id:
        return session.last_task_id
    raise SkillError(
        "VALIDATION_ERROR",
        "task_id is required. Pass --task-id <id> or --last to reuse the most recent task.",
    )


def _resolve_context_id(args, session):
    if getattr(args, "context_id", None):
        return args.context_id
    if getattr(args, "last_context", False) and session.last_context_id:
        return session.last_context_id
    raise SkillError(
        "VALIDATION_ERROR",
        "context_id is required. Pass --context-id <id> or --last-context.",
    )


def _resolve_schedule_id(args, session):
    if getattr(args, "schedule_id", None):
        return args.schedule_id
    if getattr(args, "last", False) and session.last_schedule_id:
        return session.last_schedule_id
    raise SkillError(
        "VALIDATION_ERROR",
        "schedule_id is required. Pass --schedule-id <id> or --last.",
    )


def _augment_schedule_body(body, args):
    if args.title:
        body["title"] = args.title
    if args.desktop:
        body["desktop"] = args.desktop
    if getattr(args, "context_mode", None):
        body["context_mode"] = args.context_mode
    if getattr(args, "context_id", None):
        body["context_id"] = args.context_id
    if getattr(args, "task_id", None):
        body["task_id"] = args.task_id


def _task_result(action, envelope, session):
    """Persist task/context ids from an envelope, then return data + task-flavored next."""
    task_id = envelope.get("invocation_id")
    platform = envelope.get("platform") or {}
    context_id = platform.get("context_id")
    session.set_last(
        last_task_id=task_id,
        last_invocation_id=task_id,
        last_context_id=context_id,
    )
    return {"data": envelope, "next": _next_for_task(envelope)}


def _next_for_task(envelope):
    outcome = envelope.get("outcome")
    task_id = envelope.get("invocation_id")
    script = script_path()
    next_action = envelope.get("next_action") or {}
    hint = next_action.get("agent_hint", "")
    if outcome == "in_progress":
        return {
            "command": f"python3 {script} task status --task-id {task_id}",
            "agent_hint": hint or "Keep checking task status until completed, needs_input, failed, or cancelled. "
            f"For a hands-off wait use `python3 {script} task result --task-id {task_id}`. "
            "Do not answer the task from progress.",
        }
    if outcome == "needs_input":
        return {
            "command": f'python3 {script} task answer --task-id {task_id} --answer "<USER_ANSWER>"',
            "agent_hint": hint or "Relay input_request.question to the user verbatim, then submit their reply with task answer.",
        }
    if outcome == "completed":
        return {"agent_hint": hint or "Use data.result.text as the authoritative final result. "
                "Save any produced files with artifact save."}
    if outcome == "failed":
        return {"agent_hint": hint or "CUA could not complete the task. Explain the failure; retry only if the user asks."}
    if outcome == "cancelled":
        return {"agent_hint": hint or "The task was cancelled."}
    return None


def _schedule_result(action, data, session):
    schedule_id = data.get("schedule_id")
    if schedule_id:
        session.set_last(last_schedule_id=schedule_id)
    return {"data": data, "next": {
        "command": f"python3 {script_path()} schedule status --schedule-id {schedule_id}",
        "agent_hint": "Scheduled task created. Do NOT run the goal now unless the user also asked to do it once. "
        "After the scheduled time, use schedule history to read what actually ran.",
    }}


def _looks_like_html(mime_type, raw):
    if mime_type and "html" in mime_type.lower():
        return True
    head = raw[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _content_type(headers):
    value = headers.get("content-type") or headers.get("Content-Type") or ""
    return value.split(";", 1)[0].strip() or None


def _legacy_artifact_envelope(raw, headers):
    content_type = _content_type(headers) or ""
    if "json" not in content_type and not raw.lstrip().startswith(b"{"):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("ok") is True and isinstance(payload.get("data"), dict):
        return payload["data"]
    return None


def _write_artifact(raw, output, mime_type):
    if output:
        path = os.path.abspath(os.path.expanduser(output))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(raw)
        return path
    ext = ext_for_mime(mime_type)
    fd, path = tempfile.mkstemp(prefix="cua-artifact-", suffix=ext)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    return path


def _resolve_invocation_id(args, session):
    if getattr(args, "invocation_id", None):
        return args.invocation_id
    if getattr(args, "last", False) and session.last_invocation_id:
        return session.last_invocation_id
    raise SkillError(
        "VALIDATION_ERROR",
        "invocation_id is required. Pass --invocation-id <id> or --last to reuse the most recent invocation.",
    )


def _call_timeout(wait_ms):
    """HTTP timeout must outlast the server-side wait window.

    When wait_ms is None the server applies its own default wait (up to a minute
    or so), so give a generous floor rather than timing out early.
    """
    if wait_ms is None:
        return 120
    return int(wait_ms / 1000.0) + 30


def _wait_invocation_with_budget(state, base_url, invocation_id, wait_ms, initial_envelope=None):
    """Wait for one invocation using a total client budget and <=60s server calls."""
    if wait_ms is None:
        wait_ms = DEFAULT_WATCH_WAIT_MS
    _validate_wait_ms(wait_ms)
    if not invocation_id:
        raise SkillError("INTERNAL", "CUA gateway response did not include an invocation id.")
    if initial_envelope is not None and initial_envelope.get("outcome") != "in_progress":
        return initial_envelope
    if wait_ms == 0:
        if initial_envelope is not None:
            return initial_envelope
        return cua_auth.authorized_call(
            state, base_url, "GET", f"/v1/invocations/{invocation_id}", retries=IDEMPOTENT_RETRIES
        )

    remaining_ms = wait_ms
    envelope = initial_envelope
    while remaining_ms > 0:
        chunk_ms = min(SERVER_WAIT_CHUNK_MS, remaining_ms)
        envelope = cua_auth.authorized_call(
            state,
            base_url,
            "POST",
            f"/v1/invocations/{invocation_id}/watch",
            body={"wait_ms": chunk_ms},
            timeout=_call_timeout(chunk_ms),
            retries=IDEMPOTENT_RETRIES,
        )
        if envelope.get("outcome") != "in_progress":
            return envelope
        remaining_ms -= chunk_ms
    return envelope


def _validate_wait_ms(wait_ms):
    if wait_ms is not None and wait_ms < 0:
        raise SkillError("VALIDATION_ERROR", "--wait-ms must be >= 0")


def _envelope_result(action, envelope, session):
    invocation_id = envelope.get("invocation_id")
    if invocation_id:
        session.set_last_invocation_id(invocation_id)
    return {"data": envelope, "next": _next_for_envelope(envelope)}


def _next_for_envelope(envelope):
    outcome = envelope.get("outcome")
    invocation_id = envelope.get("invocation_id")
    script = script_path()
    next_action = envelope.get("next_action") or {}
    hint = next_action.get("agent_hint", "")
    if outcome == "in_progress":
        return {
            "command": f"python3 {script} watch --invocation-id {invocation_id}",
            "agent_hint": hint or "Keep watching until completed, needs_input, failed, or cancelled. "
            "Each watch returns quickly; just call it again while in_progress. For a hands-off wait, "
            f"use `python3 {script} result --invocation-id {invocation_id}`. Do not answer the task from progress.",
        }
    if outcome == "needs_input":
        return {
            "command": f'python3 {script} answer --invocation-id {invocation_id} --answer "<USER_ANSWER>"',
            "agent_hint": hint or "Relay input_request.question to the user verbatim, then submit their reply with answer.",
        }
    if outcome == "completed":
        return {"agent_hint": hint or "Use data.result.text as the authoritative final result."}
    if outcome == "failed":
        return {"agent_hint": hint or "CUA could not complete the task. Explain the failure; retry only if the user asks."}
    if outcome == "cancelled":
        return {"agent_hint": hint or "The task was cancelled."}
    return None


def _derive_desktop_urls(access_url):
    """Split a desktop access URL into the desktop-only view and the full CUA UI.

    Legacy gateways hand back a spice link like `https://<host>/<desktop>`.
    Multi-desktop gateways instead own the full path namespace and return
    `https://<host>/desktops/{desktop_id}/cua-app/` directly. Never prepend the
    legacy `/cua-app` prefix to a namespaced path: doing so changes the desktop
    path covered by the ticket and causes DesktopTicketMismatch.

    Any query string / fragment (including a temporary ticket) is preserved.
    Returns `(desktop_view_url, full_interface_url)`; either element is None when
    it is not available or cannot be derived safely.
    """
    try:
        parts = urllib.parse.urlsplit(access_url)
    except ValueError:
        return None, None
    if not parts.scheme or not parts.netloc:
        return None, None

    path = parts.path or "/"
    segments = path.split("/")
    if len(segments) >= 3 and segments[1] == "desktops" and segments[2]:
        desktop_prefix = f"/desktops/{segments[2]}"
        scope_kind = segments[3] if len(segments) >= 4 else ""
        if scope_kind == "cua-app":
            # Access Hub already returned the canonical full-interface path.
            # A distinct desktop-only URL is not part of this response contract.
            return None, access_url
        full_path = desktop_prefix + "/cua-app/"
        desktop_view_url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, path, parts.query, parts.fragment)
        )
        full_interface_url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, full_path, parts.query, parts.fragment)
        )
        return desktop_view_url, full_interface_url

    # Normalise: if the access URL already points at /cua-app, recover the bare
    # desktop path so we can present both forms consistently.
    if path == "/cua-app" or path.startswith("/cua-app/"):
        full_path = path
        desktop_path = path[len("/cua-app"):] or "/"
    else:
        desktop_path = path
        prefix = path if path.startswith("/") else "/" + path
        full_path = "/cua-app" + prefix

    desktop_view_url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, desktop_path, parts.query, parts.fragment)
    )
    full_interface_url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, full_path, parts.query, parts.fragment)
    )
    return desktop_view_url, full_interface_url


def _save_screenshot(b64, mime_type):
    ext = ".jpg"
    if mime_type:
        if "png" in mime_type:
            ext = ".png"
        elif "webp" in mime_type:
            ext = ".webp"
    try:
        raw = base64.b64decode(b64)
    except (ValueError, TypeError) as exc:
        raise SkillError("INTERNAL", f"Screenshot was not valid base64: {exc}")
    fd, path = tempfile.mkstemp(prefix="cua-screenshot-", suffix=ext)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    return path


# -- argument parser -------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(prog="cua.py", description="CUA Skill CLI")
    parser.add_argument("--api-base-url", help="CUA gateway base URL (overrides env and cache).")
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="Authentication commands").add_subparsers(dest="auth_command")

    p = auth.add_parser("status", help="Check the current login state.")
    p.set_defaults(handler=cmd_auth_status, action="auth status")

    p = auth.add_parser("login", help="Configure a Volcengine Ark AgentPlan API key.")
    p.add_argument("--api-key", help="AgentPlan API key. Prefer the local terminal prompt or AP_CUA_AGENTPLAN_API_KEY.")
    p.add_argument("--no-prompt", action="store_true", help="Do not prompt; require --api-key or env.")
    p.set_defaults(handler=cmd_auth_login, action="auth login")

    p = auth.add_parser("logout", help="Clear the locally cached AgentPlan API key.")
    p.set_defaults(handler=cmd_auth_logout, action="auth logout")

    p = sub.add_parser("ping", help="Read-only auth and desktop-binding check. Creates no task.")
    p.set_defaults(handler=cmd_ping, action="ping")

    p = sub.add_parser("delegate", help="Delegate the user's original objective to CUA.")
    p.add_argument("--objective", required=True, help="The user's original request. Do not pre-plan or add constraints.")
    p.add_argument("--wait-ms", type=int, default=0,
                   help="Total ms to wait before returning. Calls are chunked at 60 seconds. "
                        "Default 0 returns the invocation id immediately. Does not cancel the task.")
    p.set_defaults(handler=cmd_delegate, action="delegate")

    p = sub.add_parser("watch", help="Wait for or check an invocation's next state.")
    _add_invocation_args(p)
    p.add_argument("--wait-ms", type=int, default=DEFAULT_WATCH_WAIT_MS,
                   help="Total ms to wait before returning; server calls are chunked at 60 seconds. Does not cancel the task.")
    p.set_defaults(handler=cmd_watch, action="watch")

    p = sub.add_parser("answer", help="Submit the user's answer when outcome is needs_input.")
    _add_invocation_args(p)
    p.add_argument("--answer", required=True, help="The user's answer to input_request.question.")
    p.add_argument("--wait-ms", type=int, default=DEFAULT_WATCH_WAIT_MS, help="Total ms to wait before returning; calls are chunked at 60 seconds.")
    p.set_defaults(handler=cmd_answer, action="answer")

    p = sub.add_parser("cancel", help="Request cancellation. Only when the user asks to stop.")
    _add_invocation_args(p)
    p.set_defaults(handler=cmd_cancel, action="cancel")

    p = sub.add_parser("result", help="Wait until terminal and return the authoritative result.")
    _add_invocation_args(p)
    p.add_argument("--timeout", type=int, default=600, help="Total seconds to keep waiting for a terminal outcome.")
    p.set_defaults(handler=cmd_result, action="result")

    p = sub.add_parser("observe", help="Get a temporary desktop access URL and optional screenshot.")
    p.add_argument("--invocation-id", help="Observe the desktop bound to this invocation.")
    p.add_argument("--last", action="store_true", help="Use the most recent invocation id.")
    p.add_argument("--include-screenshot", action="store_true", help="Also capture a screenshot (saved to a local file).")
    p.set_defaults(handler=cmd_observe, action="observe")

    p = sub.add_parser("self-test", help="Local-only checks. Creates no CUA task.")
    p.set_defaults(handler=cmd_self_test, action="self-test")

    _add_semantic_parsers(sub)

    return parser


def _add_semantic_parsers(sub):
    """Resource-aware semantic command surface (task/context/schedule/artifact/desktop)."""

    p = sub.add_parser("diagnose", help="Confirm CUA is reachable and a desktop is bound. Creates no task.")
    p.set_defaults(handler=cmd_diagnose, action="diagnose")

    desktop = sub.add_parser("desktop", help="Cloud-desktop commands.").add_subparsers(dest="desktop_command")
    p = desktop.add_parser("list", help="List selectable cloud desktops.")
    p.set_defaults(handler=cmd_desktop_list, action="desktop list")

    p = desktop.add_parser("access", help="Get a temporary desktop access URL.")
    p.set_defaults(handler=cmd_desktop_access, action="desktop access")

    p = desktop.add_parser("revoke-access", help="Revoke a temporary desktop access ticket.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticket", help="Ticket value returned by desktop access.")
    group.add_argument("--access-url", help="Desktop access URL containing the ticket query parameter.")
    p.set_defaults(handler=cmd_desktop_revoke_access, action="desktop revoke-access")

    p = desktop.add_parser("reboot", help="Reboot the bound cloud desktop.")
    p.add_argument("--desktop", help="Desktop id. Defaults to the bound desktop.")
    p.add_argument("--idempotency-key", help="Optional caller-scoped idempotency key.")
    p.set_defaults(handler=cmd_desktop_lifecycle, action="desktop reboot", lifecycle_action="reboot")

    p = desktop.add_parser("reset", help="Reset the bound cloud desktop. Requires --confirm.")
    p.add_argument("--desktop", help="Desktop id. Defaults to the bound desktop.")
    p.add_argument("--idempotency-key", help="Optional caller-scoped idempotency key.")
    p.add_argument("--confirm", action="store_true", required=True, help="Required explicit confirmation.")
    p.set_defaults(handler=cmd_desktop_lifecycle, action="desktop reset", lifecycle_action="reset")

    operation = desktop.add_parser("operation", help="Read desktop lifecycle operation status.").add_subparsers(dest="operation_command")
    p = operation.add_parser("get", help="Get operation status.")
    p.add_argument("--operation-id", help="Operation id returned by desktop reboot/reset.")
    p.add_argument("--last", action="store_true", help="Use the most recent operation id.")
    p.set_defaults(handler=cmd_desktop_operation_get, action="desktop operation get")

    model = sub.add_parser("model", help="Read or set the default CUA model config.").add_subparsers(dest="model_command")
    p = model.add_parser("get", help="Read the bound desktop's default model config.")
    p.set_defaults(handler=cmd_model_get, action="model get")

    p = model.add_parser("set", help="Set the bound desktop's default main model and reasoning effort.")
    p.add_argument("--main-model", required=True, help="Model id from `model get` data.available_models[].id.")
    p.add_argument("--reasoning-effort", required=True, choices=["low", "medium", "high"],
                   help="Default reasoning effort for future delegations.")
    p.set_defaults(handler=cmd_model_set, action="model set")

    # -- task --
    task = sub.add_parser("task", help="Run and manage CUA tasks (semantic delegate).").add_subparsers(dest="task_command")

    p = task.add_parser("run", help="Start a new CUA task, optionally on a chosen desktop.")
    p.add_argument("--objective", required=True, help="The user's original request. Do not pre-plan or add constraints.")
    p.add_argument("--desktop", help="Desktop id or name (from desktop list). Defaults to the bound desktop.")
    p.add_argument("--title", help="Title for the auto-created context.")
    p.add_argument("--disable-ask-user", action="store_true", help="Do not let CUA pause to ask the user mid-task.")
    p.add_argument("--wait-ms", type=int, default=0, help="Total ms to wait before returning; calls are chunked at 60 seconds. Default 0.")
    p.set_defaults(handler=cmd_task_run, action="task run")

    p = task.add_parser("continue", help="Continue work in an existing context.")
    p.add_argument("--objective", required=True, help="What to do next in this context.")
    p.add_argument("--context-id", help="The context to continue.")
    p.add_argument("--last-context", action="store_true", help="Use the most recent context id.")
    p.add_argument("--disable-ask-user", action="store_true", help="Do not let CUA pause to ask the user mid-task.")
    p.add_argument("--wait-ms", type=int, default=0, help="Total ms to wait before returning; calls are chunked at 60 seconds. Default 0.")
    p.set_defaults(handler=cmd_task_continue, action="task continue")

    p = task.add_parser("status", help="Check a task's current state.")
    _add_task_args(p)
    p.set_defaults(handler=cmd_task_status, action="task status")

    p = task.add_parser("result", help="Wait until terminal and return the authoritative result.")
    _add_task_args(p)
    p.add_argument("--timeout", type=int, default=600, help="Total seconds to keep waiting for a terminal outcome.")
    p.set_defaults(handler=cmd_task_result, action="task result")

    p = task.add_parser("answer", help="Answer CUA's question when outcome is needs_input.")
    _add_task_args(p)
    p.add_argument("--answer", required=True, help="The user's answer to input_request.question.")
    p.add_argument("--wait-ms", type=int, default=DEFAULT_WATCH_WAIT_MS, help="Total ms to wait before returning; calls are chunked at 60 seconds.")
    p.set_defaults(handler=cmd_task_answer, action="task answer")

    p = task.add_parser("cancel", help="Cancel a task. Only when the user asks to stop.")
    _add_task_args(p)
    p.set_defaults(handler=cmd_task_cancel, action="task cancel")

    # -- context --
    context = sub.add_parser("context", help="Manage reusable task contexts.").add_subparsers(dest="context_command")

    p = context.add_parser("list", help="List continuable contexts.")
    p.set_defaults(handler=cmd_context_list, action="context list")

    p = context.add_parser("create", help="Open a long-lived context without running a task yet.")
    p.add_argument("--title", help="Context title.")
    p.add_argument("--desktop", help="Desktop id or name. Defaults to the bound desktop.")
    p.set_defaults(handler=cmd_context_create, action="context create")

    p = context.add_parser("add-note", help="Add background to a context without starting a run.")
    _add_context_args(p)
    p.add_argument("--text", required=True, help="The background/context note to record.")
    p.set_defaults(handler=cmd_context_add_note, action="context add-note")

    p = context.add_parser("show", help="Show a context summary and recent task.")
    _add_context_args(p)
    p.set_defaults(handler=cmd_context_show, action="context show")

    # -- timeline --
    timeline = sub.add_parser("timeline", help="Conversation timeline commands.").add_subparsers(dest="timeline_command")
    p = timeline.add_parser("show", help="Show the full conversation timeline projection for a context.")
    _add_context_args(p)
    p.set_defaults(handler=cmd_timeline_show, action="timeline show")

    # -- artifact --
    artifact = sub.add_parser("artifact", help="List and save task artifacts.").add_subparsers(dest="artifact_command")

    p = artifact.add_parser("list", help="List artifacts produced by a task.")
    _add_task_args(p)
    p.set_defaults(handler=cmd_artifact_list, action="artifact list")

    p = artifact.add_parser("save", help="Download an artifact (file, screenshot, log) to a local path.")
    p.add_argument("--artifact-id", help="The artifact id (from artifact list / result).")
    p.add_argument("--last", action="store_true", help="Use the most recent artifact id.")
    p.add_argument("--task-id", help="Task id that owns the artifact. Defaults to the most recent task.")
    p.add_argument("--output", help="Where to write the file. Defaults to a temp file named by content type.")
    p.set_defaults(handler=cmd_artifact_save, action="artifact save")

    # -- schedule --
    schedule = sub.add_parser("schedule", help="Create and manage future/recurring tasks.").add_subparsers(dest="schedule_command")

    p = schedule.add_parser("create-once", help="Run a goal once at a future time.")
    _add_schedule_common_args(p)
    p.add_argument("--run-at", required=True, help="ISO-8601 time to run once, e.g. 2026-06-25T20:00:00Z.")
    p.set_defaults(handler=cmd_schedule_create_once, action="schedule create-once")

    p = schedule.add_parser("create-recurring", help="Run a goal repeatedly on an interval.")
    _add_schedule_common_args(p)
    p.add_argument("--start-at", required=True, help="ISO-8601 first run time.")
    p.add_argument("--interval-hours", type=int, required=True, help="Hours between runs (minimum 1).")
    p.add_argument("--allowed-start-window-ms", type=int, help="Optional allowed start jitter window in ms.")
    p.set_defaults(handler=cmd_schedule_create_recurring, action="schedule create-recurring")

    p = schedule.add_parser("list", help="List scheduled tasks.")
    p.set_defaults(handler=cmd_schedule_list, action="schedule list")

    p = schedule.add_parser("status", help="Show a scheduled task's status.")
    _add_schedule_args(p)
    p.set_defaults(handler=cmd_schedule_status, action="schedule status")

    p = schedule.add_parser("history", help="Show a scheduled task's executions and results.")
    _add_schedule_args(p)
    p.set_defaults(handler=cmd_schedule_history, action="schedule history")

    p = schedule.add_parser("stop", help="Stop future triggers of a scheduled task.")
    _add_schedule_args(p)
    p.set_defaults(handler=cmd_schedule_stop, action="schedule stop")

    p = schedule.add_parser("delete", help="Delete a scheduled task.")
    _add_schedule_args(p)
    p.set_defaults(handler=cmd_schedule_delete, action="schedule delete")


def _add_task_args(p):
    p.add_argument("--task-id", help="The task id (same id space as invocation_id).")
    p.add_argument("--last", action="store_true", help="Use the most recent task id from local session cache.")


def _add_context_args(p):
    p.add_argument("--context-id", help="The context id.")
    p.add_argument("--last-context", action="store_true", help="Use the most recent context id.")


def _add_schedule_args(p):
    p.add_argument("--schedule-id", help="The schedule id.")
    p.add_argument("--last", action="store_true", help="Use the most recent schedule id.")


def _add_schedule_common_args(p):
    p.add_argument("--goal", required=True, help="The product goal to run at the scheduled time.")
    p.add_argument("--title", help="Optional display title.")
    p.add_argument("--desktop", help="Desktop id or name. Defaults to the bound desktop.")
    p.add_argument("--context-mode", choices=["scheduled", "current"], help="Bind to a fresh scheduled context (default) or a current one.")
    p.add_argument("--context-id", help="Required when --context-mode current. Source context to bind.")
    p.add_argument("--task-id", help="Optional source task id for provenance.")


def _add_invocation_args(p):
    p.add_argument("--invocation-id", help="The invocation id returned by delegate.")
    p.add_argument("--last", action="store_true", help="Use the most recent invocation id from local session cache.")


if __name__ == "__main__":
    sys.exit(main())
