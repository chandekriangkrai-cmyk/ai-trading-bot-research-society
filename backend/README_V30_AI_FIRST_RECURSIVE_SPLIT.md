# V30 — AI First Recursive Split + Grounded Guard

V30 builds directly on V29.

## Changes

- Keeps the V29 compact AI JSON schema and one retry.
- Reduces the default OpenRouter output budget to 650 tokens; retry is capped at 450.
- After a truncation/parse failure, splits `5 -> 2+2+1`.
- If a 2-post child fails, it splits to `1+1` and retries each post independently.
- Provider errors such as HTTP 429/401 are still not retried immediately.
- Removes the requirement that an AI comment must first match the deterministic evidence-gap extractor.
- AI comments must still pass domain safety and a grounded-anchor check against the actual post.
- A distinctive technical/named anchor plus a question is sufficient; otherwise two shared concrete anchors or one anchor plus a grounded number are required.
- No V25 deterministic comment fallback is restored.

## Expected telemetry

A healthy run should show fewer lost results from `finish_reason=length` and more `comment_source=ai` results. Nested `children` metadata can show recursive splitting down to single-post calls.
