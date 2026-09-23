# V23 — Natural Research Questions

## Problems observed in V22 / seven-cycle run

1. Public comments repeatedly began with the same `For ...` construction.
2. The same sentence skeleton was reused across unrelated domains.
3. Generic evidence-gap extraction could combine an evidence sentence with an unrelated limitation elsewhere in the post.
4. A hand-written pattern could fire on a weak/shared keyword and produce a domain-mismatched question (for example an acoustic-verification question on a monocular-vision post).
5. The old provenance guard accepted one generic claim token such as `robot` or `model`, which was not strong enough as semantic proof.
6. AI availability was inconsistent across cycles; deterministic behavior therefore remains the safety baseline.
7. One public comment per cycle was preserved as an anti-spam control.

## V23 changes

- Removed the fixed `For [topic], what evidence would directly test...` public-comment template.
- Added four deterministic natural question shapes.
- Added a forbidden-opener / forbidden-template regression guard.
- Reworked the generic evidence-gap detector to bind evidence and limitation sentences by proximity and lexical linkage instead of selecting unrelated first matches.
- Tightened the acoustic-specific trigger so factory/accuracy language alone cannot create an acoustic comment.
- Strengthened provenance so multi-token normalized claims need multiple source anchors.
- Kept deterministic evidence-gap admission, domain/provenance checks, duplicate guard, and max one public comment per cycle.
- No AI provider is required for the new behavior.

## Operational intent

V23 is not a random comment generator. It should ask one source-grounded research question that naturally follows from the author's own evidence and stated uncertainty.

A post without a defensible evidence gap remains ignored.
