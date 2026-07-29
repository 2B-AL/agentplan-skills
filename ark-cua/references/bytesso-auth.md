# Auth

This skill uses the bare-metal Access Hub ByteSSO flow.

## Flow

1. Run:

   ```bash
   python3 <skill_dir>/scripts/cua.py auth login
   ```

2. The script calls Access Hub skill-auth start and prints a login URL like:

   ```text
   http://10.37.98.200/cua-access/auth/sso/start?next=...
   ```

3. The user finishes ByteSSO login in the browser.
4. Access Hub allocates or resolves the user's CUA desktop.
5. Access Hub completes the login flow and returns a local CUA credential that
   starts with `cua_api_` to the waiting script.
6. The script validates the credential with read-only `cua_get_desktop_access`
   and stores it in:

   ```text
   ~/.ark-agentplan/ark-cua/auth-schemes/bytesso/auth.json
   ```

The script uses `Authorization: Bearer <cua_api_...>` for all calls to the CUA
Skill Gateway.

Remote MCP clients use the same credential model: connect to the `mcp_url`
returned by `auth login` and configure the client to send
`Authorization: Bearer <local CUA credential>`. The exact config file and reload
behavior are Agent-specific. If an Agent cannot attach HTTP auth headers to a
remote MCP server, use the bundled `scripts/cua.py` commands as the portable
fallback instead of calling remote MCP directly.

Important: `/api/v1/skill-auth/start` and `/api/v1/skill-auth/poll` are machine
APIs. Do not present either endpoint as a browser login URL. The only browser
URL to show to the user is the `login_url` printed by `auth login`.

## Legacy Key Setup

Existing Access Hub bearer keys that start with `cua_mcp_` can still be loaded
through stdin. Use stdin, not a command-line argument:

```bash
printf '%s' "$CUA_MCP_KEY" | python3 <skill_dir>/scripts/cua.py auth login --bearer-key-stdin
```

The environment variable `CUA_SKILL_BEARER_KEY` is also supported for temporary
sessions. Prefer the local encrypted/permissioned Agent config or the CLI cache
over long-lived shell environment variables.

## Endpoint Overrides

```bash
export CUA_SKILL_ACCESS_HUB_BASE_URL=http://10.37.98.200/cua-access
export CUA_SKILL_GATEWAY_URL=http://10.37.98.200
```

The bundled defaults live in `config.json`.

## Security Rules

- Do not paste bearer tokens into chat.
- Do not pass bearer tokens as command-line arguments.
- Do not commit bearer tokens, API Keys, SSO secrets, or internal tokens.
- Do not log full request headers.
- Run `auth logout` before handing a machine to another user.
