"""Authentication orchestration for the AgentPlan CUA Skill CLI.

This skill variant uses the caller's Volcengine Ark AgentPlan API key as the
bearer credential. The key is cached locally with 0600 permissions and is never
written to stdout/stderr. The gateway validates it with Ark acquire and uses the
same key as the model API key for CUA runtime calls.
"""

import getpass
import os
import sys
import time

from cua_http import gateway_call, raw_request
from cua_util import RETRYABLE_ERROR_CODES, SkillError, login_setup_command

DEFAULT_LOGIN_TIMEOUT_SEC = 0
API_KEY_ENV_VARS = ("AP_CUA_AGENTPLAN_API_KEY", "AGENTPLAN_API_KEY", "ARK_API_KEY")


def ensure_access_token(state, base_url):
    """Return the configured AgentPlan API key."""
    token = _configured_api_key(state)
    if token:
        return token
    raise SkillError(
        "AUTH_REQUIRED",
        "AgentPlan API key required for CUA Skill.",
        setup_command=login_setup_command(),
    )


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
    try:
        return gateway_call(method, base_url, path, token=ensure_access_token(state, base_url), **kwargs)
    except SkillError as exc:
        raise _auth_error_with_retry(exc)


def _authorized_raw_call_once(state, base_url, method, path, body=None, query=None, timeout=None):
    kwargs = {"body": body, "query": query}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        _status, headers, raw = raw_request(method, base_url, path, token=ensure_access_token(state, base_url), **kwargs)
        return headers, raw
    except SkillError as exc:
        raise _auth_error_with_retry(exc)


def login(state, base_url, api_key=None, prompt=True, **_unused):
    """Configure and validate an AgentPlan API key."""
    token = _first_non_empty(api_key, *_env_api_keys())
    if not token and prompt:
        if not sys.stdin.isatty():
            raise SkillError(
                "AUTH_REQUIRED",
                "AgentPlan API key required. Open a local terminal and run setup_command; do not paste the API key into chat.",
                setup_command=login_setup_command(),
            )
        token = getpass.getpass("AgentPlan API key: ").strip()
    if not token:
        raise SkillError(
            "AUTH_REQUIRED",
            "AgentPlan API key required. Open a local terminal and run setup_command, or set AP_CUA_AGENTPLAN_API_KEY in that terminal.",
            setup_command=login_setup_command(),
        )

    try:
        data = gateway_call("GET", base_url, "/v1/auth/me", token=token)
    except SkillError as exc:
        raise _auth_error_with_retry(exc)
    user = _safe_user(data.get("user") or data.get("caller") or data)
    state.set_api_key(
        api_base_url=base_url,
        api_key=token,
        user=user,
        desktop_bound=bool(data.get("desktop_bound")),
    )
    return {
        "status": "logged_in",
        "auth_type": "agentplan_api_key",
        "user": user,
        "desktop_bound": bool(data.get("desktop_bound")),
        "scopes": _scopes(data),
    }


def auth_status(state, base_url):
    """Verify the current API key against /v1/auth/me without exposing it."""
    data = authorized_call(state, base_url, "GET", "/v1/auth/me")
    user = _safe_user(data.get("user") or data.get("caller") or data)
    if user and user != state.user and not _env_api_keys():
        state.set_api_key(
            api_base_url=base_url,
            api_key=state.access_token,
            user=user,
            desktop_bound=bool(data.get("desktop_bound")),
        )
    return {
        "status": "logged_in",
        "auth_type": "agentplan_api_key",
        "user": user,
        "scopes": _scopes(data),
        "desktop_bound": bool(data.get("desktop_bound") or state.desktop_bound),
    }


def logout(state, base_url):
    state.clear_tokens()
    return {"status": "logged_out"}


# -- internals -------------------------------------------------------------


def _configured_api_key(state):
    return _first_non_empty(*_env_api_keys(), state.access_token)


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
    message = (exc.message or "").lower()
    return "ark acquire returned status 401" in message or "ark acquire returned status 403" in message


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
