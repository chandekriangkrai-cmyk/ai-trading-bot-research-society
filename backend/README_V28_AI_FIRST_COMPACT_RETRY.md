# V28 — AI First Compact Retry

V28 keeps the V27 AI-first pipeline but changes the interaction path to reduce truncated OpenRouter responses and remove the old deterministic comment revival.

## Changes

- AI batch output is now compact: `post_id`, `decision`, and a short `question` only.
- Comment/question is limited to 220 characters by the AI prompt.
- Default AI output budget is 900 tokens; retry budget is 600.
- A batch is retried once only for output truncation / JSON parse failures.
- Provider errors such as HTTP 429 are **not** retried immediately.
- Accepted AI questions are stored with `comment_source=ai`.
- If an AI-selected comment fails the source/domain guard, V28 ignores it rather than reviving the old deterministic V25 fallback.
- Existing Moltbook feed, duplicate guard, one-comment-per-cycle policy, database persistence, and public-safety sanitization remain intact.

## Expected telemetry

A healthy cycle should show:

- `ai_provider: openrouter`
- `ai_model: openrouter/free` (or the configured model)
- `ai_batches_succeeded` close to `ai_batches_attempted`
- `ai_error: null` when all batches succeed
- `comment_source: ai` for AI-authored comments
- no `You report ...` / `For ...` deterministic fallback comments

`retry_used: true` in an individual `ai_batch_meta` entry means the first response was truncated/invalid and the compact retry succeeded.
