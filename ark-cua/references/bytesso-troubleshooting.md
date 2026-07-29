# Troubleshooting

All errors arrive as a structured envelope. `error_schema_version`, `source`,
`stage`, `accepted`, `reason`, `request_id`, `upstream_code`,
`upstream_status`, `retryable`, and `context` are included when available:

```json
{ "ok": false, "action": "...", "error": { "code": "...", "message": "...", "reason": "...", "request_id": "..." } }
```

Branch on `error.code`.

| code | cause | what to do |
| --- | --- | --- |
| `AUTH_REQUIRED` | no local CUA credential, revoked token, expired login flow, or missing auth header | run `error.retry_command`; it will print the one browser login URL to show the user |
| `FORBIDDEN` | the key is valid but not allowed for this action | tell the user they lack permission |
| `DESKTOP_NOT_BOUND` | Access Hub has not allocated a CUA desktop for the user | open Access Hub resources/setup page and allocate or contact an admin |
| `DESKTOP_BUSY` | the selected desktop already has an active run | recover/watch the active task or select another idle desktop; do not retry blindly |
| `DESKTOP_REBOOT_IN_PROGRESS` | the reboot or guest readiness checks are still running | run `error.retry_command`; do not submit a new task on that desktop yet |
| `DESKTOP_REBOOT_FAILED` or a guest readiness code | the reboot or a required component check failed | report the operation error and do not submit a new task on that desktop |
| `CONFLICT` | another operation conflicts with the requested state transition | inspect `reason`, `upstream_code`, and `context` before deciding whether to retry |
| `INVOCATION_NOT_FOUND` | wrong invocation id | use the id from `delegate` or run with `--last` |
| `INVOCATION_NOT_WAITING_INPUT` | `answer` was sent when CUA was not asking | run `watch` first |
| `CUA_BACKEND_UNAVAILABLE` | MCP gateway or CUA backend is unavailable | wait and retry |
| `GATEWAY_TIMEOUT` | CLI/nginx timed out waiting for Skill Gateway | if `accepted=unknown`, reconcile task/operation state before retrying |
| `UPSTREAM_TIMEOUT` | Skill Gateway timed out calling Access Hub or my-cua | use `source`, `stage`, `request_id`, and `context` to narrow the failing service and workflow step |
| `MODEL_TIMEOUT` | the model provider timed out | inspect diagnostics and retry only when the operation is safe |
| `DESKTOP_UNHEALTHY` | the allocated desktop or guest runtime is unhealthy | report the desktop and request id; do not treat it as an auth failure |
| `SESSION_CLEANUP` | a prior session could not be cleaned up | report the task/run context and avoid creating a retry loop |
| `RATE_LIMITED` | Skill Gateway or the model provider rejected requests due to rate limits | inspect `source`; honor `retry_after_ms` when present, then retry |
| `VALIDATION_ERROR` | bad local argument or wrong key format | fix the argument or login input |
| `NETWORK` | cannot reach Access Hub or `/skill/manifest` / `/skill/tools/{tool}` | check VPN/network and endpoint overrides |
| `UPSTREAM_PROTOCOL_ERROR` | an upstream returned malformed/non-JSON data or omitted a required field | collect request ID and inspect the named `source` |
| `UPSTREAM_FAILURE` | an internal upstream code is not part of the public enum | inspect `upstream_code`, `source`, and `stage`; do not branch automation on `upstream_code` |
| `INTERNAL` | unexpected protocol or server response | retry once; if it persists, collect logs |
| `DEPENDENCY_UNAVAILABLE` | independent Credential Skill is absent and the fixed GitHub repository cannot be reached | restore GitHub access or install a compatible standalone `al-credential-sync` |
| `DEPENDENCY_INVALID` | repository/commit pin, downloaded archive, or adapter protocol validation failed | stop; do not run the downloaded files, then repair the pinned dependency configuration |
| `TARGET_CAPABILITY_UNAVAILABLE` | the live unified Gateway has not enabled the complete production Credential surface, or the target lacks required features | finish data-plane public-key, Access Hub signer, migration, and Gateway rollout before retrying; local Agent installation has not started |
| `TARGET_NOT_AUTHORIZED` | Access Hub caller does not own the selected Credential target | select an allocated desktop; never fall back to a development target |

For `DEPENDENCY_UNAVAILABLE`, verify access to the fixed
`2B-AL/credential-skill` GitHub repository or install a compatible standalone
`al-credential-sync`. The bundled configuration pins a reviewed full commit;
do not change it to `main`, a tag, a prompt-supplied ref, or another repository.
| `BROWSER_SETUP_REQUIRED` | target Agent is paired but Connector could not finish the unpacked extension | inspect Connector/Broker health and retry the same target workflow; do not delegate a screenshot task to install it |
| `OPERATION_IN_PROGRESS` | a target mutation survived beyond the current observation window | observe the same workflow/operation; do not create a duplicate pairing or Sync Job |
| `WORKFLOW_EXPIRED` | the bounded Gateway workflow has expired | inspect the authoritative Vault Job before beginning a new target workflow |

`accepted` controls safe retry behavior:

- `false`: the operation was not accepted; retry may be safe when `retryable=true`.
- `true`: a task or operation already exists; continue with watch/result/status.
- `"unknown"`: the connection failed after a write may have reached the
  backend. Do not blindly submit it again; list/watch tasks or inspect the
  desktop operation first.

## Common Situations

- **`Expected an Access Hub bearer token starting with 'cua_api_' or 'cua_mcp_'`**:
  rerun `auth login`; do not paste an OAuth access token or browser cookie.
- **Login page opens but the command keeps waiting**: make sure the browser
  completes the Access Hub callback page. If the flow expires, rerun
  `auth login`.
- **A URL ending in `/api/v1/skill-auth/start` was shown as the login link**:
  that is a machine API, not a browser login URL. Run `auth login` and show only
  the URL printed by that command.
- **Token worked yesterday but fails now**: it may have been revoked or the
  Access Hub HMAC secret may have rotated. Rerun `auth login`.
- **Remote MCP tools still return `401 missing authorization bearer token` after
  `auth login`**: the Agent's remote MCP client is not sending
  `Authorization: Bearer <local CUA credential>`. Configure that Agent's MCP
  transport to attach the header and reload the MCP client. If the Agent does
  not support auth headers for remote MCP, use the bundled `scripts/cua.py`
  commands instead.
- **`self-test` passes but `self-test --online` fails**: Local install is fine;
  investigate network, Skill Gateway, Access Hub key, or desktop allocation.
