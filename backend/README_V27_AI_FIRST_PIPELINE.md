# V27 — AI-first interaction pipeline

V27 makes the LLM response authoritative for final comment wording when it passes source/domain safety. The deterministic evidence-gap composer is fallback-only.

## Important diagnostics
The interaction scan now reports:
- `ai_batches_attempted` — provider calls attempted
- `ai_batches_succeeded` — provider calls that returned parseable valid results
- `ai_valid_results` — valid post analyses returned by the model
- `ai_provider` / `ai_model`
- `ai_batch_meta` — per-batch attempt/success/parse information

This prevents the old false signal where OpenRouter could receive a request but `ai_used=false` because the response parser returned `{}`.

## Comment path
1. Moltbook feed
2. Full post fetch
3. AI batch analysis
4. AI comment source/domain guard
5. AI comment accepted OR deterministic fallback
6. Duplicate guard
7. Optional publish

AI comments are rejected when they begin with the known canned openers (`For ...`, `You report ...`, etc.), contain URLs, fail domain grounding, or lack concrete shared anchors.
