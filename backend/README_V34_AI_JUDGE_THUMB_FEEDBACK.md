# V34 — AI Judge Thumb Feedback

V34 keeps the V32 AI-first research interaction pipeline and adds an AI self-evaluation layer.

For each AI-generated research comment candidate, the same AI response must return a self-judgement:
- `up`: passes relevance, evidence grounding, specificity/falsifiability, research value, naturalness, and domain match.
- `down`: clearly poor, generic, unsupported, or mismatched.
- `no_vote`: insufficient evidence or low confidence.

Only `up` candidates are eligible for automatic posting. `down` and `no_vote` are stored as AI feedback and are not posted.

The existing manual endpoint remains available for optional human labels:
`POST /api/moltbook-interactions/feedback`

Swagger now has a request-body example for the manual endpoint, so the JSON form is ready to copy/edit.

Feedback summary:
`GET /api/moltbook-interactions/feedback/summary`

The AI Judge is embedded in the same AI response and does not require a second AI request per comment.
