# FXMoly EA Backtest Research — v8.5.9

This release builds on v8.5.8.

## Changes
- Natural-language public research formatting while retaining full available findings/evidence.
- Every newly generated research result receives a stable `research_run_id`.
- Added `POST /api/research/{experiment_id}/run-again` to generate a fresh result from the same persisted EA/backtest inputs without overwriting the previous DB result.
- Public preview/publish includes the research run identifier.
- Added local exact-content duplicate publish protection using a persistent publish-history artifact under the experiment folder.
- Duplicate guard blocks republishing the same research content even when a timestamp suffix would otherwise make the payload technically different.
- Random/auto/new experiment IDs remain supported by the upload endpoint.
- Moltbook verification fixes from v8.5.4+ remain included: canonical `/api/v1/verify`, two-decimal answers, robust obfuscated number parsing, and fail-closed behavior.

## Recommended workflow
1. Upload with `experiment_id=random` (or leave the default).
2. Run research.
3. Preview and inspect the result.
4. Publish once.
5. If a genuinely fresh analysis is needed from the same inputs, use `/api/research/{experiment_id}/run-again`; the new result gets a new `research_run_id`.
6. If the public content is materially unchanged, create a new experiment with fresh evidence instead of repeatedly republishing.

The research result remains the canonical evidence record; the public Moltbook post is a readable research report.

## AI↔AI Research Interaction

The service now includes an isolated Moltbook interaction subsystem. It is deliberately disabled by default.

Flow:

`feed discovery → full-post fetch → relevance/novelty/research-value gate → draft → human approval → comment → future reply handling`

Phase 1 (recommended):
- `MOLTBOOK_AI_INTERACTION_ENABLED=true`
- `MOLTBOOK_AI_INTERACTION_AUTO_COMMENT_ENABLED=false`
- Run `POST /api/moltbook-interactions/scan` or `/cycle`.
- Review drafts with `GET /api/moltbook-interactions/leads`.
- Approve a lead, then publish it explicitly.

Phase 3 can enable automatic comments, but it remains budgeted and gated by relevance/novelty/research-value thresholds.
The system does not auto-comment on casual, promotional, political, or unsupported content, and it never exposes proprietary EA implementation details.

Moltbook feed cards can be truncated on some listing routes; candidate posts are therefore re-fetched through `/api/v1/posts/{id}` before analysis. This is based on observed Moltbook behavior reported by agents and is not treated as a universal guarantee.
