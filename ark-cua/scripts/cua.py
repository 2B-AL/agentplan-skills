#!/usr/bin/env python3
"""Unified ARK CUA launcher.

The launcher resolves one explicit deployment scheme, rejects capabilities that
the selected deployment does not provide, forces scheme-scoped state below
~/.ark-agentplan/ark-cua, and then execs the complete vendored adapter.
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path


SCHEMES = ("agentplan", "bytesso")
COMMON_ROOTS = frozenset(
    {"auth", "ping", "delegate", "watch", "answer", "cancel", "observe", "self-test"}
)
AGENTPLAN_ROOTS = COMMON_ROOTS | frozenset(
    {
        "result",
        "diagnose",
        "desktop",
        "desktops",
        "model",
        "task",
        "context",
        "timeline",
        "artifact",
        "schedule",
    }
)
BYTESSO_ROOTS = COMMON_ROOTS | frozenset(
    {"desktop", "desktops", "tasks", "credentials", "credential-target"}
)
AGENTPLAN_DESKTOPS = frozenset(
    {"list", "access", "revoke-access", "reboot", "reset", "operation"}
)
BYTESSO_DESKTOPS = frozenset({"list", "allocate", "use", "reboot", "operation"})


def _skill_dir():
    return Path(__file__).resolve().parent.parent


def _config():
    path = _skill_dir() / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _error("CONFIG_INVALID", f"Cannot read bundled config: {exc}", stage="scheme_resolution")
    if not isinstance(data, dict):
        _error("CONFIG_INVALID", "Bundled config must be a JSON object.", stage="scheme_resolution")
    return data


def _state_root(config):
    value = os.environ.get("ARK_CUA_STATE_DIR") or config.get("state_root")
    if not value:
        value = str(Path.home() / ".ark-agentplan" / "ark-cua")
    return Path(value).expanduser()


def _selection_path(config):
    return _state_root(config) / "selection.json"


def _read_persisted_selection(config):
    path = _selection_path(config)
    if not path.exists():
        return None
    _secure_existing_file(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _error("STATE_INVALID", f"Cannot read scheme selection: {exc}", stage="scheme_resolution")
    scheme = data.get("auth_scheme") if isinstance(data, dict) else None
    return _validate_scheme(scheme, source="persisted selection") if scheme else None


def _write_persisted_selection(config, scheme):
    path = _selection_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
        fd, temp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".selection-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"auth_scheme": scheme}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    except OSError as exc:
        _error("STATE_WRITE_FAILED", f"Cannot save scheme selection: {exc}", stage="scheme_resolution")


def _reset_persisted_selection(config):
    path = _selection_path(config)
    if not path.exists():
        return
    _secure_existing_file(path)
    try:
        path.unlink()
    except OSError as exc:
        _error("STATE_WRITE_FAILED", f"Cannot reset scheme selection: {exc}", stage="scheme_resolution")


def _secure_existing_file(path):
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            _error("STATE_INVALID", f"State path is not a regular file: {path}", stage="scheme_resolution")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.chmod(path, 0o600)
    except OSError as exc:
        _error("STATE_INVALID", f"Cannot secure state file: {exc}", stage="scheme_resolution")


def _validate_scheme(value, source):
    normalized = str(value or "").strip().lower()
    if normalized not in SCHEMES:
        _error(
            "INVALID_AUTH_SCHEME",
            f"Unsupported auth scheme from {source}: {value!r}.",
            stage="scheme_resolution",
            available_schemes=list(SCHEMES),
        )
    return normalized


def _extract_cli_scheme(argv):
    scheme = None
    remaining = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--auth-scheme":
            if scheme is not None:
                _error("VALIDATION_ERROR", "--auth-scheme may be specified only once.")
            if index + 1 >= len(argv):
                _error("VALIDATION_ERROR", "--auth-scheme requires agentplan or bytesso.")
            scheme = _validate_scheme(argv[index + 1], source="--auth-scheme")
            index += 2
            continue
        if item.startswith("--auth-scheme="):
            if scheme is not None:
                _error("VALIDATION_ERROR", "--auth-scheme may be specified only once.")
            scheme = _validate_scheme(item.split("=", 1)[1], source="--auth-scheme")
            index += 1
            continue
        remaining.append(item)
        index += 1
    return scheme, remaining


def _resolve_scheme(config, cli_scheme):
    if cli_scheme:
        return cli_scheme, "cli"
    env_scheme = os.environ.get("ARK_CUA_AUTH_SCHEME")
    if env_scheme:
        return _validate_scheme(env_scheme, source="ARK_CUA_AUTH_SCHEME"), "environment"
    persisted = _read_persisted_selection(config)
    if persisted:
        return persisted, "persisted"
    default = config.get("default_auth_scheme") or "agentplan"
    return _validate_scheme(default, source="bundled default"), "default"


def _handle_scheme_command(config, argv, cli_scheme):
    if not argv or argv[0] != "auth-scheme":
        return False
    subcommand = argv[1] if len(argv) > 1 else "status"
    if subcommand == "status" and len(argv) == 2:
        scheme, source = _resolve_scheme(config, cli_scheme)
        _success(
            "auth-scheme status",
            {
                "auth_scheme": scheme,
                "source": source,
                "default": config.get("default_auth_scheme") or "agentplan",
                "state_root": str(_state_root(config)),
                "available_schemes": list(SCHEMES),
            },
        )
    if subcommand == "use" and len(argv) == 3:
        scheme = _validate_scheme(argv[2], source="auth-scheme use")
        _write_persisted_selection(config, scheme)
        _success(
            "auth-scheme use",
            {"auth_scheme": scheme, "source": "persisted", "state_root": str(_state_root(config))},
        )
    if subcommand == "reset" and len(argv) == 2:
        _reset_persisted_selection(config)
        scheme = _validate_scheme(
            config.get("default_auth_scheme") or "agentplan", source="bundled default"
        )
        _success(
            "auth-scheme reset",
            {"auth_scheme": scheme, "source": "default", "state_root": str(_state_root(config))},
        )
    _error(
        "VALIDATION_ERROR",
        "Usage: auth-scheme status | auth-scheme use <agentplan|bytesso> | auth-scheme reset",
    )
    return True


def _command_path(argv):
    known_roots = AGENTPLAN_ROOTS | BYTESSO_ROOTS | frozenset({"auth-scheme"})
    for index, item in enumerate(argv):
        if item not in known_roots:
            continue
        subcommand = None
        for candidate in argv[index + 1 :]:
            if not candidate.startswith("-"):
                subcommand = candidate
                break
        return item, subcommand
    return None, None


def _capability_gate(scheme, argv):
    root, subcommand = _command_path(argv)
    if not root or root in ("-h", "--help"):
        return
    allowed_roots = AGENTPLAN_ROOTS if scheme == "agentplan" else BYTESSO_ROOTS
    if root not in allowed_roots:
        _capability_unavailable(scheme, " ".join(x for x in (root, subcommand) if x))

    if root in ("desktop", "desktops") and subcommand:
        allowed = AGENTPLAN_DESKTOPS if scheme == "agentplan" else BYTESSO_DESKTOPS
        if subcommand not in allowed:
            _capability_unavailable(scheme, f"desktops {subcommand}")

    if root == "delegate" and scheme == "agentplan":
        if "--auto" in argv:
            _capability_unavailable(scheme, "delegate --auto", available_in=["bytesso"])
        if "--session-id" in argv or any(item.startswith("--session-id=") for item in argv):
            _capability_unavailable(
                scheme, "delegate --session-id", available_in=["bytesso"]
            )


def _capability_unavailable(scheme, command, available_in=None):
    if available_in is None:
        other = "bytesso" if scheme == "agentplan" else "agentplan"
        available_in = [other]
    _error(
        "CAPABILITY_UNAVAILABLE",
        f"{command!r} is not available under auth scheme {scheme!r}.",
        stage="capability_gate",
        scheme=scheme,
        command=command,
        available_in=available_in,
    )


def _adapter_argv(scheme, argv):
    translated = list(argv)
    if translated:
        if scheme == "agentplan" and translated[0] == "desktops":
            translated[0] = "desktop"
        elif scheme == "bytesso" and translated[0] == "desktop":
            translated[0] = "desktops"
    return translated


def _adapter_environment(config, scheme):
    env = os.environ.copy()
    root = _state_root(config)
    scheme_root = root / "auth-schemes" / scheme
    env["ARK_CUA_ENTRYPOINT"] = str(Path(__file__).resolve())
    env["ARK_CUA_ACTIVE_SCHEME"] = scheme

    if scheme == "agentplan":
        env["AP_CUA_SKILL_AUTH_FILE"] = str(scheme_root / "auth.json")
        env["AP_CUA_SKILL_SESSION_FILE"] = str(scheme_root / "session.json")
        env["CUA_SKILL_AUTH_FILE"] = str(scheme_root / "auth.json")
        env["CUA_SKILL_SESSION_FILE"] = str(scheme_root / "session.json")
        override = env.get("ARK_CUA_AGENTPLAN_API_BASE_URL")
        if override:
            env["AP_CUA_SKILL_API_BASE_URL"] = override
    else:
        env["CUA_SKILL_AUTH_FILE"] = str(scheme_root / "auth.json")
        env["CUA_SKILL_SESSION_FILE"] = str(scheme_root / "session.json")
        env["ARK_CUA_BYTESSO_RUNTIME_ROOT"] = str(root / "runtime" / "bytesso")
        access_hub = env.get("ARK_CUA_BYTESSO_ACCESS_HUB_BASE_URL")
        gateway = env.get("ARK_CUA_BYTESSO_SKILL_GATEWAY_URL")
        if access_hub:
            env["CUA_SKILL_ACCESS_HUB_BASE_URL"] = access_hub
        if gateway:
            env["CUA_SKILL_GATEWAY_URL"] = gateway
    return env


def _adapter_script(scheme):
    path = _skill_dir() / "vendor" / scheme / "scripts" / "cua.py"
    if not path.is_file():
        _error(
            "INSTALL_INCOMPLETE",
            f"Missing {scheme} adapter: {path}",
            stage="adapter_resolution",
        )
    return path


def _success(action, data):
    print(json.dumps({"ok": True, "action": action, "data": data}, ensure_ascii=False))
    raise SystemExit(0)


def _error(code, message, stage="local_validation", **details):
    error = {
        "error_schema_version": "ark-cua.error.v1",
        "code": code,
        "message": message,
        "source": "unified_cli",
        "stage": stage,
        "accepted": False,
    }
    error.update({key: value for key, value in details.items() if value is not None})
    payload = {"ok": False, "action": "ark-cua", "error": error}
    line = json.dumps(payload, ensure_ascii=False)
    print(line)
    print(line, file=sys.stderr)
    raise SystemExit(1)


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    config = _config()
    cli_scheme, remaining = _extract_cli_scheme(raw_argv)
    _handle_scheme_command(config, remaining, cli_scheme)
    scheme, _source = _resolve_scheme(config, cli_scheme)
    _capability_gate(scheme, remaining)
    adapter = _adapter_script(scheme)
    adapter_argv = _adapter_argv(scheme, remaining)
    env = _adapter_environment(config, scheme)
    os.execve(sys.executable, [sys.executable, str(adapter), *adapter_argv], env)


if __name__ == "__main__":
    main()
