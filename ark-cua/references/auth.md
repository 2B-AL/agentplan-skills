# Auth

Use the user's existing AgentPlan login. The CLI handles authentication; do not read credential files or ask for API keys in chat.

## Login

1. Run `auth status`. The CLI reuses a saved login or an eligible personal AgentPlan Max account from `arkcli` without changing the user's arkcli settings.
2. On `AUTH_REQUIRED`, follow a recoverable `arkcli_hint` when available. Otherwise ask the user to run the returned `setup_command` in their own terminal and enter the API key through its hidden prompt.
3. After the user completes login, run `auth status` again. Do not treat a failed login as permission to switch accounts.

## Switch accounts

When the user explicitly requests a different account or API key, ask them to run this in their own terminal:

```bash
python3 scripts/cua.py auth login --manual
```

The user enters the key through a hidden prompt, not in chat, command arguments or environment variables. This login remains active until `auth logout`. If it is rejected, repeat the returned manual login flow rather than silently using another account.

An agent-run login command returns instructions when it cannot prompt interactively; relay them to the user.

## Environments

The bundled gateway is AgentPlan. Another gateway, such as a development environment, is selected with `--api-base-url <url>` (or `AP_CUA_SKILL_API_BASE_URL`) and needs its own login: ask the user to run the returned `setup_command`, which names that gateway, in their own terminal and enter that environment's API key or token through the hidden prompt. The login is saved for that gateway and later commands use it by default. One login is saved at a time: logging in to another gateway removes the previous one.

A credential is sent only to the gateway it belongs to: a saved login to the gateway it was saved for, and arkcli's AgentPlan key only to the bundled AgentPlan gateway. `AUTH_REQUIRED` with `gateway_login_required=true` means the selected gateway needs its own login (none is saved for it, or it rejected the saved one); never copy a key between environments. `auth logout` removes the saved login and returns later commands to the bundled gateway.

## Real-name verification

If `real_name_verification.status` is `unverified` or an operation returns `VOLCENGINE_REAL_NAME_REQUIRED`, provide `verification_url` and ask the user to complete verification on Volcengine before allocating a new desktop. Then rerun `auth status`. Existing allocated desktops remain usable; this is not an invalid API key.

## Auth Errors

| Error | Action |
| --- | --- |
| `AUTH_REQUIRED` | Follow the login steps above. When `manual_login_required=true`, use the returned manual `setup_command`; when `gateway_login_required=true`, follow Environments. |
| `TOKEN_EXPIRED` / `REFRESH_FAILED` | Ask the user to run `setup_command` in their local terminal, then retry after login. |
| `FORBIDDEN` | Report missing permission; do not retry the same operation with the same key. |
| `VOLCENGINE_REAL_NAME_REQUIRED` | Follow the verification steps above without replacing a valid login. |
| `VOLCENGINE_REAL_NAME_CHECK_UNAVAILABLE` | Report the temporary failure; check status before retrying an allocation so a desktop or task is not created twice. |

## Logout

`auth logout` removes this Skill's saved login and its gateway, so later commands use the bundled AgentPlan gateway. It does not sign out of arkcli, whose account may be reused by later commands.
