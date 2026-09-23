# V26 — AI Composer + Source Guard

V25 proved that OpenRouter can run, but its output was not actually used for the final public comment: `_merge_analysis()` replaced every AI comment with the deterministic V25 composer and then re-applied the deterministic evidence-gap gate.

V26 changes that flow:

```text
Moltbook posts
   ↓
OpenRouter batch
   ↓
AI decides comment/ignore + claim/evidence/missing-validation anchors + draft comment
   ↓
Deterministic source/domain safety checks
   ├─ safe AI comment → publish candidate
   └─ unsafe AI comment → deterministic V25 fallback
```

The AI cannot introduce unsupported numeric claims, foreign domains, or unsupported source anchors. AI comments remain capped at 500 characters and forbidden template openers are rejected.

This is deliberately a controlled experiment: it changes who writes the final comment without removing the safety firewall.
