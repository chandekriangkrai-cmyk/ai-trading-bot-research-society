# fxmoly v8.5.3 — research stats fix

Fixes the research failure caused by `KeyError: 'median_hold_minutes'` in `analyze()`.

`stats()` now always returns `median_hold_minutes` (or `None` when there are no holding-time values), matching the fields consumed by the winning/losing holding-duration analysis.

Also keeps the v8.5.2 Render/preview fixes unchanged.
