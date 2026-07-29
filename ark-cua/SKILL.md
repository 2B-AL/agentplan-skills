---
name: ark-cua
description: Delegate broad computer-use work to an authenticated CUA cloud desktop through either the AgentPlan or ByteSSO deployment. Use for web browsing, application operation, file handling, multi-step desktop workflows, AgentPlan contexts/artifacts/schedules, or ByteSSO multi-desktop/parallel/Credential Agent work. Use AgentPlan by default; select ByteSSO only when the user explicitly requests the ByteSSO scheme.
---

# ARK CUA

Operate CUA only through the bundled CLI:

```bash
python3 <skill_dir>/scripts/cua.py [--auth-scheme agentplan|bytesso] <command> [options]
```

Every call emits one JSON response. Never call either deployment's HTTP API
directly.

## Select the scheme

- If the user explicitly says AgentPlan, pass `--auth-scheme agentplan`.
- If the user explicitly says ByteSSO, pass `--auth-scheme bytesso`.
- If the user does not specify a scheme, omit the flag. The CLI defaults to
  `agentplan`.
- Do not infer a scheme from reachable networks, cached credentials, environment
  variables, old Skill state, or the requested command.
- If a command is unavailable under the selected scheme, report
  `CAPABILITY_UNAVAILABLE`. Do not silently switch schemes.

Inspect or persist an explicit choice with:

```bash
python3 <skill_dir>/scripts/cua.py auth-scheme status
python3 <skill_dir>/scripts/cua.py auth-scheme use bytesso
python3 <skill_dir>/scripts/cua.py auth-scheme reset
```

The new Skill stores state only below `~/.ark-agentplan/ark-cua/` by default.
It never reads or changes either legacy Skill's state.

## Authenticate

Run `auth status` under the selected scheme before real work.

### AgentPlan

If `auth status` returns `AUTH_REQUIRED`, do not run the interactive setup
yourself. Relay `error.setup_command` or `next.setup_command` to the user and
ask them to run it in their own local terminal. Never ask the user to paste an
AgentPlan API Key into chat.

After the user confirms setup, run `auth status` again.

### ByteSSO

If auth is missing or expired, run `auth login` yourself. Show the single
ByteSSO browser login URL returned by the command, wait for the user to complete
login, and allow the command to cache the returned credential. Do not ask the
user to run the command and do not expose bearer credentials.

Read [agentplan-auth.md](references/agentplan-auth.md) or
[bytesso-auth.md](references/bytesso-auth.md) when authentication needs detail.

## Run a task

For ordinary work:

```bash
python3 <skill_dir>/scripts/cua.py [scheme option] delegate --objective "<user request>"
```

- Preserve the user's original objective.
- Record the returned invocation/task id.
- Do not call `delegate` twice for the same request.
- Drive `data.outcome`:
  - `in_progress`: run `next.command`, `watch`, or the selected scheme's task
    watcher.
  - `needs_input`: relay the question verbatim, then run `answer`.
  - `completed`: use `data.result.text` as the authoritative answer.
  - `failed` or `cancelled`: report the terminal state and safe diagnostics.
- Treat progress and screenshots as status only.
- Cancel only when the user explicitly asks to stop.
- Do not finish a delegated objective with unrelated local browser/search tools.

For AgentPlan, `ACTIVE_RUN_CONFLICT` means the new task did not start. Stop and
tell the user the desktop is busy; do not retry or probe another task unless
the user asks.

For waits longer than 60 seconds, let the CLI split the client budget into
server-sized calls. A timeout does not by itself mean a task failed.

## Use scheme-specific capabilities

Common commands:

```text
auth status|login|logout
ping
delegate
watch
answer
cancel
observe
self-test
desktop(s) list|reboot|operation
```

AgentPlan additionally provides:

```text
result
diagnose
desktop access|revoke-access|reset
model get|set
task run|continue|status|result|answer|cancel
context list|create|add-note|show
timeline show
artifact list|save
schedule create-once|create-recurring|list|status|history|stop|delete
```

ByteSSO additionally provides:

```text
desktops allocate|use
tasks list|watch
delegate --auto
delegate --session-id
credentials status|setup|sync|reset
credential-target ...  # internal; use only for al-credential-sync
```

Use one ByteSSO desktop for dependent work and different idle desktops only for
independent parallel subtasks. Preserve every task id and collect them with
`tasks watch`.

Use AgentPlan `artifact save` for local delivery. Do not put "download to my
local machine" into the cloud-desktop objective; create the artifact first and
then save it locally.

Use AgentPlan schedules for future or recurring work. Read scheduled results
with `schedule history`, not live `watch`.

## Security rules

- Never print, log, or place credentials in chat, argv, objectives, answers, or
  repository files.
- Never send an AgentPlan credential to ByteSSO endpoints or a ByteSSO
  credential to AgentPlan endpoints.
- Treat temporary desktop URLs as secrets. Revoke AgentPlan access tickets when
  they may have leaked or are no longer needed.
- Keep ByteSSO Credential synchronization within the public `credentials`
  workflow. Do not enumerate secret values, copy browser profiles, read cookies,
  broaden exact resource names, or expose pair relay ciphertext.
- Do not auto-migrate credentials from `~/.openclaw`.

## References

Read only the selected scheme's references:

- AgentPlan:
  [commands](references/agentplan-commands.md),
  [outcomes](references/agentplan-outcomes.md),
  [API contract](references/agentplan-api-contract.md),
  [troubleshooting](references/agentplan-troubleshooting.md).
- ByteSSO:
  [commands](references/bytesso-commands.md),
  [outcomes](references/bytesso-outcomes.md),
  [API contract](references/bytesso-api-contract.md),
  [troubleshooting](references/bytesso-troubleshooting.md).
- Implementation provenance and adapter changes:
  [sources](references/sources.md).
