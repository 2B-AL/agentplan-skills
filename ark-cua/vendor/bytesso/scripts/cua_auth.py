"""ByteSSO / Access Hub authentication for the CUA Skill CLI.

The default login path starts an Access Hub browser login flow, waits for the
user to finish ByteSSO, then stores the returned `cua_api_...` credential
locally with 0600 permissions. Legacy `cua_mcp_...` keys remain supported when
explicitly supplied through stdin or the environment.
"""

import getpass
import json
import os
import sys
import time
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cua_http import gateway_manifest, gateway_tool_call
from cua_util import RETRYABLE_ERROR_CODES, SkillError, login_retry_command


def bearer_key_from_env():
    token = os.environ.get("CUA_SKILL_BEARER_KEY")
    return token.strip() if isinstance(token, str) and token.strip() else None


def ensure_bearer_key(state, access_hub_base_url):
    token = bearer_key_from_env() or state.bearer_key
    if token:
        return token
    raise SkillError(
        "AUTH_REQUIRED",
        "Login required for CUA Skill. Run auth login; it will print the ByteSSO browser login URL.",
        retry_command=login_retry_command(),
    )


def authorized_tool_call(state, access_hub_base_url, gateway_url, tool_name, arguments=None, timeout=None, retries=0):
    attempt = 0
    while True:
        try:
            token = ensure_bearer_key(state, access_hub_base_url)
            return gateway_tool_call(gateway_url, token, tool_name, arguments or {}, timeout=timeout or 120)
        except SkillError as exc:
            if exc.code in RETRYABLE_ERROR_CODES and attempt < retries:
                attempt += 1
                time.sleep(min(2 * attempt, 5))
                continue
            if exc.code in ("AUTH_REQUIRED", "TOKEN_EXPIRED", "REFRESH_FAILED") and "retry_command" not in exc.extra:
                exc.extra["retry_command"] = login_retry_command()
            raise


def login(state, access_hub_base_url, gateway_url, open_browser=True, bearer_key_stdin=False, no_validate=False):
    if bearer_key_stdin or bearer_key_from_env():
        token = _read_login_token(bearer_key_stdin)
    else:
        token = _login_with_skill_auth_flow(access_hub_base_url, open_browser=open_browser)
    _validate_token_shape(token)

    user = {}
    if not no_validate:
        access = gateway_tool_call(gateway_url, token, "cua_get_desktop_access", {}, timeout=30)
        user = _user_from_desktop_access(access, token)

    state.set_bearer_key(
        access_hub_base_url=access_hub_base_url,
        gateway_url=gateway_url,
        bearer_key=token,
        user=user,
        credential_type=_credential_type_for_token(token),
    )
    return {
        "status": "logged_in",
        "access_hub_url": access_hub_base_url,
        "gateway_url": gateway_url,
        "mcp_url": _mcp_url_from_gateway(gateway_url),
        "user": user,
        "credential": {"type": _credential_type_for_token(token), "source": "local_cache"},
    }


def auth_status(state, access_hub_base_url, gateway_url, online=True):
    token = bearer_key_from_env() or state.bearer_key
    if not token:
        return {
            "status": "logged_out",
            "access_hub_url": access_hub_base_url,
            "gateway_url": gateway_url,
            "retry_command": login_retry_command(),
            "agent_hint": "Run retry_command instead of opening Access Hub API endpoints directly. auth login will print exactly one browser login URL.",
        }
    if not online:
        return {
            "status": "configured",
            "access_hub_url": access_hub_base_url,
            "gateway_url": gateway_url,
            "credential": {"type": _credential_type_for_token(token, state), "source": _credential_source(state)},
        }
    access = gateway_tool_call(gateway_url, token, "cua_get_desktop_access", {}, timeout=30)
    return {
        "status": "logged_in",
        "access_hub_url": access_hub_base_url,
        "gateway_url": gateway_url,
        "user": _user_from_desktop_access(access, token),
        "credential": {"type": _credential_type_for_token(token, state), "source": _credential_source(state)},
    }


def logout(state):
    state.clear_tokens()
    return {"status": "logged_out"}


def online_self_test(state, access_hub_base_url, gateway_url):
    token = ensure_bearer_key(state, access_hub_base_url)
    manifest = gateway_manifest(gateway_url, timeout=30)
    desktop = gateway_tool_call(gateway_url, token, "cua_get_desktop_access", {}, timeout=30)
    return {
        "manifest": bool(manifest),
        "tool_count": len(manifest.get("tools") or []),
        "desktop_access": {
            "desktop": desktop.get("desktop"),
            "has_access_url": bool((desktop.get("access") or {}).get("desktop_login_url")),
        },
    }


def skill_auth_start_url(access_hub_base_url):
    return access_hub_base_url.rstrip("/") + "/api/v1/skill-auth/start"


def skill_auth_poll_url(access_hub_base_url):
    return access_hub_base_url.rstrip("/") + "/api/v1/skill-auth/poll"


def _user_from_desktop_access(access, token=None):
    desktop = access.get("desktop") or {}
    cua = access.get("cua") or {}
    return {
        "auth_type": _credential_type_for_token(token) if token else "access_hub_bearer",
        "desktop_bound": bool(desktop or cua),
        "desktop_id": desktop.get("id") or cua.get("vm_id"),
        "desktop_name": desktop.get("name") or cua.get("vm_name"),
    }


def _login_with_skill_auth_flow(access_hub_base_url, open_browser=True):
    start = _access_hub_json(skill_auth_start_url(access_hub_base_url), {})
    flow_id = str(start.get("flow_id") or "").strip()
    poll_token = str(start.get("poll_token") or "").strip()
    login_url = str(start.get("login_url") or start.get("verification_uri") or "").strip()
    if not flow_id or not poll_token or not login_url:
        raise SkillError("INTERNAL", "Access Hub skill-auth start response is missing flow_id, poll_token, or login_url.")

    _show_login_url(login_url)
    if open_browser:
        try:
            webbrowser.open(login_url)
        except Exception:  # noqa: BLE001 - headless environments are normal
            pass

    interval = _positive_int(start.get("poll_interval_seconds"), 2)
    expires_in = _positive_int(start.get("expires_in"), 600)
    deadline = time.time() + min(max(expires_in, 60), 900)
    while time.time() < deadline:
        time.sleep(interval)
        poll = _access_hub_json(skill_auth_poll_url(access_hub_base_url), {
            "flow_id": flow_id,
            "poll_token": poll_token,
        })
        status = str(poll.get("status") or "").strip().lower()
        if status == "pending":
            continue
        if status == "completed":
            credential = poll.get("credential") if isinstance(poll.get("credential"), dict) else {}
            token = str(credential.get("bearer_token") or "").strip()
            if not token:
                raise SkillError("INTERNAL", "Access Hub completed login without returning a bearer token.")
            return token
        raise SkillError("AUTH_REQUIRED", f"Access Hub login flow ended with status={status or 'unknown'}.", retry_command=login_retry_command())
    raise SkillError("AUTH_REQUIRED", "Access Hub login timed out before ByteSSO authorization completed.", retry_command=login_retry_command(), login_url=login_url)


def _access_hub_json(url, payload, timeout=30):
    raw = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=raw,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return _decode_json(resp.read(), "Access Hub")
    except HTTPError as exc:
        body = exc.read()
        message = _error_message(body) or f"HTTP {exc.code}"
        if exc.code in (401, 403, 404, 410):
            raise SkillError("AUTH_REQUIRED", message, retry_command=login_retry_command())
        raise SkillError("CUA_BACKEND_UNAVAILABLE", f"Access Hub returned HTTP {exc.code}: {message}")
    except URLError as exc:
        raise SkillError("NETWORK", f"Cannot reach Access Hub at {url}: {exc.reason}")
    except TimeoutError:
        raise SkillError("NETWORK", f"Request to Access Hub timed out: {url}")


def _decode_json(raw, service):
    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise SkillError("INTERNAL", f"{service} returned non-JSON response: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise SkillError("INTERNAL", f"{service} JSON response was not an object.")
    return data


def _error_message(raw):
    try:
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError:
        return ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "")
        if error:
            return str(error)
        if data.get("message"):
            return str(data.get("message"))
    return ""


def _show_login_url(login_url):
    sys.stderr.write("Open this URL to finish CUA Skill ByteSSO login:\n")
    sys.stderr.write(login_url + "\n")
    sys.stderr.flush()


def _positive_int(value, default):
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _mcp_url_from_gateway(gateway_url):
    url = gateway_url.rstrip("/")
    if url.endswith("/skill/mcp"):
        return url
    return url + "/skill/mcp"


def _read_login_token(bearer_key_stdin):
    token = bearer_key_from_env()
    if token:
        return token
    if bearer_key_stdin:
        return sys.stdin.read().strip()
    try:
        return getpass.getpass("Paste legacy Access Hub bearer token (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise SkillError(
            "AUTH_REQUIRED",
            "Legacy bearer token was not provided. Run auth login again.",
            retry_command=login_retry_command(),
        ) from exc


def _validate_token_shape(token):
    if not token:
        raise SkillError("AUTH_REQUIRED", "Bearer token was empty.", retry_command=login_retry_command())
    if not (token.startswith("cua_api_") or token.startswith("cua_mcp_")):
        raise SkillError("VALIDATION_ERROR", "Expected an Access Hub bearer token starting with 'cua_api_' or 'cua_mcp_'.")


def _credential_type_for_token(token, state=None):
    if token and token.startswith("cua_api_"):
        return "access_hub_skill_api_key"
    if token and token.startswith("cua_mcp_"):
        return "access_hub_bearer_key"
    if state and state.credential_type:
        return state.credential_type
    return "access_hub_bearer"


def _credential_source(state):
    if bearer_key_from_env():
        return "environment"
    if state.bearer_key:
        return "local_cache"
    return "none"
