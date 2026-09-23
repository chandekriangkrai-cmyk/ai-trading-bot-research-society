# V25 — Natural Evidence Composer

V25 keeps V24 evidence-gap admission and provenance/domain guards, but changes the public comment composer.

## Goals
- Remove mail-merge phrasing such as `The post reports ... The key open question ... What result would distinguish ...`.
- Never generate the legacy `For ...` opener.
- Preserve explicit research questions from the source when available.
- Lead with concrete evidence actually present in the post.
- Avoid repeating extractor labels and malformed fragments such as `The post reports The ...` or `You report The authors ...`.
- Keep the V24 provenance/domain firewall.

## Composer behavior
1. Extract evidence gap.
2. Clean extractor prefixes from evidence/mechanism text.
3. If the author already asks a concrete question, keep that question as the follow-up.
4. Otherwise generate one short testable follow-up using the extracted mechanism/evidence.
5. Run the final comment through forbidden-opener, anchor, provenance and domain guards.

This version does not add new domain templates.
