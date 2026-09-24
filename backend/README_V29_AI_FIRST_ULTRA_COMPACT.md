# V29 — AI First Ultra-Compact Split Retry

V29 builds on V28 and targets the remaining OpenRouter `finish_reason=length` failures.

## Changes
- Uses a shorter response schema: `{"r":[{"p":"POST_ID","d":"i|c","q":"..."}]}`.
- Normalizes the compact schema internally to the existing `post_id/decision/question` representation.
- Keeps one retry for truncation/JSON parse failures.
- If a batch still fails after retry, splits the batch once into two smaller child batches.
- Split recursion is bounded (`allow_split=False` for children) to avoid runaway API calls.
- Provider errors such as HTTP 429/401 are still not retried immediately.
- No deterministic V25 fallback is restored.
- AI comments remain subject to the existing source/domain guard.

## Expected telemetry
Look for `split_used=true` in `ai_batch_meta` when a parent batch had to be divided.
A healthy run should have more successful batches and fewer `finish_reason=length` errors.
