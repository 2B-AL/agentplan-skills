# Troubleshooting

All errors arrive as `{ "ok": false, "action": "...", "error": { "code", "message", ... } }`.
Branch on `error.code`. `reason`, `request_id`, `upstream_code`,
`upstream_status`, and safe `context` are included when available.

| code | HTTP | cause | what to do |
| --- | --- | --- | --- |
| `AUTH_REQUIRED` | 401 | no/invalid AgentPlan API key | ask the user to run `setup_command` in their own local terminal, then retry the original command |
| `TOKEN_EXPIRED` | 401 | gateway rejected the bearer credential | ask the user to run `setup_command` in their own local terminal again |
| `REFRESH_FAILED` | 401 | legacy alias for re-login needed | ask the user to run `setup_command` in their own local terminal again |
| `FORBIDDEN` | 403 | missing scope | tell the user they lack permission for this action |
| `DESKTOP_NOT_BOUND` | 403 | no CUA desktop allocated | tell the user CUA is not provisioned; contact an admin |
| `INVOCATION_NOT_FOUND` | 404 | wrong/unknown invocation id | re-check the id; do not guess. Use `--last` or the id from `delegate` |
| `INVOCATION_NOT_WAITING_INPUT` | 409 | `answer` sent but CUA is not asking | run `watch`/`task status` first to see the real state |
| `CONTEXT_NOT_FOUND` | 404 | wrong/unknown context id | re-check the id; use `context list` or `--last-context` |
| `SCHEDULE_NOT_FOUND` | 404 | wrong/unknown (or deleted) schedule id | re-check with `schedule list` |
| `ARTIFACT_NOT_FOUND` | 404 | unknown artifact, or it has no bytes | re-check with `artifact list`; a placeholder artifact has no downloadable content |
| `ACTIVE_RUN_CONFLICT` | 409 | the cloud desktop already has an active task/run, so the new task was not started | tell the user to wait until the current desktop task finishes. Do not retry or start another task; only inspect/cancel the existing task if the user explicitly asks and the response includes a usable id |
| `MODEL_TIMEOUT` | 504/5xx | the model provider timed out | report the request id and reason; retry only when safe and requested |
| `DESKTOP_UNHEALTHY` | 409/5xx | desktop or guest runtime health check failed | report the desktop/run context and request id; do not treat it as auth failure |
| `SESSION_CLEANUP` | 409/5xx | a previous session could not be cleaned up | report the task/run context and avoid retry loops |
| `UPSTREAM_FAILURE` | varies | upstream reported failure without usable diagnostics | report the request id; do not retry blindly |
| `SCHEDULE_NESTING_NOT_ALLOWED` | 409 | a scheduled task tried to manage schedules | scheduled tasks cannot create/modify other schedules; do it directly |
| `SCHEDULING_UNAVAILABLE` | 501 | this CUA backend has no scheduled-task endpoint (platform 404) | tell the user scheduling is unavailable; run the goal once with `task run`. Do NOT retry with other args or fall back to an external scheduler |
| `PAYLOAD_TOO_LARGE` | 413 | request body too large | shorten the objective/note/answer |
| `DESKTOP_NOT_FOUND` | 404 | `--desktop` id/name does not exist | run `desktop list` and use a listed id/name |
| `CUA_BACKEND_UNAVAILABLE` | 503 | CUA backend down | wait and retry, or report the outage |
| `RATE_LIMITED` | 429 | too many requests | wait, then retry |
| `VALIDATION_ERROR` | 400 | bad/missing argument (e.g. malformed `--run-at`) | fix the argument and retry |
| `NETWORK` | — | cannot reach the gateway | check connectivity / `--api-base-url`; retry |
| `INTERNAL` | 500 | unexpected | retry once; if it persists, report it |

The gateway translates the platform's raw snake_case errors (`active_run_conflict`,
`run_not_blocked`, `session_desktop_mismatch`, `nested_scheduled_task_not_allowed`,
`deleted_task`, `artifact_missing`, `payload_too_large`, …) into these stable
Skill codes, so you only ever branch on the codes above — never on raw platform codes.

## Common situations

- **"No CUA gateway configured."** The bundled `config.json` still has the
  REPLACE placeholder. Pass `--api-base-url <url>` or set
  `AP_CUA_SKILL_API_BASE_URL`, or have the publisher fill in `config.json`.
- **Login is required.** Do not run the interactive login command from the agent
  session. Show `setup_command` to the user and ask them to run it in their own
  local terminal, then retry after they confirm it finished.
- **Login fails.** Make sure the user entered a valid Volcengine Ark AgentPlan
  API key in their local terminal, or set `AP_CUA_AGENTPLAN_API_KEY` before
  running the command.
- **Task seems stuck.** `in_progress` after a wait window just means it is still
  running — `watch` again. Do not `cancel` unless the user asks.
- **Desktop already busy.** `ACTIVE_RUN_CONFLICT` means this new request did not
  start because another desktop task/run is active. This is not a task to watch
  with `--last` and not a reason to try `task run` again. Tell the user to wait
  for the current task to finish unless they explicitly ask to inspect or cancel
  it.
- **`access_url` won't open / expired.** Run `observe` again for a fresh URL.
- **Unsafe permissions on the auth file.** The script repairs to `0600`
  automatically; if it cannot, fix the filesystem and retry.
