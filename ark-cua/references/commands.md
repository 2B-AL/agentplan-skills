# AgentPlan CUA Skill commands

All commands: `python3 <skill_dir>/scripts/cua.py <command> [options]`.
Global option: `--api-base-url <url>` overrides the gateway URL for one call.

## Authentication and health

```bash
python3 scripts/cua.py auth status
python3 scripts/cua.py auth login [--api-key <key>] [--no-prompt]
python3 scripts/cua.py auth logout
python3 scripts/cua.py ping
python3 scripts/cua.py diagnose
python3 scripts/cua.py self-test
```

Prefer the local terminal prompt or `AP_CUA_AGENTPLAN_API_KEY`. `AGENTPLAN_API_KEY` and `ARK_API_KEY` are compatibility aliases. In a non-interactive agent-run command, `auth login` returns `AUTH_REQUIRED` with `setup_command` instead of blocking for input. Do not print or log the API key. `ping`, `diagnose`, and `self-test` do not create a CUA task.

## Core delegation

```bash
python3 scripts/cua.py delegate --objective "<request>" [--wait-ms 0]
python3 scripts/cua.py watch (--invocation-id <id> | --last) [--wait-ms 20000]
python3 scripts/cua.py answer (--invocation-id <id> | --last) --answer "<reply>"
python3 scripts/cua.py result (--invocation-id <id> | --last) [--timeout 600]
python3 scripts/cua.py cancel (--invocation-id <id> | --last)
```

Call `delegate` once and follow `next.command`. Long wait budgets are split into gateway waits of at most 60 seconds. Use `cancel` only on explicit user request.

## Read-only desktop and model inspection

```bash
python3 scripts/cua.py desktop list
python3 scripts/cua.py model get
```

The Skill intentionally excludes desktop access-ticket generation, reboot/reset, and persistent model modification.

## Tasks and reusable contexts

```bash
python3 scripts/cua.py task run --objective "<request>" \
  [--desktop <id-or-name>] [--title "<title>"] [--wait-ms 0]
python3 scripts/cua.py task continue (--context-id <id> | --last-context) \
  --objective "<next step>" [--wait-ms 0]
python3 scripts/cua.py task status (--task-id <id> | --last)
python3 scripts/cua.py task result (--task-id <id> | --last) [--timeout 600]
python3 scripts/cua.py task answer (--task-id <id> | --last) --answer "<reply>"
python3 scripts/cua.py task cancel (--task-id <id> | --last)

python3 scripts/cua.py context list
python3 scripts/cua.py context create [--title "<title>"] [--desktop <id-or-name>]
python3 scripts/cua.py context add-note (--context-id <id> | --last-context) --text "<background>"
python3 scripts/cua.py context show (--context-id <id> | --last-context)
python3 scripts/cua.py timeline show (--context-id <id> | --last-context)
```

Tasks and invocations share the same identifier space. This Skill does not provide an option to suppress CUA's user questions.

## Artifacts

```bash
python3 scripts/cua.py artifact list (--task-id <id> | --last)
python3 scripts/cua.py artifact save (--artifact-id <id> | --last) \
  [--task-id <id>] [--output <new-path>]
```

`artifact save` never overwrites an existing path and does not create a missing parent directory. Omit `--output` to use a secure temporary file. Downloads are limited to 256 MiB; HTML/interstitial content and missing artifacts are not written.
