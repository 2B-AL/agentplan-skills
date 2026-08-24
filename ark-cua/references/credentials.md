# Credential integration

Credential intents use the same `ark-cua` CLI; users do not install or invoke a second skill. The CLI first checks the existing AgentPlan `/skill/manifest` and `cua_credential_*` tools. Only after the server advertises the capability does it resolve the official `2B-AL/credential-skill` at the full commit pinned in `assets/config.json`. The versioned runtime is installed atomically under `~/.openclaw/ark-cua/dependencies/al-credential-sync`.

The trust path is:

`AgentPlan credential handle -> existing Skill Gateway tools -> caller-owned exact desktop -> signed target Credential Agent`

The local AgentPlan key remains inside the non-serializable auth handle. Pairing codes move only through the one-time encrypted relay between the source and target Agents. Secret values remain inside Credential Agent subprocesses; the CLI returns bounded status and identifiers only.

## Workflow

1. Run `credentials status [--desktop-id <id>]`. This is read-only. If the Gateway reports `TARGET_CAPABILITY_UNAVAILABLE`, stop; do not download a dependency or fall back to a model task.
2. Run `credentials setup [--desktop-id <id>]` only when setup is requested or the exact target is not ready. Add `--skip-browser` for non-browser resources.
3. Run one exact sync command:

   ```bash
   python3 scripts/cua.py credentials sync browser --desktop-id <id> <site>...
   python3 scripts/cua.py credentials sync env --desktop-id <id> <name>...
   python3 scripts/cua.py credentials sync secret --desktop-id <id> <name>...
   python3 scripts/cua.py credentials sync credential-set --desktop-id <id> --type <type> --name <name>
   python3 scripts/cua.py credentials sync file --desktop-id <id> --profile <profile>
   ```

4. Preserve the returned Job identity and let the authoritative workflow reach its terminal state. Do not start a second sync because a client-side wait ended.

Normal CUA browsing and task delegation never imply credential synchronization. Do not invent `--all`, discover extra resources, or broaden the user's requested scope.

## Browser permission boundary

The browser extension's required `host_permissions: ["https://*/*"]` is a browser capability, not business authorization. The target still requires a Vault-signed Policy for an exact HTTPS Origin, validates its digest/version and Cookie/Storage/validation scope, and checks `chrome.permissions.contains()` for that exact Origin before every task.

Do not call `chrome.permissions.request()` or `remove()`, open Options, or use `open-permissions` as setup. The internal `browser-authorize-begin/watch` Adapter actions are compatibility-only read-only observations; the normal `sync-cua.py` path does not call them. If Chrome withholds Site access, keep the same Job in `waiting_permission`, report `HOST_PERMISSION_REQUIRED`, and wait for the user to restore Site access. Never bypass the check or create a per-origin fallback.

## Reset

```bash
python3 scripts/cua.py credentials reset --desktop-id <id> [--device-id <exact-id>]
```

Reset is resumable and ordered: centrally revoke the exact Device ID first, require a confirmed `revoked` response, and only then reset the exact target. If central revocation is not confirmed, do not delete local state or reset the target.
