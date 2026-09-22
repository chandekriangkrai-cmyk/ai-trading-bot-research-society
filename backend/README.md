# fxmoly v8.5.4 — Moltbook verification fix

This build fixes the Moltbook verification flow.

- Submits verification to the canonical `POST /api/v1/verify` endpoint.
- Sends exactly `{"verification_code": "...", "answer": "NN.NN"}`.
- Keeps the local Swagger endpoint `/api/moltbook/post/{post_id}/verify` as a convenience wrapper.
- Improves parsing of obfuscated number words such as `tW/eNnTy T hRrEe`.
- Prioritizes explicit `+`, `*`, and arithmetic words over `/` characters that are often just obfuscation.
- Always formats answers with exactly two decimal places, per Moltbook's current instructions.
- Auto-publish verification remains fail-closed: if the challenge cannot be parsed unambiguously, it does not submit a guess.

Existing research / preview / publish behavior is otherwise unchanged.
