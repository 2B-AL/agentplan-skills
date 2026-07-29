# Auth

This AgentPlan CUA skill uses the caller's Volcengine Ark AgentPlan API key as
the bearer credential. The gateway validates that key with Ark acquire, resolves
an AgentPlan-only user principal, allocates the user's cloud desktop, and passes
the same API key to CUA runtime model calls.

## Login

1. A business command (or `auth status`) returns `AUTH_REQUIRED` when there is no
   configured API key. Do not run an interactive login command from the agent
   session. Show the user `error.setup_command` or `next.setup_command` and ask
   them to run it in their own local terminal.
2. `auth login`, when run by the user in a local terminal, asks the user to enter
   their AgentPlan API key through a terminal prompt, then calls `/v1/auth/me`
   to validate it.
3. On success it returns `status: "logged_in"` and caches the API key locally.

For non-interactive use, set `AP_CUA_AGENTPLAN_API_KEY`. `AGENTPLAN_API_KEY` is
accepted as a dedicated compatibility alias. `ARK_API_KEY` is accepted only as
a final generic compatibility fallback: an explicit AgentPlan variable and a
validated local cache take priority over it. During interactive login, the CLI
prompts for the AgentPlan key instead of silently consuming `ARK_API_KEY`.
Never print or log any key.

When stdin is not a TTY, `auth login` does not prompt or block. It returns
`AUTH_REQUIRED` with `setup_command` so the agent can ask the user to perform the
login in a real local terminal instead of pasting the API key into chat.

## Local Cache

- Location:
  `~/.ark-agentplan/ark-cua/auth-schemes/agentplan/auth.json` (the unified
  launcher sets `AP_CUA_SKILL_AUTH_FILE`).
- Permissions: `0600`; the script attempts to repair unsafe permissions and
  refuses to continue if it cannot.
- `auth.json` holds the API base URL, the API key, last verified user summary,
  and desktop binding flag. It is never printed.

## Auth Errors

| Error | Meaning | Action |
| --- | --- | --- |
| `AUTH_REQUIRED` | no API key, or the API key is invalid | ask the user to run `setup_command` in their own local terminal, then retry |
| `TOKEN_EXPIRED` | gateway rejected the bearer credential | ask the user to run `setup_command` in their own local terminal again |
| `REFRESH_FAILED` | legacy alias for re-login needed | ask the user to run `setup_command` in their own local terminal again |
| `FORBIDDEN` | API key is valid but not allowed for this operation | do not retry with the same key |

## Logout

`auth logout` clears the local cache. There is no server-side refresh token to
revoke in this AgentPlan variant.
