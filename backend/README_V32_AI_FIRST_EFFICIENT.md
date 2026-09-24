# V32 — AI First Efficient Complete Question

Built from V31 hard-budget-48.

Goals:
- Preserve AI-first comment quality and the 48-request hard ceiling.
- Reduce request amplification caused by recursive 2+2+1 splitting.
- Use balanced 3+2 splitting first, then recurse only for failed children.
- Reject incomplete/truncated questions instead of posting them.
- Keep source/domain grounding and no deterministic V25 fallback.

Telemetry adds per-batch `split_depth`.

No Render environment-variable change is required.
