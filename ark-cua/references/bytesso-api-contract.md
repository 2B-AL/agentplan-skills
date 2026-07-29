# API Contract

The CLI talks to the AP-style CUA Skill Gateway:

```text
GET  http://10.37.98.200/skill/manifest
POST http://10.37.98.200/skill/tools/{tool}
Authorization: Bearer cua_api_...
Content-Type: application/json
Accept: application/json
```

Legacy `cua_mcp_...` bearer keys are still accepted for compatibility.

## Tools Used By This Skill

| CLI command | Gateway tool |
| --- | --- |
| `ping` | `cua_get_desktop_access` plus `GET /skill/manifest` |
| `desktops list` | `cua_list_desktops` |
| `desktops allocate` | `cua_allocate_desktop` |
| `desktops use` | `cua_list_desktops` plus local state update |
| `desktops reboot` | `cua_reboot_desktop`, then `cua_get_desktop_operation` until terminal |
| `desktops operation` | `cua_get_desktop_operation` until terminal |
| `delegate` | `cua_run_task` |
| `tasks list` | `cua_list_tasks` |
| `tasks watch` | `cua_watch_tasks` |
| `watch` | `cua_wait_task` |
| `answer` | `cua_resume_task` |
| `cancel` | `cua_cancel_task` |
| `observe` | `cua_get_desktop_access`, optionally `cua_take_screenshot` |
| `credentials status` | `cua_credential_capabilities` |
| `credential-target begin` | `cua_credential_begin` plus private pair-relay begin/complete when required |
| `credential-target health` | `cua_credential_health` |
| browser authorization/network | corresponding `cua_credential_browser_*` tool |
| `credential-target finish` | `cua_credential_finish` |
| `credentials reset` | central Agent revoke, then `cua_credential_reset` |

Credential Tool results contain only opaque workflow/operation/device IDs and
bounded readiness. Pair relay ciphertext is available only from the private
CLI endpoint and is written directly to local Agent stdin. Operation
capabilities, my-cua session IDs, relay keys/envelopes, and upstream Connector
objects are never part of a public Tool or Adapter response.

Production Credential calls always resolve caller ownership and exact desktop
through Access Hub. They use short-lived Ed25519 operation capabilities between
Gateway and my-cua. This contract has no development fallback and never reads
`my-cua-dev` configuration.

For a transport failure before Access Hub signs a capability, the Gateway
returns `accepted=false`. If my-cua may already have received a mutation but
the response is unavailable, it returns `accepted=unknown` together with the
same opaque workflow, operation, and request IDs. The caller inspects/resumes
that identity and never creates a second workflow or Vault Job blindly.

## Gateway Envelope

Success:

```json
{
  "ok": true,
  "tool": "cua_run_task",
  "result": {}
}
```

Failure:

```json
{
  "ok": false,
  "tool": "cua_run_task",
  "error": {
    "error_schema_version": "cua.error.v1",
    "code": "UPSTREAM_TIMEOUT",
    "message": "my_cua request timed out",
    "source": "my_cua",
    "stage": "run_create",
    "accepted": "unknown",
    "request_id": "req_...",
    "upstream_code": "UpstreamTimeout",
    "upstream_status": 504,
    "retryable": true,
    "reason": "deadline_exceeded",
    "context": {
      "desktop_id": "desk_...",
      "session_id": "session_...",
      "task_id": "task_..."
    }
  }
}
```

`code` is the stable public code. `upstream_code` is diagnostic only and may
change as internal services evolve. `source` identifies the failing service
boundary, `stage` identifies the workflow step, and `accepted` is `false`,
`true`, or `"unknown"`. Do not blindly replay a state-changing request when
`accepted` is `"unknown"`; reconcile task or desktop operation state first.

Failed CLI commands write the same single-line JSON envelope to stdout and
stderr before exiting 1. This preserves stdout consumers and lets stderr-only
runners retain the real error. Consumers that merge both streams should
de-duplicate identical complete JSON lines.

A task that was accepted and later failed is still returned as a successful
watch/result HTTP call. Its invocation envelope uses `outcome=failed` and puts
the same stable error shape in `result.error`; diagnostic fields are also
copied to `diagnostics` for compatibility. Unknown my-cua runtime codes become
`UPSTREAM_FAILURE`, with the original safe code retained in `upstream_code`.

## Output Mapping

The CLI maps gateway `task_id` to the existing `invocation_id` field so agents
can keep using `watch --last` and `answer --last`.

When multiple desktops exist, gateway tools accept `desktop_id` where relevant.
`task_id` remains globally sufficient for `watch`, `answer`, `cancel`, and
result lookup; those commands do not need a desktop selector.

One `cua_run_task` creates one CUA task and one new run. When `session_id` is
omitted, the gateway first creates a new my-cua session. When `session_id` is
provided, the new run is created in that existing session so it can continue
with the session's prior context. The session belongs to its original desktop;
callers with multiple desktops should send that same `desktop_id` together with
`session_id`.

The CLI exposes the gateway response field `mycua_session_id` as
`data.session_id` for single-task commands and `data.tasks[].session_id` for
`tasks watch`. It also keeps the value in the corresponding
`diagnostics.mycua_session_id`. An invocation/task id and a session id are
different identifiers and are not interchangeable.

To run independent work in parallel, omit `session_id`, call `cua_run_task`
once per subtask, and collect results with `cua_watch_tasks`.

`cua_list_desktops` may include scheduling hints on each desktop:

```json
{
  "desktop_id": "desk-02",
  "instance_name": "win10-spice-desk-02",
  "busy": true,
  "current_task_id": "cua_task_...",
  "current_task_status": "running"
}
```

The CLI uses these hints for `delegate --auto`; they are advisory and the
gateway remains authoritative when creating the task.

`cua_reboot_desktop` does not require a confirmation field. It returns an
asynchronous operation with public status `running`, `succeeded`, or `failed`.
The CLI polls `cua_get_desktop_operation` and returns success only after the
operation reaches `succeeded`, which means the virtual machine and required
guest components passed readiness checks. The gateway also rejects new task
creation while the latest reboot has not succeeded.
