# Outcome state machine

`delegate`, `watch`, `answer`, and `result` all return the same invocation
envelope under `data`:

```json
{
  "invocation_id": "cua_inv_...",
  "outcome": "in_progress | needs_input | completed | failed | cancelled",
  "result": { "text": null, "artifacts": [] },
  "input_request": { "question": "...", "choices": [] } ,
  "progress": { "summary": "...", "step_count": 2, "updated_at": "..." },
  "next_action": { "type": "...", "agent_hint": "..." },
  "diagnostics": { "trace_id": null }
}
```

`task run`, `task continue`, `task status`, `task result`, and `task answer`
return the same envelope, plus a `platform` block of cross-system reference ids:

```json
"platform": {
  "desktop": "win10-...", "session_id": null, "run_id": "run_...",
  "context_id": "cua_ctx_...", "trace_id": null
}
```

Keep using the semantic ids (`invocation_id` / task id, `context_id`); the
`platform` ids are only for logs/dashboards and scheduled-task provenance.

The CLI also adds a top-level `next` block. For task progress this usually
contains a ready-to-run `command`. For auth setup it contains `setup_command`,
which the user must run in their own local terminal; the agent must not run it.

## Platform `resultType` → outcome

The gateway maps the platform run result onto the stable `outcome`:

| platform `resultType` / run status | outcome |
| --- | --- |
| `running`, `queued` | `in_progress` |
| `waiting_user_input`, `blocked` | `needs_input` |
| `final` | `completed` |
| `error`, `failed` | `failed` |
| `cancelled` | `cancelled` |

## Artifacts

When `outcome == completed`, `result.artifacts` lists produced files:
`{ id, kind, mime_type, size_bytes }`. Download bytes with
`artifact save --artifact-id <id>`. A `missing: true` artifact has no bytes
(placeholder only) — report it as unavailable.

## Scheduled execution status

`schedule history` returns `executions[]`, each
`{ id, status, scheduled_for, started_at, finished_at, run_id, final_text, error }`.
Read `final_text` / `run_id` to learn what a scheduled run actually did.

## Transitions

```
delegate ──> in_progress ──watch──> in_progress   (loop)
                         └────────> needs_input ──answer──> in_progress
                         └────────> completed
                         └────────> failed
                         └────────> cancelled
```

## How to handle each outcome

| outcome | what it means | what to do |
| --- | --- | --- |
| `in_progress` | CUA is still working | Run `next.command` (a `watch`). Do not answer the task yourself. Do not cancel for slowness. |
| `needs_input` | CUA needs the user | Relay `input_request.question` to the user verbatim, then `answer`. |
| `completed` | Done | Use `result.text` as the authoritative final answer. Mention `result.artifacts` if relevant. |
| `failed` | CUA could not finish | Explain the failure. Retry only if the user asks. |
| `cancelled` | Stopped | Tell the user it was cancelled. |

## Rules

- `result.text` is authoritative ONLY when `outcome == completed`. In every other
  state `result.text` is `null` — never fabricate a result from `progress` or a
  screenshot.
- A timeout is NOT an outcome. If a `watch`/`delegate` returns `in_progress`
  because its `wait_ms` elapsed, just `watch` again.
- `watch` uses a total client-side wait budget. The server caps one wait at 60
  seconds, so the CLI splits longer budgets into repeated calls.
