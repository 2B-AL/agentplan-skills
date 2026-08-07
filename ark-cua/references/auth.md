# Auth

This AgentPlan CUA skill uses the caller's Volcengine Ark AgentPlan API key as
the bearer credential. The gateway validates that key with Ark acquire, resolves
an AgentPlan-only user principal, allocates the user's cloud desktop, and passes
the same API key to CUA runtime model calls.

## Login

1. A business command (or `auth status`) reuses an environment or cached key when
   present. Otherwise it checks whether `arkcli` is in `PATH`.
2. If arkcli is installed, the CLI runs `arkcli profile list --format json`,
   selects the first profile whose `type` is exactly `agent-plan` and whose
   `plan_tier` is exactly `max`, then runs
   `arkcli profile apikey get --profile <name> --plain`. It does not use the
   current active profile and does not substitute the shell's `ARK_API_KEY` for
   arkcli's persisted profile key.
3. An arkcli-sourced key is validated through `/v1/auth/me`, used only in that
   process, and never copied into the CUA cache. Successful auth output exposes
   `api_key_source` (`arkcli`, `environment`, or `cache`), the compatible
   `credential_source` field, and `arkcli_profile` when applicable—never the key.
4. If arkcli is not installed, has no personal Agent Plan Max profile, has no
   usable key, or otherwise fails, `AUTH_REQUIRED` includes `arkcli_status`,
   `arkcli_hint`, and the existing `setup_command`. Ask the user to run that
   command in their own local terminal; it uses the hidden API-key prompt.

For non-interactive fallback use, set `AP_CUA_AGENTPLAN_API_KEY`.
`AGENTPLAN_API_KEY` and `ARK_API_KEY` remain compatibility aliases. These are
fallback inputs for CUA itself; they do not replace arkcli profile filtering.
Never print or log any key.

When stdin is not a TTY, `auth login` does not prompt or block. It returns
`AUTH_REQUIRED` with `setup_command` so the agent can ask the user to perform the
login in a real local terminal instead of pasting the API key into chat.

## Local Cache

- Location: `~/.openclaw/ark-cua/auth.json` (override with
  `AP_CUA_SKILL_AUTH_FILE`).
- Permissions: `0600`; the script attempts to repair unsafe permissions and
  refuses to continue if it cannot.
- `auth.json` holds the API base URL and, only for the manual/environment
  fallback, the API key plus last verified user summary and desktop binding
  flag. arkcli-sourced keys are never copied here. Cache contents are never
  printed.

## Auth Errors

| Error | Meaning | Action |
| --- | --- | --- |
| `AUTH_REQUIRED` | no usable key from existing configuration or arkcli, or the key is invalid | inspect `arkcli_status`; fix arkcli when practical, otherwise ask the user to run fallback `setup_command`, then retry |
| `TOKEN_EXPIRED` | gateway rejected the bearer credential | ask the user to run `setup_command` in their own local terminal again |
| `REFRESH_FAILED` | legacy alias for re-login needed | ask the user to run `setup_command` in their own local terminal again |
| `FORBIDDEN` | API key is valid but not allowed for this operation | do not retry with the same key |

## Logout

`auth logout` clears the local cache. There is no server-side refresh token to
revoke in this AgentPlan variant.
