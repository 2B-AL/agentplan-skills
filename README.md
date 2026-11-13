# ARK CUA

This repository publishes the **ark-cua** skill for delegating web and desktop
tasks to CUA. The repository is `2B-AL/cua-skill`; the skill directory and name
are both `ark-cua`.

## Install or update

```bash
npx -y skills add 2B-AL/cua-skill --skill ark-cua -g
```

Choose the coding agents to install to. For a non-interactive installation to
Codex and Claude Code:

```bash
npx -y skills add 2B-AL/cua-skill --skill ark-cua --agent codex claude-code -g --copy -y
```

In Codex, invoke the installed skill as `$ark-cua`. If an earlier distribution installed
it as `byted-util-ark-cua`, remove that old skill entry after installing
`ark-cua` so the agent sees one copy. Keep the runtime state directory described
below.

Existing Git clones should use the current repository address:

```bash
git remote set-url origin https://github.com/2B-AL/cua-skill.git
```

## AgentPlan and development gateways

The bundled gateway is AgentPlan. Start with:

```bash
python3 <skill-dir>/scripts/cua.py auth status
```

The CLI reuses a saved login for that gateway or an eligible personal AgentPlan
Max account from `arkcli`. A development gateway uses its own API key or
environment token:

```bash
python3 <skill-dir>/scripts/cua.py --api-base-url "<DEV_GATEWAY_URL>" auth login
```

Enter the credential in the terminal's hidden prompt. The selected gateway is
saved with the login and used by later commands. A saved credential is sent
only to its gateway; `arkcli` credentials are used only for the bundled
AgentPlan gateway. See [authentication](ark-cua/references/auth.md) for account
switching and gateway selection.

The rename preserves `~/.openclaw/ark-cua/`, including `auth.json` and
`session.json`. Both `AP_CUA_SKILL_*` and `CUA_SKILL_*` overrides remain
supported for the API base URL, auth file and session file. One login is saved
per auth file; logging in to another gateway replaces that login.

Available operations depend on the selected gateway's capabilities and
authorization. Non-browser Credential Agent resource synchronization retains
its existing `prod` environment and `agentplan_device` contract; changing the
CUA gateway does not select a different Credential Agent environment. See
[commands](ark-cua/references/commands.md) and
[credential resources](ark-cua/references/credentials.md).

## Local checks

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ark-cua/tests
node --test ark-cua/tests/cookiecloud-extension.test.mjs
```
