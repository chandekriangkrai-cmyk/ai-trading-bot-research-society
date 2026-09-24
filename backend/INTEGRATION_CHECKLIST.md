# Integration checklist
- Keep V26 as rollback.
- Add `app/ai_first_v28.py`.
- Route Lead Scan AI analysis through `analyze_batch()`.
- Route AI questions through `choose_final_comment()`.
- Keep existing duplicate guard and Moltbook POST/verification.
- Disable the old V25 deterministic composer as a final-comment fallback.
- Run tests.
- Run one Lead Scan Cycle.
- Inspect: `ai_batches_attempted`, `ai_batches_succeeded`, `ai_used`, `ai_error`, `candidates`, `comments_posted`.
