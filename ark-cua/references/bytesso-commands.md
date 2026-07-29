# Commands

All commands print one JSON object.

## Auth

```bash
python3 <skill_dir>/scripts/cua.py auth status
python3 <skill_dir>/scripts/cua.py auth status --offline
python3 <skill_dir>/scripts/cua.py auth login
python3 <skill_dir>/scripts/cua.py auth login --bearer-key-stdin
python3 <skill_dir>/scripts/cua.py auth logout
```

`auth login` prints and opens one Access Hub ByteSSO browser login URL, waits
for the browser callback, and stores the returned local CUA credential. Agents
should run this command themselves when auth is required; users should only open
the printed browser URL. `auth status` validates the cached credential online
with `cua_get_desktop_access` unless `--offline` is set.

## Connectivity

```bash
python3 <skill_dir>/scripts/cua.py ping
```

`ping` is read-only. It validates gateway connectivity, auth, and desktop binding.

## Desktops

```bash
python3 <skill_dir>/scripts/cua.py desktops list
python3 <skill_dir>/scripts/cua.py desktops allocate
python3 <skill_dir>/scripts/cua.py desktops allocate --label "qa-run"
python3 <skill_dir>/scripts/cua.py desktops allocate --spec-code s80 --label "qa-run"
python3 <skill_dir>/scripts/cua.py desktops use <desktop_id>
python3 <skill_dir>/scripts/cua.py desktops reboot <desktop_id>
python3 <skill_dir>/scripts/cua.py desktops operation <operation_id>
```

`desktops list` returns the caller's allocated desktops and quota. `desktops
allocate` requests one additional CUA desktop and is rejected if the caller is
over quota. `desktops use` stores a local default desktop for later `observe`
and `delegate` calls.

`desktops reboot` does not ask for confirmation. It waits up to 10 minutes by
default for the VM and required guest components to become ready. Override the
total wait budget with `--wait-ms`. A successful command means it is safe to
submit a new task. If the operation remains `running`, use the structured
error's `next.command` to run `desktops operation`; do not submit a task until
the operation succeeds. A failed operation must be reported instead of retried
as a task.

## Tasks

```bash
python3 <skill_dir>/scripts/cua.py tasks list
python3 <skill_dir>/scripts/cua.py tasks list --status all --limit 50
python3 <skill_dir>/scripts/cua.py tasks watch --task-id <id> --task-id <id>
python3 <skill_dir>/scripts/cua.py tasks watch --task-id <id> --wait-ms 60000
python3 <skill_dir>/scripts/cua.py tasks watch --last
```

`tasks list` returns delegated CUA tasks for the current credential. The default
filter is `active`, which is the right view for concurrent QA work. Use
`--status all` to recover recent terminal tasks.

`tasks watch` refreshes or waits on several task ids. `--wait-ms` is the total
client-side wait budget. The CLI splits budgets above 60 seconds into several
gateway calls because each server wait is capped at 60 seconds. It
returns `data.tasks`, where each item uses the same invocation envelope shape as
`watch`. Use this instead of repeatedly blocking on one task when multiple
desktops are running work.

## Parallel Tasks

Use several `delegate` calls only for independent subtasks. Track their task ids
with `tasks watch`:

```bash
python3 <skill_dir>/scripts/cua.py desktops list
python3 <skill_dir>/scripts/cua.py delegate --auto --objective "<subtask A>"
python3 <skill_dir>/scripts/cua.py delegate --auto --objective "<subtask B>"
python3 <skill_dir>/scripts/cua.py tasks watch --task-id <idA> --task-id <idB> --wait-ms 60000
```

Each subtask must be self-contained. Use completed `result.text` and artifacts
as the source for the final response.

## Delegate

```bash
python3 <skill_dir>/scripts/cua.py delegate --objective "<user objective>"
python3 <skill_dir>/scripts/cua.py delegate --desktop-id <desktop_id> --objective "<user objective>"
python3 <skill_dir>/scripts/cua.py delegate --auto --objective "<user objective>"
python3 <skill_dir>/scripts/cua.py delegate --objective "<user objective>" --wait-ms 30000
python3 <skill_dir>/scripts/cua.py delegate --desktop-id <desktop_id> --session-id <session_id> --objective "<follow-up objective>"
```

Pass the user's original objective directly. By default `delegate` starts the
task in a new my-cua session and returns quickly with an invocation id and
`data.session_id`. Omit `--session-id` to retain this behavior.

Pass a previously returned `data.session_id` when the new objective is a
follow-up that should continue in the existing my-cua conversation/task
context. This creates a new task and run inside that session; it does not resume
the previous run. In a multi-desktop environment, also pass the original
`--desktop-id` because sessions belong to the desktop where they were created.
Do not use an invocation/task id as a session id. Do not combine `--auto` with
`--session-id`.

`--wait-ms` is a total client-side wait budget; the CLI polls in server-sized
chunks of at most 60 seconds and does not cancel the task when the budget
expires. `--auto` selects an idle desktop, or allocates a new one when quota
allows.

## Watch

```bash
python3 <skill_dir>/scripts/cua.py watch --invocation-id <id>
python3 <skill_dir>/scripts/cua.py watch --last
python3 <skill_dir>/scripts/cua.py watch --wait-ms 60000
```

If `--invocation-id` is omitted, the script uses the last invocation id saved by
`delegate`, `watch`, or `answer`. For example, `--wait-ms 900000` waits for up
to 15 minutes through multiple gateway calls while preserving the gateway's
60-second per-request maximum.

## Answer

```bash
python3 <skill_dir>/scripts/cua.py answer --invocation-id <id> --answer "<user answer>"
python3 <skill_dir>/scripts/cua.py answer --answer "<user answer>"
```

Use only after CUA returns `needs_input`. Preserve the user's answer.

## Cancel

```bash
python3 <skill_dir>/scripts/cua.py cancel --invocation-id <id>
python3 <skill_dir>/scripts/cua.py cancel
```

Use only when the user explicitly asks to stop.

## Observe

```bash
python3 <skill_dir>/scripts/cua.py observe
python3 <skill_dir>/scripts/cua.py observe --desktop-id <desktop_id>
python3 <skill_dir>/scripts/cua.py observe --last
python3 <skill_dir>/scripts/cua.py observe --invocation-id <id>
python3 <skill_dir>/scripts/cua.py observe --include-screenshot
```

`observe` is read-only. It returns the current environment state and a temporary
desktop access URL. With `--include-screenshot`, the script saves the image to a
temporary file and returns `screenshot_file`.

Do not ask the user to run `observe`. Run it after auth is ready when the user
asks for the CUA/cloud desktop link. Do not use `observe` to decide whether a
task is done. Use `watch`.

## Credentials

```bash
python3 <skill_dir>/scripts/cua.py credentials status --desktop-id <desktop_id>
python3 <skill_dir>/scripts/cua.py credentials setup --desktop-id <desktop_id>
python3 <skill_dir>/scripts/cua.py credentials sync browser --desktop-id <desktop_id> github volcengine
python3 <skill_dir>/scripts/cua.py credentials sync env --desktop-id <desktop_id> OPENAI_API_KEY
python3 <skill_dir>/scripts/cua.py credentials sync secret --desktop-id <desktop_id> service/api-key
python3 <skill_dir>/scripts/cua.py credentials sync credential-set --desktop-id <desktop_id> --type volcengine_aksk --name volcengine-default
python3 <skill_dir>/scripts/cua.py credentials sync file --desktop-id <desktop_id> --profile codex-auth
python3 <skill_dir>/scripts/cua.py credentials reset --desktop-id <desktop_id> [--device-id <exact-id>]
```

These commands first fail fast against the live Gateway Credential manifest and
the caller-owned target, then resolve the independently published
`al-credential-sync` runtime and use this Skill's production-only
`access_hub_gateway` Target Adapter. Each `credentials sync` command also
idempotently prepares the source Agent itself; browser sync additionally
prepares the local browser. `credentials setup` remains an optional prewarm
that pairs the selected CUA and waits for its unpacked extension heartbeat.
Browser sync accepts exact site policy IDs only. Env, Secret, credential-set,
and file commands retain the independent Agent's hidden-input and policy
checks; this Skill never reads their values.

The production package pins the official
`https://github.com/2B-AL/credential-skill` repository and a full 40-character
commit under `config.json` → `credential_skill.repository/commit`. A controlled
Agent runtime may select another already-reviewed commit from the same fixed
repository with `CUA_CREDENTIAL_SKILL_COMMIT`; arbitrary repositories, branch
names, tags, prompt arguments, and redirects outside GitHub codeload are
rejected. The archive is safely extracted into a commit-versioned private cache
and its required files plus `cua-target/v1` contract are validated before use.
Normal sync reuses a compatible installed runtime and does not contact GitHub.

`credentials reset` first asks the signed-in personal Agent to centrally revoke
the exact Device ID. It invokes target cleanup only after central revocation
succeeds and accepts completion only at `pair_ready=true`.

The `credential-target` subtree is an internal Adapter surface. Do not invoke
it as a replacement for the public `credentials` workflow.

## Self-Test

```bash
python3 <skill_dir>/scripts/cua.py self-test
python3 <skill_dir>/scripts/cua.py self-test --online
```

Plain `self-test` checks local files and config only. `--online` checks
`/skill/manifest` and `cua_get_desktop_access`; it requires login and creates no
CUA task.
