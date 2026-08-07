"""Authentication orchestration for the AgentPlan CUA Skill CLI.

This skill variant uses the caller's Volcengine Ark AgentPlan API key as the
bearer credential. Keys sourced from arkcli stay in memory; manual fallback
keys are cached locally with 0600 permissions. No key is written to
stdout/stderr. The gateway validates it with Ark acquire and uses the same key
as the model API key for CUA runtime calls.
"""

import getpass
import json
import os
import shutil
import subprocess
import sys
import time

from cua_http import gateway_call, raw_request
from cua_util import RETRYABLE_ERROR_CODES, SkillError, login_setup_command

DEFAULT_LOGIN_TIMEOUT_SEC = 0
API_KEY_ENV_VARS = ("AP_CUA_AGENTPLAN_API_KEY", "AGENTPLAN_API_KEY", "ARK_API_KEY")
ARKCLI_TIMEOUT_SEC = 20


def ensure_access_token(state, base_url):
    """Return the configured AgentPlan API key."""
    credential, discovery = _resolve_credential(state)
    if credential:
        return credential["token"]
    raise _auth_required(discovery)


def refresh_access_token(state, base_url):
    raise SkillError(
        "AUTH_REQUIRED",
        "AgentPlan API keys are not refreshed by the skill. Ask the user to run setup_command in a local terminal.",
        setup_command=login_setup_command(),
    )


def authorized_call(state, base_url, method, path, body=None, query=None, timeout=None, retries=0):
    """Call a business endpoint with AgentPlan bearer auth and optional retry.

    `retries` should only be > 0 for idempotent calls (GET, or watch/observe/ping
    which are safe to repeat). Never retry delegate/answer — they create state.
    """
    attempt = 0
    while True:
        try:
            return _authorized_call_once(state, base_url, method, path, body=body, query=query, timeout=timeout)
        except SkillError as exc:
            if exc.code in RETRYABLE_ERROR_CODES and attempt < retries:
                attempt += 1
                time.sleep(min(2 * attempt, 5))
                continue
            raise


def authorized_raw_call(state, base_url, method, path, body=None, query=None, timeout=None, retries=0):
    """Call a business endpoint and return (headers, raw_bytes), with the same
    auth/retry behavior as authorized_call."""
    attempt = 0
    while True:
        try:
            return _authorized_raw_call_once(state, base_url, method, path, body=body, query=query, timeout=timeout)
        except SkillError as exc:
            if exc.code in RETRYABLE_ERROR_CODES and attempt < retries:
                attempt += 1
                time.sleep(min(2 * attempt, 5))
                continue
            raise


def _authorized_call_once(state, base_url, method, path, body=None, query=None, timeout=None):
    kwargs = {"body": body, "query": query}
    if timeout is not None:
        kwargs["timeout"] = timeout
    data, _credential = _with_credential_recovery(
        state,
        lambda token: gateway_call(method, base_url, path, token=token, **kwargs),
    )
    return data


def _authorized_raw_call_once(state, base_url, method, path, body=None, query=None, timeout=None):
    kwargs = {"body": body, "query": query}
    if timeout is not None:
        kwargs["timeout"] = timeout
    result, _credential = _with_credential_recovery(
        state,
        lambda token: raw_request(method, base_url, path, token=token, **kwargs),
    )
    _status, headers, raw = result
    return headers, raw


def login(state, base_url, api_key=None, prompt=True, **_unused):
    """Configure and validate an AgentPlan API key."""
    token = _first_non_empty(api_key, *_env_api_keys())
    source = "explicit" if _first_non_empty(api_key) else ("environment" if token else None)
    profile = None
    arkcli_discovery = None
    if not token:
        credential, arkcli_discovery = _arkcli_credential()
        if credential:
            token = credential["token"]
            source = credential["source"]
            profile = credential.get("profile")
    if not token and prompt:
        if not sys.stdin.isatty():
            raise _auth_required(arkcli_discovery)
        token = getpass.getpass("AgentPlan API key: ").strip()
        source = "prompt"
    if not token:
        raise _auth_required(arkcli_discovery)

    try:
        data = gateway_call("GET", base_url, "/v1/auth/me", token=token)
    except SkillError as exc:
        if source == "arkcli" and _is_agentplan_auth_rejection(exc):
            if not prompt or not sys.stdin.isatty():
                raise _auth_required({"status": "api_key_rejected", "profile": profile})
            token = getpass.getpass("AgentPlan API key (arkcli fallback): ").strip()
            if not token:
                raise _auth_required({"status": "api_key_rejected", "profile": profile})
            source = "prompt"
            profile = None
            try:
                data = gateway_call("GET", base_url, "/v1/auth/me", token=token)
            except SkillError as fallback_exc:
                raise _auth_error_with_retry(fallback_exc)
        else:
            raise _auth_error_with_retry(exc)
    user = _safe_user(data.get("user") or data.get("caller") or data)
    # arkcli remains the source of truth. Its key is used only by this process
    # and is deliberately not copied into the CUA auth cache.
    if source != "arkcli":
        state.set_api_key(
            api_base_url=base_url,
            api_key=token,
            user=user,
            desktop_bound=bool(data.get("desktop_bound")),
        )
    result = {
        "status": "logged_in",
        "auth_type": "agentplan_api_key",
        "credential_source": source,
        "user": user,
        "desktop_bound": bool(data.get("desktop_bound")),
        "scopes": _scopes(data),
    }
    if profile:
        result["arkcli_profile"] = profile
    return result


def auth_status(state, base_url):
    """Verify the current API key against /v1/auth/me without exposing it."""
    data, credential = _with_credential_recovery(
        state,
        lambda token: gateway_call("GET", base_url, "/v1/auth/me", token=token),
    )
    user = _safe_user(data.get("user") or data.get("caller") or data)
    if user and user != state.user and credential["source"] == "cache":
        state.set_api_key(
            api_base_url=base_url,
            api_key=state.access_token,
            user=user,
            desktop_bound=bool(data.get("desktop_bound")),
        )
    result = {
        "status": "logged_in",
        "auth_type": "agentplan_api_key",
        "credential_source": credential["source"],
        "api_key_source": credential["source"],
        "user": user,
        "scopes": _scopes(data),
        "desktop_bound": bool(data.get("desktop_bound") or state.desktop_bound),
    }
    if credential.get("profile"):
        result["arkcli_profile"] = credential["profile"]
    return result


def logout(state, base_url):
    state.clear_tokens()
    return {"status": "logged_out"}


# -- internals -------------------------------------------------------------


def _resolve_credential(state):
    token = _first_non_empty(*_env_api_keys())
    if token:
        return {"token": token, "source": "environment"}, None
    if state.access_token:
        return {"token": state.access_token, "source": "cache"}, None
    return _arkcli_credential()


def _with_credential_recovery(state, operation):
    credential, discovery = _resolve_credential(state)
    if not credential:
        raise _auth_required(discovery)
    try:
        return operation(credential["token"]), credential
    except SkillError as exc:
        if _is_agentplan_auth_rejection(exc):
            if credential["source"] == "arkcli":
                raise _auth_required({"status": "api_key_rejected", "profile": credential.get("profile")})
            arkcli_credential, arkcli_discovery = _arkcli_credential()
            if arkcli_credential and arkcli_credential["token"] != credential["token"]:
                try:
                    return operation(arkcli_credential["token"]), arkcli_credential
                except SkillError as arkcli_exc:
                    if _is_agentplan_auth_rejection(arkcli_exc):
                        raise _auth_required({
                            "status": "api_key_rejected",
                            "profile": arkcli_credential.get("profile"),
                        })
                    raise _auth_error_with_retry(arkcli_exc)
            if arkcli_credential:
                raise _auth_required({
                    "status": "api_key_rejected",
                    "profile": arkcli_credential.get("profile"),
                })
            if not arkcli_credential:
                raise _auth_required(arkcli_discovery)
        raise _auth_error_with_retry(exc)


def _arkcli_credential():
    """Read an Agent Plan personal Max key from arkcli without logging it or persisting it."""
    executable = shutil.which("arkcli")
    if not executable:
        return None, {"status": "not_installed"}

    env = os.environ.copy()
    env.setdefault("ARKCLI_CALLER_TYPE", "ai_agent")
    env.setdefault("ARKCLI_CALLER_NAME", "unknown_agent")
    env["ARKCLI_SKILL_NAME"] = "ark-cua"
    try:
        listed = subprocess.run(
            [executable, "profile", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=ARKCLI_TIMEOUT_SEC,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None, {"status": "profile_list_failed"}
    if listed.returncode != 0:
        return None, {"status": _arkcli_error_status(listed.stderr, "profile_list_failed")}
    try:
        payload = json.loads(listed.stdout)
    except (TypeError, ValueError):
        return None, {"status": "invalid_profile_list"}
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list) and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        profiles = payload["data"].get("profiles")
    matches = [
        profile for profile in (profiles or [])
        if isinstance(profile, dict)
        and profile.get("type") == "agent-plan"
        and profile.get("plan_tier") == "max"
        and isinstance(profile.get("name"), str)
        and profile["name"].strip()
    ]
    if not matches:
        return None, {"status": "no_agent_plan_max_profile"}
    profile_name = matches[0]["name"].strip()

    try:
        fetched = subprocess.run(
            [executable, "profile", "apikey", "get", "--profile", profile_name, "--plain"],
            capture_output=True,
            text=True,
            timeout=ARKCLI_TIMEOUT_SEC,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None, {"status": "apikey_get_failed", "profile": profile_name}
    if fetched.returncode != 0:
        return None, {
            "status": _arkcli_error_status(fetched.stderr, "apikey_get_failed"),
            "profile": profile_name,
        }
    token = fetched.stdout.strip()
    if not token:
        return None, {"status": "no_api_key", "profile": profile_name}
    return {
        "token": token,
        "source": "arkcli",
        "profile": profile_name,
    }, {"status": "ready", "profile": profile_name}


def _arkcli_error_status(stderr, fallback):
    try:
        payload = json.loads(stderr)
    except (TypeError, ValueError):
        return fallback
    error = payload.get("error") if isinstance(payload, dict) else None
    error_type = error.get("type") if isinstance(error, dict) else None
    return error_type if isinstance(error_type, str) and error_type else fallback


def _auth_required(discovery=None):
    status = (discovery or {}).get("status") or "unavailable"
    hints = {
        "not_installed": "arkcli is not installed; use the local hidden API-key prompt.",
        "no_agent_plan_max_profile": "arkcli has no personal Agent Plan Max profile; log in or open that plan, then retry.",
        "no_api_key": "The arkcli profile has no API key; run `arkcli auth apikey` or `arkcli profile keys refresh`, then retry.",
        "api_key_rejected": "The Agent Plan Max key returned by arkcli was rejected; run `arkcli profile keys refresh` or `arkcli auth apikey`, then retry.",
    }
    return SkillError(
        "AUTH_REQUIRED",
        "AgentPlan API key required for CUA Skill.",
        setup_command=login_setup_command(),
        arkcli_status=status,
        arkcli_hint=hints.get(status, "arkcli could not supply an Agent Plan Max API key; use the local hidden API-key prompt."),
    )


def _auth_error_with_retry(exc):
    if _is_agentplan_auth_rejection(exc):
        return SkillError(
            "AUTH_REQUIRED",
            "AgentPlan APIKey 不合法，请输入正确的 APIKey。",
            setup_command=login_setup_command(),
            auth_type="agentplan_bearer",
        )
    if exc.code in ("AUTH_REQUIRED", "TOKEN_EXPIRED", "REFRESH_FAILED") and "setup_command" not in exc.extra:
        exc.extra["setup_command"] = login_setup_command()
    return exc


def _is_agentplan_auth_rejection(exc):
    if exc.code not in ("AUTH_REQUIRED", "TOKEN_EXPIRED", "FORBIDDEN"):
        return False
    if exc.extra.get("auth_type") == "agentplan_bearer":
        return True
    message = " ".join([
        str(exc.message or ""),
        str(exc.extra.get("reason") or ""),
        str(exc.extra.get("upstream_code") or ""),
    ]).lower()
    if "ark acquire returned status 401" in message or "ark acquire returned status 403" in message:
        return True
    return exc.extra.get("upstream_status") == 401 and (
        "agentplan apikey" in message or "unauthorized" in message
    )


def _env_api_keys():
    return [os.environ.get(name) for name in API_KEY_ENV_VARS]


def _first_non_empty(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _scopes(data):
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if isinstance(scopes, list):
        return scopes
    scope = data.get("scope") if isinstance(data, dict) else None
    if isinstance(scope, str):
        return scope.split()
    return []


def _safe_user(user):
    if not isinstance(user, dict):
        return {}
    return {
        "account_id": user.get("account_id") or user.get("accountId"),
        "project_name": user.get("project_name") or user.get("projectName"),
        "apikey_id": user.get("apikey_id") or user.get("api_key_id") or user.get("apiKeyId"),
        "org_id": user.get("org_id"),
        "user_id": user.get("user_id"),
        "email": user.get("email"),
    }
