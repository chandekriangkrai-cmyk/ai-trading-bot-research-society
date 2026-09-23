# V20 — Evidence-Gap-First Admission

V20 changes admission logic so a concrete evidence gap is the primary signal.

A post can pass even when heuristic relevance/research-value scores are low, provided:
- a deterministic evidence-gap extractor finds a concrete claim,
- existing evidence is present,
- a missing validation boundary is defined,
- novelty is valid,
- domain/provenance/specificity guards still pass.

The heuristic relevance/value scores remain reported for observability, but they no longer veto a concrete evidence-gap candidate.

Added deterministic evidence-gap coverage for:
- compile-rate / vulnerability-repair evaluation
- HazardArena semantic safety
- NSGA-II hydrogen-buffer grid optimization

No generic title-token fallback was restored.
