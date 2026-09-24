# V36 — 50-Request Optimized AI Research Cycle

- Per-cycle AI request hard cap: 50.
- OpenRouter daily free-model quota errors are fatal and stop immediately; no retry/split amplification.
- Default Moltbook AI batch size: 5 to reduce JSON truncation.
- Compact AI prompt/output: question <= 150 chars; judge_reason <= 45 chars.
- Post content sent to the AI is bounded to 2200 chars per item.
- AI Judge remains three-state: `up` => eligible to post; `down`/`no_vote` => blocked.
- Normal truncation still retries and may split recursively while budget remains.

The 50-request cap is an application-side maximum, not a guarantee that OpenRouter will provide 50 accepted requests. Provider quota remains authoritative.
