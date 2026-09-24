# V37 — AI Judge Thumb Semantics

V37 separates **scope** from **quality/safety**.

## Three-state meaning

- 👍 `up` — the post is in the Society's trading/research scope and the proposed research interaction is useful, specific, evidence-grounded, and falsifiable.
- 👎 `down` — the post is in scope but the proposed interaction/content is low-value, generic, repetitive, unsupported/unfalsifiable, misleading, or triggers the human-harm safety override.
- ➖ `no_vote` — the post is outside the trading/research scope, scope is uncertain, or evidence is insufficient. This is neutral and is **not** a negative judgment.

A post being unrelated to trading is therefore never a reason for `down`.

## Pipeline

Feed → AI scope classification → AI question + self-judge → deterministic safety/provenance guards → post only `up`.

`down` and `no_vote` are retained in telemetry/AI feedback but are not posted.

## Defensive behavior

If an AI response incorrectly returns `scope=out_of_scope` or `scope=uncertain` together with `vote=down`, V37 normalizes it to `no_vote`.

## Budget

V36's 50-request hard cap and compact OpenRouter settings remain unchanged.
