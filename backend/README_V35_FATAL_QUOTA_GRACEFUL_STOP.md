# V35 — Fatal OpenRouter Daily Quota + Graceful Stop

## Purpose
Prevent wasted OpenRouter requests after the provider has definitively reported that the free-model daily quota is exhausted.

## Behavior
- `HTTP 429` + `free-models-per-day` / `openrouter_free_tier_daily` / remaining=0 is treated as a fatal daily-quota condition.
- No retry.
- No recursive split.
- No child batch after the quota signal.
- The current cycle stops AI processing gracefully.
- Normal truncation / parse failures still use the existing retry/split recovery path.
- AI Judge remains a single-response self-evaluation with `up`, `down`, or `no_vote`.
- `up` is required for an AI-generated comment to be posted.
- `down` and `no_vote` are never posted.

## Telemetry
The scan response adds:
- `ai_quota_exhausted`
- `ai_stop_reason`
- `ai_error_type`

When the OpenRouter daily free quota is exhausted:
- `ai_quota_exhausted = true`
- `ai_stop_reason = "openrouter_daily_quota"`
- `ai_error_type = "openrouter_daily_quota"`

## Verification
- 67 pytest tests passed.
- Python compileall passed.
- AST parsing passed.
- V35-specific tests cover fatal 429, normal truncation retry, and all three AI Judge states.
