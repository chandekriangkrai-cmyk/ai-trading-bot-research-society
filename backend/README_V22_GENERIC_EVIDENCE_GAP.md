# V22 — Generic Evidence-Gap Detector

V22 builds on V21 and changes the evidence-gap extractor itself rather than adding another domain/template list.

## Admission structure

A post can pass when its own text contains:

1. quantitative or observed evidence (preferably multiple numeric anchors),
2. an explicit limitation, uncertainty, boundary, or unresolved condition,
3. enough surrounding context to formulate a validation question.

The detector then constructs:

`POST -> EVIDENCE -> LIMITATION/BOUNDARY -> MISSING VALIDATION -> QUESTION`

The existing V21 domain, provenance, and anchor-density guards remain in place.

## Why

V21 still missed research-rich posts outside its hand-written rule set, including posts such as:

- 14C yield-function reconstruction with measured production values and an explicit cosmic-ray-spectrum bias limitation.
- Disk-gap / candidate-planet analysis with numerical alignment probability, orbital movement, and the explicit absence of a complete orbital solution.

V22 does not create dedicated templates for those domains. It detects the evidence/limitation structure generically.

## Safety / fail-closed behavior

A single number or vague research wording is not enough. The generic detector requires both an evidence sentence and a boundary/limitation signal plus at least two numeric anchors. The final public comment still has to pass V21 provenance, domain, and anchor-density checks.

## AI behavior

No AI is required. If AI is enabled later, deterministic evidence-gap validation remains the final gate for public comments.
