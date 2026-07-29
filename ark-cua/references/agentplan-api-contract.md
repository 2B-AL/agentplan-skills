# Gateway REST API contract

The skill talks to the CUA Skill Gateway over HTTPS JSON. You never call this
directly — `scripts/cua.py` does. This is reference only.

Base URL comes from `config.json`, `AP_CUA_SKILL_API_BASE_URL`, or `--api-base-url`.
All paths are under `/v1`.

## Unified envelope

Success:

```json
{ "ok": true, "request_id": "req_...", "data": { }, "error": null }
```

Error:

```json
{ "ok": false, "request_id": "req_...", "data": null,
  "error": { "code": "TOKEN_EXPIRED", "message": "Access token expired.", "retryable": true } }
```

`ACTIVE_RUN_CONFLICT` is a 409 admission result, not a started task. The gateway
may include diagnostic fields such as `conflict_scope`, `active_run`,
`active_task`, `active_task_id`, or `allowed_actions`; these fields are
sanitized context for explaining the conflict. Do not treat them as a command to
probe automatically. Tell the user to wait unless they explicitly ask to inspect
or cancel the existing task.

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/manifest` | none | capability declaration |
| `GET` | `/v1/auth/me` | AgentPlan bearer | identity + scopes |
| `GET` | `/v1/ping` | AgentPlan bearer | auth + desktop-binding check |
| `GET` | `/v1/model-config` | AgentPlan bearer | read bound desktop default model config |
| `POST` | `/v1/model-config` | AgentPlan bearer | set bound desktop default model config (`{main_model, reasoning_effort}`) |
| `POST` | `/v1/invocations` | AgentPlan bearer | delegate (`{objective, wait_ms}`) |
| `GET` | `/v1/invocations/{id}` | AgentPlan bearer | current invocation state |
| `POST` | `/v1/invocations/{id}/watch` | AgentPlan bearer | wait for next state (`{wait_ms}`) |
| `POST` | `/v1/invocations/{id}/answer` | AgentPlan bearer | submit answer (`{answer, wait_ms}`) |
| `POST` | `/v1/invocations/{id}/cancel` | AgentPlan bearer | request cancellation |
| `GET` | `/v1/desktop/access` | AgentPlan bearer | temporary desktop access URL (default desktop) |
| `POST` | `/v1/desktop/access/revoke` | AgentPlan bearer | revoke a temporary desktop access ticket (`{ticket}` or `{access_url}`) |
| `GET` | `/v1/desktop/screenshot` | AgentPlan bearer | screenshot of the default desktop |
| `POST` | `/v1/desktop/reboot` | AgentPlan bearer | reboot the caller's bound desktop (`{desktop_id?, idempotency_key?}`) |
| `POST` | `/v1/desktop/reset` | AgentPlan bearer | reset the caller's bound desktop (`{desktop_id?, confirm:true, idempotency_key?}`) |
| `GET` | `/v1/desktop/operations/{id}` | AgentPlan bearer | lifecycle operation status |
| `GET` | `/v1/invocations/{id}/desktop/access` | AgentPlan bearer | access URL for the invocation's desktop |
| `GET` | `/v1/invocations/{id}/desktop/screenshot` | AgentPlan bearer | screenshot of the invocation's desktop |
| `GET` | `/v1/diagnostics` | AgentPlan bearer | reachability + desktop binding summary |
| `GET` | `/v1/desktop-options` | AgentPlan bearer | selectable desktops (`id`, `name`, `ready`) |
| `POST` | `/v1/tasks` | AgentPlan bearer | start a task (`{objective, desktop?, title?, context_id?, disable_ask_user?, wait_ms?}`) |
| `GET` | `/v1/tasks/{id}` | AgentPlan bearer | task state |
| `GET` | `/v1/tasks/{id}/result` | AgentPlan bearer | authoritative task result |
| `GET` | `/v1/tasks/{id}/artifacts` | AgentPlan bearer | task artifacts |
| `POST` | `/v1/tasks/{id}/answer` | AgentPlan bearer | answer (`{answer, wait_ms}`) |
| `POST` | `/v1/tasks/{id}/cancel` | AgentPlan bearer | cancel |
| `GET` | `/v1/contexts` | AgentPlan bearer | list contexts |
| `POST` | `/v1/contexts` | AgentPlan bearer | create context (`{title?, desktop?}`) |
| `GET` | `/v1/contexts/{id}` | AgentPlan bearer | context summary |
| `POST` | `/v1/contexts/{id}/notes` | AgentPlan bearer | add a note (`{text}`) |
| `POST` | `/v1/contexts/{id}/tasks` | AgentPlan bearer | continue (`{objective, wait_ms}`) |
| `GET` | `/v1/contexts/{id}/timeline` | AgentPlan bearer | conversation timeline |
| `GET` | `/v1/schedules` | AgentPlan bearer | list scheduled tasks |
| `POST` | `/v1/schedules/once` | AgentPlan bearer | one-off (`{goal, run_at, ...}`) |
| `POST` | `/v1/schedules/recurring` | AgentPlan bearer | recurring (`{goal, start_at, interval_hours, ...}`) |
| `GET` | `/v1/schedules/{id}` | AgentPlan bearer | schedule status |
| `GET` | `/v1/schedules/{id}/history` | AgentPlan bearer | executions + results |
| `POST` | `/v1/schedules/{id}/stop` | AgentPlan bearer | stop future triggers |
| `DELETE` | `/v1/schedules/{id}` | AgentPlan bearer | delete schedule |
| `GET` | `/v1/artifacts/{id}/content?task_id={task_id}` | AgentPlan bearer | raw artifact bytes; legacy JSON/base64 may be accepted during migration only |

The gateway owns all platform `/api/**` calls (desktops, sessions, runs,
scheduled-tasks, artifacts) behind these stable semantic routes; the skill never
touches the platform directly.

## Auth/me response

```json
{
  "auth_type": "agentplan_api_key",
  "user": { "account_id": "2100...", "project_name": "default", "apikey_id": "ak_..." },
  "scopes": ["cua:read", "cua:invoke", "cua:observe", "cua:cancel"],
  "desktop_bound": true
}
```

## Auth model

- Login: the user provides a Volcengine Ark AgentPlan API key locally.
- Bearer credential: the API key is sent as `Authorization: Bearer`.
- Gateway verification: each business request calls Ark acquire (or uses an
  equivalent verified context) before reaching CUA.
- Runtime model calls: the same API key is passed to CUA so the user's AgentPlan
  quota and permissions are used.

## Privacy

The gateway does not persist the user's objective, answers, final result text,
process traces, screenshots, full desktop access URLs, or artifact contents.
