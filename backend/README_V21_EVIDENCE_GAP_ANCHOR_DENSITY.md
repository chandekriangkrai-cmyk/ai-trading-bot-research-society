# V21 — Evidence-Gap Admission + Anchor Density

V21 keeps V20's evidence-gap-first admission, domain guard, and provenance guard.

Changes:
- A concrete evidence gap is sufficient for admission; the legacy claim-anchor score cannot veto an evidence-rich post.
- Adds deterministic evidence-gap rules for SysML verification, confidence/decision records, entropic transport, HazardArena, compile-rate validity, hydrogen/grid buffering, semantic VLA safety, exploitable-path security validation, reproducible-build verifiability, and tool-call efficiency.
- Adds a final anchor-density guard: the generated public comment must contain at least two concrete anchors traceable to the source post.
- If the source/domain/provenance/anchor guard fails, the post is ignored rather than receiving a generic template comment.
- AI remains optional/off-compatible.
