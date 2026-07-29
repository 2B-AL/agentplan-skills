# AgentPlan CUA Skill commands

All commands: `python3 <skill_dir>/scripts/cua.py <command> [options]`.
Global option: `--api-base-url <url>` overrides the gateway URL for one call.

Every call prints one JSON object: `{ "ok": true, "action": "...", "data": {...}, "next": {...} }`
or `{ "ok": false, "action": "...", "error": { "code": "...", "message": "..." } }`.

## auth status

Verify the configured AgentPlan API key against the gateway. Creates no task.

```bash
python3 scripts/cua.py auth status
```

```json
{"ok": true, "action": "auth status", "data": {
  "status": "logged_in",
  "auth_type": "agentplan_api_key",
  "user": {"account_id": "2100...", "project_name": "default", "apikey_id": "ak_..."},
  "scopes": ["cua:read", "cua:invoke", "cua:observe", "cua:cancel"],
  "desktop_bound": true
}}
```

If not logged in: `error.code = AUTH_REQUIRED` with `setup_command`. The agent
must ask the user to run that command in their own local terminal; the agent
must not run the interactive login command itself.

## auth login

Configure the caller's Volcengine Ark AgentPlan API key and cache it locally
(0600).

```bash
python3 scripts/cua.py auth login [--api-key <key>] [--no-prompt]
```

- Prefer the local terminal prompt or `AP_CUA_AGENTPLAN_API_KEY`.
- In a non-interactive agent-run command, `auth login` returns `AUTH_REQUIRED`
  with `setup_command` instead of blocking for input.
- Do not print or log the API key.
- Success: `data.status = "logged_in"`.

## auth logout

Clear the locally cached API key.

```bash
python3 scripts/cua.py auth logout
```

## ping

Read-only auth + desktop-binding check. Creates no CUA task.

```bash
python3 scripts/cua.py ping
```

```json
{"ok": true, "action": "ping", "data": {
  "ok": true,
  "server": {"name": "cua-mcp-server", "version": "0.1.0"},
  "auth": {"authenticated": true, "org_id": "org_x", "user_id": "user_x", "team_id": null, "desktop_bound": true},
  "agent_hint": "..."
}}
```

## delegate

Create an invocation from the user's original objective.

```bash
python3 scripts/cua.py delegate --objective "<user request>" [--wait-ms 30000]
```

- `--objective` (required): the user's request, unmodified.
- `--wait-ms`: total client-side wait budget. Calls above 60 seconds are split
  into repeated gateway waits. It does NOT cancel the task.

`data` is the invocation envelope (see `outcomes.md`). `next.command` tells you
what to run next.

## watch

Wait for or check an invocation's next state.

```bash
python3 scripts/cua.py watch (--invocation-id <id> | --last) [--wait-ms 60000]
```

`--wait-ms` is the total client-side budget, not one server long poll. For
example, `--wait-ms 900000` can remain connected for up to 15 minutes while the
CLI uses individual waits of at most 60 seconds.

## answer

Submit the user's answer when `outcome == needs_input`.

```bash
python3 scripts/cua.py answer (--invocation-id <id> | --last) --answer "<reply>" [--wait-ms 60000]
```

## cancel

Request cancellation. Use only when the user asks to stop.

```bash
python3 scripts/cua.py cancel (--invocation-id <id> | --last)
```

```json
{"ok": true, "action": "cancel", "data": {
  "invocation_id": "cua_inv_...", "cancel_requested": true, "outcome": "in_progress", "agent_hint": "..."
}}
```

## result

Block until a terminal/needs_input outcome and return the authoritative result.
Internally polls `watch`.

```bash
python3 scripts/cua.py result (--invocation-id <id> | --last) [--timeout 600]
```

## observe

Get short-lived desktop view links, optionally a screenshot.

```bash
python3 scripts/cua.py observe [--invocation-id <id> | --last] [--include-screenshot]
```

- `access_url` — the gateway-owned temporary entry URL. New multi-desktop
  gateways return the full interface directly as
  `https://<host>/desktops/<desktop-id>/cua-app/?ticket=...`.
- `full_interface_url` — the full CUA interface (desktop + the agent panel). It
  is identical to `access_url` for the new namespaced route. Legacy gateways
  may still return a bare `https://<host>/<desktop>` URL, in which case the CLI
  derives the legacy `https://<host>/cua-app/<desktop>` form.
- `desktop_view_url` — an optional desktop-only URL. It is omitted when the
  namespaced gateway response does not provide a distinct desktop-only entry.
  Offer `full_interface_url` when the user wants to watch CUA work.
- These links are temporary; if one expires, run `observe` again.
- `--include-screenshot` saves the image to a local file and returns
  `data.screenshot_file` plus `data.screenshot` metadata. The raw image bytes are
  never printed.

Example `data`:

```json
{
  "access_url": "https://sd8f5rf313dl1693i3fmg.apigateway-cn-beijing.volceapi.com/desktops/desk-15/cua-app/?ticket=dt_xxx",
  "full_interface_url": "https://sd8f5rf313dl1693i3fmg.apigateway-cn-beijing.volceapi.com/desktops/desk-15/cua-app/?ticket=dt_xxx"
}
```

## self-test

Local-only checks (Python version, cache file, login state). Creates no task.

```bash
python3 scripts/cua.py self-test
```

---

# Semantic command surface (resource-aware)

`delegate/watch/answer/cancel/result/observe` remain the simple default. The
commands below add desktop selection, reusable contexts, artifacts, and
scheduled tasks. They go through the same gateway (`/v1/**`); the agent never
touches the platform `/api/**` directly. Every command still prints one JSON
object. Most accept `--last*` to reuse the most recent id from local cache.

## diagnose

Confirm CUA is reachable and a desktop is bound. Creates no task. Prefer this
over `delegate`-to-test.

```bash
python3 scripts/cua.py diagnose
```

## desktop list

List selectable cloud desktops (`id`, `name`, `ready`). Use when the user wants
a specific desktop, then pass it to `task run --desktop`.

```bash
python3 scripts/cua.py desktop list
```

## desktop access / revoke-access / reboot / reset / operation get

Desktop access returns a short-lived Desktop Gateway URL for the bound desktop.
`revoke-access` immediately invalidates a previously returned ticket. Lifecycle
commands are asynchronous and return an operation id.

```bash
python3 scripts/cua.py desktop access
python3 scripts/cua.py desktop revoke-access (--ticket <ticket> | --access-url <url>)
python3 scripts/cua.py desktop reboot [--desktop <id>] [--idempotency-key <key>]
python3 scripts/cua.py desktop reset --confirm [--desktop <id>] [--idempotency-key <key>]
python3 scripts/cua.py desktop operation get (--operation-id <id> | --last)
```

Use `desktop revoke-access` when a temporary access URL may have leaked or is no
longer needed. Use `desktop operation get` until `data.terminal == true`.
`reset` always requires `--confirm`.

## model get / model set

Read or update the default model config for the bound CUA desktop. Use only
when the user explicitly asks which model CUA uses or asks to change model /
reasoning effort. This is persistent for future delegations on the bound
desktop; it is not a one-task override.

```bash
python3 scripts/cua.py model get
python3 scripts/cua.py model set --main-model deepseek-v4-pro --reasoning-effort high
```

`model get` returns:

```json
{
  "desktop": {"id": "desktop_xxx", "name": "win10-spice-xxx"},
  "model_config": {
    "main_model": "deepseek-v4-pro",
    "reasoning_effort": "high",
    "source": "settings"
  },
  "available_models": [
    {"id": "deepseek-v4-pro", "label": "deepseek-v4-pro", "description": "..."}
  ],
  "reasoning_effort_options": ["low", "medium", "high"],
  "agent_hint": "..."
}
```

## task run

Start a new task. Like `delegate`, but can target a desktop and creates a
reusable context (returned as `data.platform.context_id`).

```bash
python3 scripts/cua.py task run --objective "<request>" [--desktop <id-or-name>] \
    [--title "<context title>"] [--disable-ask-user] [--wait-ms 0]
```

`data` is the task envelope (same shape as the invocation envelope, plus a
`platform` block with `desktop`, `run_id`, `context_id`, `trace_id`). Drive the
outcome with `task status` / `task result` / `task answer`, just like watch/answer.

## task continue

Continue work in an existing context (keeps prior session state).

```bash
python3 scripts/cua.py task continue (--context-id <id> | --last-context) --objective "<next step>" [--disable-ask-user] [--wait-ms 0]
```

## task status / task result / task answer / task cancel

```bash
python3 scripts/cua.py task status (--task-id <id> | --last)
python3 scripts/cua.py task result (--task-id <id> | --last) [--timeout 600]
python3 scripts/cua.py task answer (--task-id <id> | --last) --answer "<reply>"
python3 scripts/cua.py task cancel (--task-id <id> | --last)
```

`task-id` shares the same id space as `invocation_id`, so `--last` works after a
plain `delegate` too.

## context list / create / add-note / show

```bash
python3 scripts/cua.py context list
python3 scripts/cua.py context create [--title "<t>"] [--desktop <id-or-name>]
python3 scripts/cua.py context add-note (--context-id <id> | --last-context) --text "<background>"
python3 scripts/cua.py context show (--context-id <id> | --last-context)
```

`context create` / `add-note` record context without starting a run. Run work
later with `task continue`.

## timeline show

Full conversation timeline projection for a context (for review/debugging).

```bash
python3 scripts/cua.py timeline show (--context-id <id> | --last-context)
```

## artifact list / save

```bash
python3 scripts/cua.py artifact list (--task-id <id> | --last)
python3 scripts/cua.py artifact save (--artifact-id <id> | --last) [--task-id <id>] [--output <path>]
```

`artifact save` writes the file to `--output` (or a temp file named by content
type) and returns `data.file`, `data.mime_type`, `data.bytes`,
`data.source_artifact_id`. New gateways return raw bytes; during migration the
CLI can still decode a legacy JSON/base64 envelope. The bytes are never printed.
If the artifact has no downloadable content, `data.missing` is `true` with
`data.placeholder_text` — do NOT claim a file was saved.

## schedule create-once / create-recurring

Use these when the user wants a future or recurring task ("today at 8pm",
"every day at 9am"). Do NOT run the goal immediately unless the user also asks
for a one-off now.

```bash
python3 scripts/cua.py schedule create-once --goal "<goal>" --run-at 2026-06-25T20:00:00Z \
    [--title "<t>"] [--desktop <id-or-name>]
python3 scripts/cua.py schedule create-recurring --goal "<goal>" --start-at 2026-06-25T09:00:00Z \
    --interval-hours 24 [--allowed-start-window-ms <n>] [--title "<t>"] [--desktop <id-or-name>]
```

Times must be ISO-8601 (`...Z` or `+08:00`). `--interval-hours` minimum is 1.
To bind to a current context instead of a fresh scheduled one, add
`--context-mode current --context-id <id>`.

## schedule list / status / history / stop / delete

```bash
python3 scripts/cua.py schedule list
python3 scripts/cua.py schedule status (--schedule-id <id> | --last)
python3 scripts/cua.py schedule history (--schedule-id <id> | --last)
python3 scripts/cua.py schedule stop (--schedule-id <id> | --last)
python3 scripts/cua.py schedule delete (--schedule-id <id> | --last)
```

Read actual run outcomes with `schedule history` after the scheduled time.
