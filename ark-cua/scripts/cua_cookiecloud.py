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

"""Private CookieCloud configuration and upload orchestration.

Configuration stays in this process and the local Chrome helper's stdin.
Only bounded domain/status metadata is returned to the CLI.

After the extension is configured it uploads on its own schedule, or when the
user syncs manually, and CUA imports each upload. A Chrome debugging
connection is therefore needed only to change the extension's configuration.

On a Gateway with shared connections, every desktop of one owner joins the
same connection, so one extension configuration and one upload reach all of
them. This computer remembers, per Gateway, which connection it last wrote into
the extension; a desktop that joins that connection needs no Chrome connection.
"""

import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from cua_util import RETRYABLE_ERROR_CODES, SkillError

EXTENSION_ID = "ffjiejobkoibkjlhjnlgmcnnigeelbdl"
STORAGE_KEY = "COOKIE_SYNC_SETTING"
AUTH_CODES = {"AUTH_REQUIRED", "TOKEN_EXPIRED", "REFRESH_FAILED", "FORBIDDEN"}
RECOVERABLE_CREATE = {"NETWORK", "GATEWAY_TIMEOUT", "CUA_BACKEND_UNAVAILABLE", "CONFLICT", "Conflict", "cookiecloud_disconnect_first"}
# An older Gateway has no status route; the Chrome-based setup still works there.
STATUS_UNSUPPORTED = {"INTERNAL", "NotFound", "NOT_FOUND"}
# A failed poll while waiting is retried at the next interval, never as an upload.
TRANSIENT_POLL = {"NETWORK", "GATEWAY_TIMEOUT", "CUA_BACKEND_UNAVAILABLE"}
ERROR_CODE = re.compile(r"cookiecloud_[a-z_]{1,64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
TARGET_STATES = {"imported", "import_pending", "waiting_upload", "waiting_authorization"}
MAX_DESKTOPS = 20
MAX_TARGETS = 100
MAX_WAIT_SECONDS = 300
POLL_SECONDS = 5
_sleep = time.sleep
_clock = time.monotonic


def domains(values):
    result = []
    for value in values:
        try:
            value = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
        except (AttributeError, UnicodeError):
            raise SkillError("VALIDATION_ERROR", "Select complete website domains.") from None
        if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
            raise SkillError("VALIDATION_ERROR", "Select complete website domains without paths or wildcards.")
        if value not in result:
            result.append(value)
    if not result:
        raise SkillError("VALIDATION_ERROR", "Select at least one website domain.")
    return sorted(result)


def desktop_ids(values):
    """Exact desktop ids in the order given, without duplicates."""
    values = [values] if isinstance(values, str) else list(values or [])
    result = []
    for value in values:
        value = value.strip() if isinstance(value, str) else ""
        if not IDENTIFIER.fullmatch(value):
            raise SkillError("VALIDATION_ERROR", "Use exact CUA desktop ids.")
        if value not in result:
            result.append(value)
    if not 1 <= len(result) <= MAX_DESKTOPS:
        raise SkillError("VALIDATION_ERROR", f"Select between 1 and {MAX_DESKTOPS} desktops.")
    return result


def _identifier(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def _one_of(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def validate_cdp_url(value):
    try:
        url = urlsplit(value)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        valid = url.scheme in {"http", "ws"} and local and url.port and not url.username and not url.password and not url.query and not url.fragment
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise SkillError("VALIDATION_ERROR", "Use the user-authorized Chrome debugging URL on loopback.")


def _call(api, method, path, body=None, unsupported=frozenset()):
    try:
        return api(method, path, body)
    except SkillError as exc:
        if exc.code in unsupported:
            return None
        if exc.code in AUTH_CODES:
            safe = {k: v for k, v in exc.extra.items() if k in {"setup_command", "arkcli_status", "arkcli_hint", "manual_login_required", "gateway_login_required"}}
            raise SkillError(exc.code, "Current Gateway authentication or desktop permission is required.", **safe) from None
        if exc.code in RECOVERABLE_CREATE | {"cookiecloud_not_connected"}:
            raise SkillError(exc.code, "Browser sync configuration is unavailable on this Gateway. Check its version and CookieCloud setting; keep the current environment.") from None
        if exc.code == "cookiecloud_unavailable":
            # The desktop refused the change for now, for example while it imports an upload or runs a task.
            raise SkillError(exc.code, "Browser sync is temporarily unavailable for this desktop, for example while it imports an upload or runs a task. Retry once in a minute; keep the current extension settings.") from None
        if ERROR_CODE.fullmatch(exc.code):
            raise SkillError(exc.code, "Browser sync refused the request; report its code and keep the current extension settings.") from None
        raise SkillError("COOKIECLOUD_API_UNAVAILABLE", "Browser sync configuration is unavailable on this Gateway. Check its version and CookieCloud setting; keep the current environment.") from None


def _sync_path(desktop_id, suffix=""):
    return "/v1/desktops/" + quote(desktop_id, safe="") + "/browser-sync" + suffix


def _configuration(data, desktop_id):
    invalid = SkillError("COOKIECLOUD_CONFIG_INVALID", "The Gateway returned incomplete browser sync configuration; Chrome settings were not changed.")
    if not isinstance(data, dict) or data.get("desktop_id") != desktop_id or data.get("extension_id") != EXTENSION_ID or data.get("storage_key") != STORAGE_KEY:
        raise invalid
    settings = data.get("settings")
    if not isinstance(settings, dict) or any(not isinstance(settings.get(k), str) or not settings[k].strip() for k in (
        "endpoint",
        "uuid",
        "password",
        "headers",
    )):
        raise invalid
    try:
        endpoint = urlsplit(settings["endpoint"])
        valid_url = endpoint.scheme == "https" and endpoint.hostname and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment
    except ValueError:
        raise invalid from None
    if not valid_url or settings.get("type") != "up" or settings.get("with_storage") not in {0, 1} or settings.get("crypto_type") not in {"legacy", "aes-128-cbc-fixed"} or type(settings.get("interval")) is not int or settings["interval"] < 1 or (data.get("received_at") is not None and _timestamp(data["received_at"]) is None):
        raise invalid
    return {k: settings[k] for k in (
        "endpoint",
        "uuid",
        "password",
        "headers",
        "type",
        "interval",
        "crypto_type",
    )}


def _join(api, desktop_id):
    """Read the desktop's configuration, joining it to its owner's connection when it has none."""
    path = _sync_path(desktop_id, "/config")
    try:
        return _call(api, "GET", path)
    except SkillError as exc:
        if exc.code != "cookiecloud_not_connected":
            raise
    try:
        return _call(api, "POST", path, {})
    except SkillError as failure:
        if failure.code not in RECOVERABLE_CREATE:
            raise
        try:
            return _call(api, "GET", path)
        except SkillError:
            raise failure from None


def _view(data, desktop_id):
    """Validate a secret-free status view; connection fields are absent on older Gateways."""
    invalid = SkillError("COOKIECLOUD_API_UNAVAILABLE", "The Gateway returned an unreadable browser sync status; keep the current extension settings.")
    if not isinstance(data, dict) or data.get("desktop_id") != desktop_id or type(data.get("connected")) is not bool or type(data.get("import_pending")) is not bool:
        raise invalid
    if data.get("received_at") is not None and _timestamp(data["received_at"]) is None:
        raise invalid
    rejection = data.get("last_rejection")
    if rejection is not None and (not isinstance(rejection, dict) or not isinstance(rejection.get("code"), str) or not ERROR_CODE.fullmatch(rejection["code"])):
        raise invalid
    sites = []
    for site in data.get("sites") if isinstance(data.get("sites"), list) else []:
        if not isinstance(site, dict) or not isinstance(site.get("domain"), str) or site.get("status") not in {"succeeded", "failed"}:
            continue
        code = site.get("error_code")
        sites.append({"domain": site["domain"].lower(), "status": site["status"], "last_synced_at": site.get("last_synced_at") if _timestamp(site.get("last_synced_at")) else None,
                      "error_code": code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else None})
    targets = []
    for target in data.get("targets") if isinstance(data.get("targets"), list) else []:
        if not isinstance(target, dict) or not _identifier(target.get("desktop_id")) or not _one_of(target.get("status"), TARGET_STATES):
            continue
        code = target.get("last_error")
        targets.append({"desktop_id": target["desktop_id"], "status": target["status"],
                        "last_import_at": target.get("last_import_at") if _timestamp(target.get("last_import_at")) else None,
                        "last_error": code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else None})
        if len(targets) == MAX_TARGETS:
            break
    return {"connected": data["connected"], "received_at": data.get("received_at"), "import_pending": data["import_pending"],
            "last_rejection": {"code": rejection["code"], "at": rejection.get("at") if _timestamp(rejection.get("at")) else None} if rejection else None,
            "sites": sites, "connection_id": _identifier(data.get("connection_id")),
            "target_status": _one_of(data.get("target_status"), TARGET_STATES), "targets": targets}


def _status(api, desktop_id):
    """Read the secret-free sync status, or None when the Gateway lacks it."""
    data = _call(api, "GET", _sync_path(desktop_id, "/status"), unsupported=STATUS_UNSUPPORTED)
    return None if data is None else _view(data, desktop_id)


def _configure_extension(payload, timeout):
    node = shutil.which("node")
    if not node:
        raise SkillError("COOKIECLOUD_NODE_REQUIRED", "Automatic Chrome configuration needs Node.js 22 or newer.")
    try:
        version = subprocess.run([node, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
        major = re.match(r"v([0-9]+)\.", version.stdout.strip())
        if version.returncode or not major or int(major[1]) < 22:
            raise SkillError("COOKIECLOUD_NODE_REQUIRED", "Automatic Chrome configuration needs Node.js 22 or newer.")
        run = subprocess.run([node, str(Path(__file__).with_name("cookiecloud-extension.mjs"))],
                             input=json.dumps(payload), text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        result = json.loads(run.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise SkillError("COOKIECLOUD_CHROME_UNAVAILABLE", "Chrome configuration could not be confirmed. Check the authorized debugging connection and extension before retrying.") from None
    if run.returncode or not isinstance(result, dict) or result.get("ok") is not True:
        stage = result.get("stage") if isinstance(result, dict) else None
        raise SkillError("COOKIECLOUD_CHROME_UNAVAILABLE", "Chrome configuration could not be confirmed. Check the authorized debugging connection and installed CookieCloud extension.", stage=stage if stage in {"connect", "extension", "read", "write", "reload", "upload"} else None)
    return result


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _covers(site, domain):
    return site == domain or domain.endswith("." + site) or site.endswith("." + domain)


def _website_results(selected, status):
    results = []
    for domain in selected:
        matches = [site for site in status["sites"] if _covers(site["domain"], domain)]
        synced = [site["last_synced_at"] for site in matches if site["status"] == "succeeded" and site["last_synced_at"]]
        failed = next((site for site in matches if site["status"] == "failed"), None)
        if synced:
            results.append({"domain": domain, "state": "synced", "last_synced_at": max(synced, key=_timestamp)})
        elif failed:
            results.append({"domain": domain, "state": "failed", "error_code": failed["error_code"]})
        else:
            results.append({"domain": domain, "state": "not_received"})
    return results


def _wait_for_uploads(api, statuses, deadline):
    """Wait until each desktop imported the upload it was importing or a newer
    one, or a newer upload was rejected, or the deadline passed; never trigger
    an upload.

    All starting statuses are read before waiting and the desktops share one
    deadline, so one upload that reaches them all ends the wait for each.
    """
    start = {desktop_id: (_timestamp(status["received_at"]), status["last_rejection"], status["import_pending"])
             for desktop_id, status in statuses.items()}
    latest, outcome = dict(statuses), {}
    while len(outcome) < len(statuses):
        remaining = deadline - _clock()
        if remaining <= 0:
            break
        _sleep(min(POLL_SECONDS, remaining))
        for desktop_id, (before, rejection, pending) in start.items():
            if desktop_id in outcome:
                continue
            try:
                status = _status(api, desktop_id)
            except SkillError as exc:
                if exc.code in TRANSIENT_POLL:
                    continue
                raise
            if status is None or not status["connected"]:
                # A desktop that left meanwhile keeps its last connected status;
                # the next check reports the change.
                outcome[desktop_id] = "timeout"
                continue
            latest[desktop_id] = status
            after = _timestamp(status["received_at"])
            if status["last_rejection"] and status["last_rejection"] != rejection:
                outcome[desktop_id] = "rejected"
            elif not status["import_pending"] and after and (pending or before is None or after > before):
                outcome[desktop_id] = "received"
    return {desktop_id: (latest[desktop_id], outcome.get(desktop_id, "timeout")) for desktop_id in statuses}


CHROME_STEP = ("ask the user to enable Chrome remote debugging (chrome://inspect/#remote-debugging) and approve the connection, "
               "then rerun with --cdp-url. Later syncs do not need it.")
MANUAL_STEP = "click Manual Sync (手动同步) in the CookieCloud extension, then rerun with --wait-seconds 60"


def _connection_fields(status, joined=False):
    return {"connection_id": status["connection_id"], "target_status": status["target_status"], "targets": status["targets"], "joined": joined}


def _check(desktop_id, selected, api, held):
    """Read one desktop's sync results without touching Chrome or the extension.

    Returns the result and, when a wait can confirm a newer import, the status
    to wait from (None otherwise).
    """
    status = _status(api, desktop_id)
    base = {"desktop_id": desktop_id, "domains": selected, "configured": False}
    if status is None:
        return {"data": {**base, "status": "user_action_required", "reason": "chrome_connection_required"},
                "next": {"agent_hint": "This Gateway cannot report sync status; " + CHROME_STEP}}, None
    joined = False
    if not status["connected"] and held:
        # The extension on this computer already uploads to a connection on this
        # Gateway. Join the desktop to its owner's connection, usually that one,
        # so it receives the uploads with no change in Chrome.
        _join(api, desktop_id)
        joined, status = True, _status(api, desktop_id)
        if status is None:
            raise SkillError("COOKIECLOUD_API_UNAVAILABLE", "The Gateway stopped reporting browser sync status; keep the current extension settings.")
    if held and status["connected"] and status["connection_id"] and status["connection_id"] != held:
        # The extension's connection was revoked or belongs to another login, or
        # the desktop is still in an older connection. Either change stops some
        # connection's uploads, so only the user decides.
        step = ("switch the extension to this desktop's connection, which stops uploads to the extension's current one: " + CHROME_STEP if joined else
                "run browser-sync-leave for it and then browser-sync again; it then joins its owner's current connection, and the result tells "
                "whether that is the extension's.")
        return {"data": {**base, "status": "user_action_required", "reason": "connection_changed", **_connection_fields(status, joined),
                         "extension_connection_id": held},
                "next": {"agent_hint": "This desktop is not in the browser sync connection that the CookieCloud extension on this computer uploads to, "
                         "so it does not receive this computer's uploads. Tell the user; only if they want it to, " + step}}, None
    # Any accepted or rejected upload means the extension already holds this
    # desktop's connection, and so does this computer's record of it.
    if not status["connected"] or not (status["received_at"] or status["last_rejection"] or (held and status["connection_id"] == held)):
        return {"data": {**base, "status": "user_action_required", "reason": "setup_required", **_connection_fields(status)},
                "next": {"agent_hint": "This desktop has no active browser sync connection yet, or it was disconnected; " + CHROME_STEP}}, None
    if not status["received_at"] and not status["last_rejection"]:
        return {"data": {**base, "configured": True, "status": "user_action_required", "reason": "waiting_upload", **_connection_fields(status, joined)},
                "next": {"agent_hint": "The CookieCloud extension on this computer holds this connection, but no upload has arrived yet. Ask the user to "
                         "sign in to the websites in local Chrome and " + MANUAL_STEP + "."}}, None
    return _report(desktop_id, selected, status, joined), status


def _report(desktop_id, selected, status, joined, waited=None):
    """The result of a desktop that has received or rejected an upload."""
    results = _website_results(selected, status)
    data = {"desktop_id": desktop_id, "domains": selected, "configured": True, "received_at": status["received_at"], "import_pending": status["import_pending"],
            "last_rejection": status["last_rejection"], "sites": results, "waited": waited, **_connection_fields(status, joined)}
    joined_hint = "This desktop joined the connection the extension already uploads to; no Chrome change was needed. " if joined else ""
    if status["last_rejection"]:
        if status["last_rejection"]["code"] == "cookiecloud_payload_too_large":
            hint = ("The latest upload was too large and was rejected. Ask the user to set Sync Local Storage? (是否同步Local Storage) "
                    "to No (否) in the extension, or to sync fewer websites; then " + MANUAL_STEP + ". "
                    "With the user's Chrome debugging connection, rerunning with --cdp-url also turns Local Storage off.")
        else:
            hint = "The latest upload was rejected; report its code. Ask the user to " + MANUAL_STEP + "."
        return {"data": {**data, "status": "user_action_required", "reason": "upload_rejected"}, "next": {"agent_hint": joined_hint + hint}}
    if status["import_pending"]:
        return {"data": {**data, "status": "importing"},
                "next": {"agent_hint": joined_hint + "CUA received the latest upload and is importing it. Rerun with --wait-seconds 60 to confirm website results."}}
    missing = [item["domain"] for item in results if item["state"] == "not_received"]
    if missing:
        return {"data": {**data, "status": "user_action_required", "reason": "website_not_received"},
                "next": {"agent_hint": joined_hint + "CUA has not received these websites: " + ", ".join(missing) + ". Ask the user to sign in to them in local Chrome, "
                         "add any missing one to the extension's Sync Domain Keyword (同步域名关键词) list, click Save (保存), and " + MANUAL_STEP + ". "
                         "To have the helper add them instead, the user authorizes the Chrome debugging connection and you rerun with --cdp-url."}}
    if any(item["state"] == "failed" for item in results):
        return {"data": {**data, "status": "user_action_required", "reason": "import_failed"},
                "next": {"agent_hint": joined_hint + "CUA could not import some websites; report their error_code. Ask the user to " + MANUAL_STEP + "."}}
    later = (" No newer upload arrived while waiting; these results are from the latest upload" + (" at " + status["received_at"] if status["received_at"] else "")
             + ". If the user synced after that, the upload did not reach this connection: ask them to check the extension, or set it up again with --cdp-url."
             if waited == "timeout" else "")
    return {"data": {**data, "status": "synced"},
            "next": {"agent_hint": joined_hint + "CUA imported these websites." + later + " Refresh them on the remote desktop to verify login; an import does not prove login. "
                     "After the user signs in again locally, ask them to " + MANUAL_STEP + "."}}


def _check_all(args, desktops, selected, api, memory):
    held = memory.get() if memory else None
    results, wait_from = [], {}
    for desktop_id in desktops:
        try:
            result, status = _check(desktop_id, selected, api, held)
        except SkillError as exc:
            # One desktop's failure does not hide the others' results; a login
            # problem, or the failure of the only desktop, stops the check.
            if exc.code in AUTH_CODES or len(desktops) == 1:
                raise
            result, status = {"data": {"desktop_id": desktop_id, "domains": selected, "configured": False, "status": "user_action_required",
                                       "reason": "unavailable", "error_code": exc.code},
                              "next": {"agent_hint": "This desktop could not be read now; check the Gateway's availability, then rerun browser-sync for it."
                                       if exc.code in RETRYABLE_ERROR_CODES else str(exc)}}, None
        results.append(result)
        if status:
            wait_from[desktop_id] = status
    if args.wait_seconds and wait_from:
        waited = _wait_for_uploads(api, wait_from, _clock() + args.wait_seconds)
        for index, result in enumerate(results):
            desktop_id = result["data"]["desktop_id"]
            if desktop_id in waited:
                status, outcome = waited[desktop_id]
                results[index] = _report(desktop_id, selected, status, result["data"]["joined"], outcome)
    # Reading a desktop's status refreshes its saved authorization, so only
    # members that were not checked can still be waiting for that refresh.
    waiting = sorted({target["desktop_id"] for result in results for target in result["data"].get("targets") or []
                      if target["status"] == "waiting_authorization"} - set(desktops))
    note = (" Desktops " + ", ".join(waiting) + " of this connection wait for an authorization refresh and import no uploads until browser-sync "
            "runs for them.") if waiting else ""
    if len(results) == 1:
        results[0]["next"]["agent_hint"] += note
        return results[0]
    datas = [result["data"] for result in results]
    order = {"user_action_required": 0, "importing": 1, "synced": 2}
    worst = min(datas, key=lambda item: order.get(item["status"], 0))
    connections = sorted({item["connection_id"] for item in datas if item.get("connection_id")})
    hints = {}
    for result in results:
        hints.setdefault(result["next"]["agent_hint"], []).append(result["data"]["desktop_id"])
    hint = " ".join(", ".join(ids) + ": " + text for text, ids in hints.items())
    if len(connections) > 1:
        hint = ("These desktops are in different browser sync connections (different owners, or an older connection); one CookieCloud "
                "extension uploads to only one of them. Tell the user which desktops share a connection. ") + hint
    data = {"status": worst["status"], "reason": worst.get("reason"), "domains": selected, "desktop_ids": desktops, "desktops": datas,
            "connection_id": connections[0] if len(connections) == 1 else None, "connection_ids": connections,
            "different_connections": len(connections) > 1}
    return {"data": data, "next": {"agent_hint": hint + note}}


def _setup(args, desktops, selected, api, memory):
    validate_cdp_url(args.cdp_url)
    joined = []
    for desktop_id in desktops:
        data = _join(api, desktop_id)
        settings, connection = _configuration(data, desktop_id), _identifier(data.get("connection_id"))
        if connection is None and len(desktops) > 1:
            # A Gateway without shared connections keeps one per desktop; joining
            # the others would only create connections nobody uploads to.
            return {"data": {"status": "user_action_required", "reason": "different_connections", "configured": False, "domains": selected,
                             "desktop_ids": desktops, "desktops": [{"desktop_id": desktop_id, "connection_id": None}]},
                    "next": {"agent_hint": "This Gateway keeps a separate browser sync connection for each desktop, and one CookieCloud extension "
                             "uploads to only one connection. Chrome was not changed. Ask the user which desktop to set up, then rerun with only that desktop."}}
        joined.append((desktop_id, data, settings, connection))
    if len({(settings["endpoint"], settings["uuid"]) for _, _, settings, _ in joined}) > 1:
        # One extension holds one configuration. Never write one desktop's
        # credentials when the others would silently stop receiving uploads.
        return {"data": {"status": "user_action_required", "reason": "different_connections", "configured": False, "domains": selected,
                         "desktop_ids": desktops, "desktops": [{"desktop_id": desktop_id, "connection_id": connection} for desktop_id, _, _, connection in joined]},
                "next": {"agent_hint": "These desktops do not share one browser sync connection (different owners, or an older connection), and one "
                         "CookieCloud extension uploads to only one connection. Chrome was not changed. Ask the user which desktops to set up, then rerun "
                         "with only desktops that share a connection. A desktop of the same owner that is in an older connection joins the current "
                         "one when browser-sync runs after browser-sync-leave for it."}}
    desktop_id, data, settings, connection = joined[0]
    # Website selection belongs to the local extension, not a server-side list;
    # the helper adds these websites to it. Page storage is opt-in.
    settings["domains"] = "\n".join(selected)
    settings["with_storage"] = 1 if args.with_storage else 0
    before = _timestamp(data.get("received_at"))
    result = _configure_extension({"cdp_url": args.cdp_url, "extension_id": EXTENSION_ID,
                                   "storage_key": STORAGE_KEY, "settings": settings,
                                   "confirm_domains": args.confirm_domains,
                                   "confirm_desktop_switch": args.confirm_desktop_switch}, args.timeout_seconds)
    if result.get("confirmation_required") is True:
        switch, narrow = result.get("other_desktop_configured") is True, result.get("local_scope_all_websites") is True
        reasons = [text for flag, text in (
            (switch, "the extension uploads to another connection, whose desktops stop receiving updates after the switch (confirm with --confirm-desktop-switch)"),
            (narrow, "the extension currently syncs all websites and would sync only the selected ones (confirm with --confirm-domains)"),
        ) if flag]
        return {"data": {"status": "user_action_required", "reason": "confirmation_required", "configured": False, "domains": selected,
                         "desktop_ids": desktops, "other_desktop_configured": switch, "local_scope_all_websites": narrow},
                "next": {"agent_hint": "Explain to the user that " + "; and ".join(reasons) + ". Only after the user agrees, repeat with the stated flag."}}
    configured = result.get("configured") is True and result.get("readback_verified") is True
    if not configured:
        raise SkillError("COOKIECLOUD_CHROME_UNAVAILABLE", "Extension configuration readback was not confirmed.")
    configured_domains = [d for d in result.get("domains", []) if isinstance(d, str) and len(d) <= 253][:200] if isinstance(result.get("domains"), list) else selected
    if memory and connection:
        memory.remember(connection)
    # The extension's test completes its upload request before returning. Read
    # the authoritative receipt once; never manufacture an upload or replay it.
    after = None
    uploaded = False
    try:
        after_data = _call(api, "GET", _sync_path(desktop_id, "/config"))
        after_settings = _configuration(after_data, desktop_id)
        after = _timestamp(after_data.get("received_at"))
        uploaded = result.get("upload_attempted") is True and after_settings["uuid"] == settings["uuid"] and after is not None and (before is None or after > before)
    except SkillError:
        # Writes have already been verified. An unavailable receipt is a
        # partial result, not a transient error inviting a second upload.
        pass
    later = " Later syncs, and desktops of this owner added later, do not need the debugging connection; the user can turn it off now. Run browser-sync without --cdp-url to check results."
    if result.get("local_storage_turned_off") is True:
        later += " Local Storage sync is now off; if a selected website's login needs page storage, rerun with --with-storage."
    return {"data": {"status": "uploaded" if uploaded else "configured",
                     "desktop_id": desktop_id, "desktop_ids": desktops, "connection_id": connection,
                     "domains": selected, "extension_domains": configured_domains,
                     "local_storage": settings["with_storage"] == 1, "local_storage_turned_off": result.get("local_storage_turned_off") is True,
                     "configured": True, "readback_verified": True, "uploaded": uploaded,
                     "received_at": after.isoformat() if after else None},
            "next": {"agent_hint": ("Upload receipt is confirmed. CUA automatically imports uploaded browser data into every desktop of the connection. "
                                    "Run browser-sync without --cdp-url and with --wait-seconds 60 to confirm website results, then refresh the selected "
                                    "remote websites to verify login." if uploaded else
                                    "Configuration is saved, but a new upload receipt is unconfirmed. Run browser-sync without --cdp-url to check the upload status; "
                                    "do not claim upload or website login success or retry automatically.") + later}}


def _owned_desktops(api):
    """The login's ready desktops, and the others it lists."""
    data = _call(api, "GET", "/v1/desktop-options")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SkillError("COOKIECLOUD_API_UNAVAILABLE", "The Gateway did not return this login's desktops; name them with --desktop-id.")
    ready, skipped = [], []
    for item in items:
        if isinstance(item, dict) and _identifier(item.get("id")) and item["id"] not in ready + skipped:
            (ready if item.get("ready") is True else skipped).append(item["id"])
    if len(ready) > MAX_DESKTOPS:
        raise SkillError("VALIDATION_ERROR", f"This login has more than {MAX_DESKTOPS} ready desktops; name them with --desktop-id.")
    return ready, skipped


def synchronize(args, api, memory=None):
    selected = domains(args.domain)
    every = getattr(args, "all_desktops", False) is True
    if every == bool(args.desktop_id):
        raise SkillError("VALIDATION_ERROR", "Name desktops with --desktop-id, or use --all-desktops.")
    desktops = [] if every else desktop_ids(args.desktop_id)
    if args.timeout_seconds < 5:
        raise SkillError("VALIDATION_ERROR", "Allow at least five seconds for Chrome configuration.")
    if not 0 <= args.wait_seconds <= MAX_WAIT_SECONDS:
        raise SkillError("VALIDATION_ERROR", f"Wait between 0 and {MAX_WAIT_SECONDS} seconds.")
    if not args.cdp_url and args.with_storage:
        return {"data": {"status": "user_action_required", "reason": "storage_change", "desktop_id": desktops[0] if desktops else None, "desktop_ids": desktops,
                         "domains": selected, "configured": False},
                "next": {"agent_hint": "Turning on Local Storage sync changes the extension. Ask the user to set Sync Local Storage? (是否同步Local Storage) to Yes (是) and save, "
                         "or to authorize the Chrome debugging connection so you can rerun with --cdp-url --with-storage."}}
    skipped = []
    if every:
        desktops, skipped = _owned_desktops(api)
        if not desktops:
            return {"data": {"status": "user_action_required", "reason": "no_ready_desktops", "desktop_ids": [], "all_desktops": True,
                             "skipped_desktops": skipped, "domains": selected, "configured": False},
                    "next": {"agent_hint": "This login has no ready desktop to sync. Start one on the user's request, or name a desktop with --desktop-id."}}
    result = _setup(args, desktops, selected, api, memory) if args.cdp_url else _check_all(args, desktops, selected, api, memory)
    if every:
        result["data"].update({"all_desktops": True, "desktop_ids": desktops, "skipped_desktops": skipped})
        if skipped:
            result["next"]["agent_hint"] += (" Desktops " + ", ".join(skipped) + " are not ready and were skipped; run browser-sync for them once they are running.")
    return result


def leave(args, api):
    """Stop one desktop's imports; the connection and its other desktops are unchanged."""
    desktop_id = desktop_ids(args.desktop_id)[0]
    try:
        view = _view(_call(api, "DELETE", _sync_path(desktop_id)), desktop_id)
    except SkillError as exc:
        if exc.code != "cookiecloud_not_connected":
            raise
        view = None
    return {"data": {"status": "left", "desktop_id": desktop_id, "connected": bool(view and view["connected"])},
            "next": {"agent_hint": "This desktop no longer imports browser sync uploads; logins already in its browser stay. The connection's other "
                     "desktops keep receiving uploads; a connection with no desktop left refuses uploads. To include this one again, "
                     "run browser-sync for it."}}


def revoke(args, api, memory=None):
    """Revoke the desktop's connection for every desktop that shares it."""
    if getattr(args, "confirm", False) is not True:
        raise SkillError("VALIDATION_ERROR", "Revoking browser sync needs --confirm after the user explicitly asks for it.")
    desktop_id = desktop_ids(args.desktop_id)[0]
    status = _status(api, desktop_id)
    if status is not None and not status["connected"]:
        return {"data": {"status": "not_connected", "desktop_id": desktop_id, "connection_id": None},
                "next": {"agent_hint": "This desktop has no active browser sync connection; nothing was revoked."}}
    connection = status["connection_id"] if status else None
    view = _view(_call(api, "POST", _sync_path(desktop_id, "/revoke"), {}), desktop_id)
    if memory and connection:
        # Forgotten only when it is the extension's connection on this computer.
        memory.forget(connection)
    return {"data": {"status": "revoked", "desktop_id": desktop_id, "connection_id": connection, "connected": view["connected"]},
            "next": {"agent_hint": "Browser sync is revoked for every desktop that shared this connection: the extension's current settings can no "
                     "longer upload and the cloud snapshots are deleted. Logins already in the desktops' browsers stay. A later browser-sync "
                     "creates a new connection and needs the Chrome debugging connection to write its settings."}}
