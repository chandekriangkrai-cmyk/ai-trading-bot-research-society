# fxmoly v8.5.8 — Natural Full-Evidence Research Posts

This build keeps the natural research-agent voice while preserving the accumulated evidence in the public research post.

Changes from v8.5.7:
- Keeps all available research findings instead of limiting the public post to the first 8.
- Keeps the complete evidence object for each finding rather than silently truncating it to a compact subset.
- Uses varied, natural research-note transitions instead of repeating `Reading the result:`.
- Renames the hypothesis section to `Working hypotheses and open questions`.
- Raises configurable public-post ceiling from 12,000 to 24,000 characters.
- The stored research result remains the canonical full record.
- Existing Random Experiment ID and Moltbook verification fixes are retained.

Environment option:
`MOLTBOOK_PUBLIC_MAX_CHARS` can be set to a different ceiling if the target API/feed requires it.
