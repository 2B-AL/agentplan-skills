"""Local caches for the ByteSSO CUA Skill CLI."""

import json
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path

from cua_util import SkillError

DEFAULT_DIR = Path.home() / ".ark-agentplan" / "ark-cua" / "auth-schemes" / "bytesso"
DEFAULT_AUTH_FILE = DEFAULT_DIR / "auth.json"


def auth_file_path():
    override = os.environ.get("CUA_SKILL_AUTH_FILE")
    return Path(override).expanduser() if override else DEFAULT_AUTH_FILE


def session_file_path():
    override = os.environ.get("CUA_SKILL_SESSION_FILE")
    if override:
        return Path(override).expanduser()
    return auth_file_path().parent / "session.json"


class _JsonFile:
    def __init__(self, path, data):
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path):
        if not path.exists():
            return cls(path, {})
        _ensure_secure_permissions(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError("INTERNAL", f"Cannot read {path}: {exc}")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise SkillError("INTERNAL", f"{path} is corrupted; run auth login again")
        return cls(path, data)

    def save(self):
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, ensure_ascii=False, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            raise SkillError("INTERNAL", f"Cannot persist {path}: {exc}")


class AuthState(_JsonFile):
    @classmethod
    def load(cls):
        return super().load(auth_file_path())

    @property
    def access_hub_base_url(self):
        return self.data.get("access_hub_base_url")

    @property
    def gateway_url(self):
        return self.data.get("gateway_url") or self.data.get("mcp_url")

    @property
    def mcp_url(self):
        return self.data.get("mcp_url")

    @property
    def bearer_key(self):
        return self.data.get("bearer_key")

    @property
    def credential_type(self):
        return self.data.get("credential_type")

    @property
    def user(self):
        return self.data.get("user") or {}

    def set_endpoints(self, *, access_hub_base_url, gateway_url):
        changed = False
        if access_hub_base_url and self.data.get("access_hub_base_url") != access_hub_base_url:
            self.data["access_hub_base_url"] = access_hub_base_url
            changed = True
        if gateway_url and self.data.get("gateway_url") != gateway_url:
            self.data["gateway_url"] = gateway_url
            changed = True
        if changed:
            self.save()

    def set_bearer_key(self, *, access_hub_base_url, gateway_url, bearer_key, user=None, credential_type=None):
        self.data.update({
            "access_hub_base_url": access_hub_base_url,
            "gateway_url": gateway_url,
            "bearer_key": bearer_key,
            "credential_type": credential_type or "access_hub_bearer",
            "user": user or {},
        })
        self.save()

    def clear_tokens(self):
        for key in ("bearer_key", "credential_type", "user"):
            self.data.pop(key, None)
        self.save()


class SessionState(_JsonFile):
    @classmethod
    def load(cls):
        return super().load(session_file_path())

    @property
    def last_invocation_id(self):
        return self.data.get("last_invocation_id")

    @property
    def default_desktop_id(self):
        return self.data.get("default_desktop_id")

    @property
    def last_task_desktop_id(self):
        return self.data.get("last_task_desktop_id")

    def set_last_invocation_id(self, invocation_id):
        if not invocation_id:
            return
        self.data["last_invocation_id"] = invocation_id
        self.save()

    def set_default_desktop_id(self, desktop_id):
        if not desktop_id:
            return
        self.data["default_desktop_id"] = desktop_id
        self.save()

    def set_last_task_desktop_id(self, desktop_id):
        if not desktop_id:
            return
        self.data["last_task_desktop_id"] = desktop_id
        self.save()

    def remember_desktops(self, desktops):
        if not isinstance(desktops, list):
            return
        current = self.data.get("desktops")
        if not isinstance(current, dict):
            current = {}
        for desktop in desktops:
            if not isinstance(desktop, dict):
                continue
            desktop_id = desktop.get("desktop_id")
            if not desktop_id:
                continue
            current[desktop_id] = {
                "name": desktop.get("instance_name") or desktop.get("name") or desktop_id,
                "last_seen_at": desktop.get("last_access_at") or desktop.get("assigned_at"),
                "is_default": bool(desktop.get("is_default")),
            }
            if desktop.get("is_default") and not self.data.get("default_desktop_id"):
                self.data["default_desktop_id"] = desktop_id
        self.data["desktops"] = current
        self.save()

    def credential_begin_request(self, desktop_id, mode):
        key = f"{desktop_id or 'default'}:{mode}"
        pending = self.data.get("credential_begin_requests")
        if not isinstance(pending, dict):
            pending = {}
        item = pending.get(key)
        if isinstance(item, dict) and int(item.get("expires_at") or 0) > int(time.time()):
            request_id = str(item.get("request_id") or "").strip()
            if request_id:
                return request_id
        request_id = "cred-" + uuid.uuid4().hex
        pending[key] = {"request_id": request_id, "expires_at": int(time.time()) + 3600}
        self.data["credential_begin_requests"] = pending
        self.save()
        return request_id

    def complete_credential_begin(self, desktop_id, mode, workflow_id, device_id=None):
        key = f"{desktop_id or 'default'}:{mode}"
        pending = self.data.get("credential_begin_requests")
        if isinstance(pending, dict):
            pending.pop(key, None)
            self.data["credential_begin_requests"] = pending
        workflows = self.data.get("credential_workflows")
        if not isinstance(workflows, dict):
            workflows = {}
        workflows[workflow_id] = {
            "desktop_id": desktop_id or "",
            "mode": mode,
            "updated_at": int(time.time()),
        }
        self.data["credential_workflows"] = workflows
        if device_id:
            devices = self.data.get("credential_devices")
            if not isinstance(devices, dict):
                devices = {}
            devices[desktop_id or "default"] = device_id
            self.data["credential_devices"] = devices
        self.save()

    def credential_device(self, desktop_id):
        devices = self.data.get("credential_devices")
        return devices.get(desktop_id or "default") if isinstance(devices, dict) else None

    def forget_credential_device(self, desktop_id):
        devices = self.data.get("credential_devices")
        if isinstance(devices, dict):
            devices.pop(desktop_id or "default", None)
            self.data["credential_devices"] = devices
            self.save()

    def credential_reset_request(self, desktop_id, device_id):
        key = f"{desktop_id}:{device_id}"
        resets = self.data.get("credential_reset_requests")
        if not isinstance(resets, dict):
            resets = {}
        current = resets.get(key)
        request_id = str(current.get("request_id") if isinstance(current, dict) else current or "").strip()
        if not request_id:
            request_id = "cred-reset-" + uuid.uuid4().hex
            resets[key] = {"request_id": request_id, "central_revoked": False}
            self.data["credential_reset_requests"] = resets
            self.save()
        return request_id

    def credential_reset_central_revoked(self, desktop_id, device_id):
        resets = self.data.get("credential_reset_requests")
        item = resets.get(f"{desktop_id}:{device_id}") if isinstance(resets, dict) else None
        return isinstance(item, dict) and item.get("central_revoked") is True

    def mark_credential_reset_central_revoked(self, desktop_id, device_id):
        key = f"{desktop_id}:{device_id}"
        resets = self.data.get("credential_reset_requests")
        if not isinstance(resets, dict):
            resets = {}
        item = resets.get(key)
        if not isinstance(item, dict):
            item = {"request_id": str(item or "cred-reset-" + uuid.uuid4().hex)}
        item["central_revoked"] = True
        resets[key] = item
        self.data["credential_reset_requests"] = resets
        self.save()

    def finish_credential_reset(self, desktop_id, device_id):
        key = f"{desktop_id}:{device_id}"
        resets = self.data.get("credential_reset_requests")
        if isinstance(resets, dict):
            resets.pop(key, None)
            self.data["credential_reset_requests"] = resets
        devices = self.data.get("credential_devices")
        if isinstance(devices, dict):
            devices.pop(desktop_id or "default", None)
            self.data["credential_devices"] = devices
        self.save()

    def remember_credential_operation(self, operation_id, workflow_id):
        operations = self.data.get("credential_operations")
        if not isinstance(operations, dict):
            operations = {}
        operations[operation_id] = workflow_id
        self.data["credential_operations"] = operations
        self.save()

    def workflow_for_credential_operation(self, operation_id):
        operations = self.data.get("credential_operations")
        return operations.get(operation_id) if isinstance(operations, dict) else None

    def finish_credential_workflow(self, workflow_id):
        workflows = self.data.get("credential_workflows")
        if isinstance(workflows, dict):
            workflows.pop(workflow_id, None)
            self.data["credential_workflows"] = workflows
        operations = self.data.get("credential_operations")
        if isinstance(operations, dict):
            self.data["credential_operations"] = {
                key: value for key, value in operations.items() if value != workflow_id
            }
        self.save()


def _ensure_secure_permissions(path):
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            os.chmod(path, 0o600)
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise SkillError("INTERNAL", f"{path} has unsafe permissions and could not be repaired")
    except OSError as exc:
        raise SkillError("INTERNAL", f"Cannot inspect {path}: {exc}")
