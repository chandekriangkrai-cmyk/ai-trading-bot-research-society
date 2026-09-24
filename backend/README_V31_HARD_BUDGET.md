# V31 — AI First Hard Budget 48 + Recursive Split

V31 is based directly on V30.

## Changes

- Hard cap: `MOLTBOOK_AI_REQUEST_BUDGET=48` AI/OpenRouter requests per Lead Scan Cycle.
- Counts every actual provider call, including retries and recursive split children.
- When the budget is exhausted, no further provider call is attempted.
- Recursive split remains available for truncation/parse failures while budget remains.
- HTTP 429/rate-limit errors are not retried and do not trigger recursive splitting.
- Cycle telemetry exposes:
  - `ai_request_budget`
  - `ai_requests_used`
  - `ai_requests_remaining`
  - `ai_budget_exhausted`
- Existing V30 AI grounding/domain guard and AI-first comment behavior are preserved.

## Default

```text
MOLTBOOK_AI_REQUEST_BUDGET=48
```

This is a per-cycle application-level cap. It does not increase or reset the provider's own daily quota.
