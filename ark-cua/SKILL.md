---
name: ark-cua
description: Delegate broad computer-use tasks to ARK CUA for Volcengine AgentPlan users through an authenticated cloud desktop. Use for web browsing, authenticated website or desktop-app operation, file handling, multi-step GUI workflows, reusable task contexts, artifact download, task status, temporary CUA App login URLs, or read-only desktop and model inspection. Do not use when local reasoning or a purpose-built local/API tool can complete the request more directly.
---

# ARK CUA

Operate ARK CUA through the bundled Python CLI. Keep all gateway access inside the CLI and never request an AgentPlan API key in chat.

## Command surface

```bash
python3 <skill-dir>/scripts/cua.py <command> [options]
```

Parse the single JSON object printed by each invocation:

- Success: `ok: true`, with `data` and sometimes `next`.
- Failure: `ok: false`, with `error.code` and sometimes `next.setup_command`.

Read [references/commands.md](references/commands.md) for non-core commands. Read [references/auth.md](references/auth.md), [references/outcomes.md](references/outcomes.md), or [references/troubleshooting.md](references/troubleshooting.md) only when the corresponding state occurs.

## Core workflow

1. Run `auth status`.
2. On `AUTH_REQUIRED`, `TOKEN_EXPIRED`, or `REFRESH_FAILED`, relay the returned `setup_command`. Ask the user to run it in their own local terminal and enter the AgentPlan API key through the hidden prompt. Never run the interactive login from an agent session and never accept the key in chat.
3. After the user confirms login completed, run `auth status` again.
4. Run `delegate --objective "<user request>"` once. Preserve the user's objective without planning or decomposing it.
5. Record `data.invocation_id`; never submit the same request again.
6. Drive `data.outcome` until terminal:
   - `in_progress`: run `next.command` and continue watching.
   - `needs_input`: relay `data.input_request.question` verbatim, then submit the user's reply with `answer`.
   - `completed`: use `data.result.text` as the authoritative result.
   - `failed`: report the failure; retry only when requested and safe.
   - `cancelled`: report cancellation.

If task creation returns `ACTIVE_RUN_CONFLICT`, the new request did not start. Stop, tell the user the desktop is busy, and do not retry or inspect the existing task unless the user explicitly asks.

## Route special intents

- Specific desktop or reusable context: use `desktop list`, `task run`, `context`, and `task continue`.
- CUA App login URL: run `desktop access` after the requested work finishes and return that command's new `data.full_interface_url`, falling back to `data.access_url`. Never reuse a URL from an earlier result. On `runtime_capability_required`, revoke the failed ticket and run `desktop access` once for a fresh URL; never rewrite the gateway-owned path. If the fresh URL also fails, report a gateway/runtime configuration failure. Use `desktop revoke-access` if a URL may have leaked or is no longer needed.
- Local file delivery: remove only local-delivery wording from the CUA objective, have CUA export a registered artifact, then use `artifact list` and `artifact save`.
- Health or configuration inspection: use `ping`, `diagnose`, or `model get`; do not create a task merely to test availability.
- Stop an active task: use `cancel` only when the user explicitly asks.

## Safety and result integrity

- Use the bundled gateway in `assets/config.json`; allow the same per-call and environment overrides as `ap-cua-skill`.
- Configure credentials exactly like `ap-cua-skill`: resolve `--api-key`, then `AP_CUA_AGENTPLAN_API_KEY`, `AGENTPLAN_API_KEY`, or `ARK_API_KEY`, then use the hidden local-terminal prompt when no key is otherwise available. Validate and store successful credentials in the local `0600` cache.
- Never expose API keys, cache contents, authorization headers, user answers, or artifact bytes.
- Never infer completion from progress text or a nonterminal state.
- Treat `result.text` as authoritative only when `outcome == completed`.
- Refuse to overwrite existing local files. Require a new output path for `artifact save`.
- Reject HTML/interstitial responses as artifacts and do not write them to disk.
- Do not accept base64 text or an external share link as a downloaded file; require a registered artifact.
- Treat temporary desktop URLs and their tickets as secrets. Return a URL only when the user requests access, never log it, and revoke it when exposure is suspected.
- Do not bypass CUA questions, modify persistent model settings, manage schedules, or invoke desktop reboot/reset operations.
