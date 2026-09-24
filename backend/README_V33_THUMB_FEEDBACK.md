# V33 — Thumb Feedback Loop

Built from V32 AI-first efficient complete-question baseline.

- V32 AI-first pipeline and 48-request hard budget are preserved.
- No human approval gate is added. Existing guards still decide whether a generated comment is eligible for posting.
- Posted comments can receive `up` or `down` feedback through `/api/moltbook-interactions/feedback`.
- Thumb feedback uses **0 AI requests**.
- Latest positive/negative examples are injected into the existing AI prompt on later cycles. This adds no API call.
- Feedback is stored in `moltbook_comment_feedback`; one label per comment.

## Endpoints
- `POST /api/moltbook-interactions/feedback` body `{ "comment_id": "...", "rating": "up" }` or `down`.
- `GET /api/moltbook-interactions/feedback/summary`

## API budget
`MOLTBOOK_AI_REQUEST_BUDGET` remains unchanged (48 by default). Thumb feedback never increments it.
