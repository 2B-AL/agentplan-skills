# Outcomes

`delegate`, `watch`, and `answer` return an invocation envelope under `data`.
When available, `data.session_id` identifies the my-cua session that owns the
task context. Pass it to a later `delegate --session-id` call only when the new
task should continue in that context; omit it for a new session.
`tasks watch` returns the same envelope shape for each entry under `data.tasks`.

```json
{
  "invocation_id": "cua_inv_...",
  "outcome": "in_progress | needs_input | completed | failed | cancelled",
  "result": {
    "text": null,
    "artifacts": [
      {
        "id": "art_...",
        "type": "text | image | file | browser_snapshot",
        "kind": "screenshot",
        "mime_type": "image/png",
        "name": "result.png",
        "url": "/api/artifacts/art_...",
        "path": "C:/Users/user/Desktop/result.md",
        "text": "small text content when available"
      }
    ],
    "error": {
      "code": "RATE_LIMITED",
      "message": "model provider rate limited the request",
      "source": "model_provider",
      "stage": "model_execute",
      "accepted": true,
      "retryable": true,
      "upstream_status": 429,
      "upstream_code": "ModelAccountTpmRateLimitExceeded",
      "retry_after_ms": 3000
    }
  },
  "input_request": { "question": "...", "choices": [] },
  "progress": { "summary": "...", "step_count": 2, "updated_at": "..." },
  "next_action": { "type": "...", "agent_hint": "..." },
  "diagnostics": { "trace_id": null }
}
```

## How To Handle Outcomes

| outcome | what to do |
| --- | --- |
| `in_progress` | Keep the invocation id and use `watch`, `watch --last`, or `tasks watch` later. Do not answer from progress text. |
| `needs_input` | Relay `input_request.question` to the user, then run `answer`. |
| `completed` | Use `result.text` as the authoritative final answer. Mention artifacts if relevant. |
| `failed` | Report the failure and diagnostics. Retry only if the user asks. |
| `cancelled` | Tell the user it was cancelled. |

## Rules

- `result.text` is authoritative only when `outcome == completed`.
- `result.error` is non-null only when `outcome == failed`. Branch on its
  stable `code`; use `upstream_code` only for diagnostics.
- `result.artifacts` is a normalized artifact list. Use `type` to decide how to
  present it:
  - `text`: may include inline `text` when the upstream result provides small
    text content; otherwise use `url` or `path`.
  - `image`: present `url` or `path`, plus `mime_type`, `width`, and `height`
    when available. Do not expect inline base64.
  - `file`: present `name`, `mime_type`, `size_bytes`, `url`, or `path`.
  - `browser_snapshot`: use as diagnostic/page evidence, not as the final
    answer.
- A timeout is not an outcome. If CUA is still running, run `watch` or
  `tasks watch` again.
- Progress summaries and screenshots are not final answers.
- Use `observe` for desktop visibility only; use `watch` or `tasks watch` for
  task state.
