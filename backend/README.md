# AI Trading Bot Research Society — V42.4

Clean production architecture for the current EA/backtest research engine and Moltbook AI research interaction system.

## Architecture

```text
backend/
└── app/
    ├── main.py                  # FastAPI composition + startup recovery
    ├── config.py                # Environment settings
    ├── database.py              # SQLAlchemy engine/session/base
    ├── models.py                # Core agent models
    ├── research_models.py       # Research + Moltbook interaction models
    ├── schemas.py               # API schemas
    ├── ea_files.py              # EA source storage model
    ├── unified_research.py      # EA + MT5 backtest research engine
    ├── moltbook.py              # Moltbook API integration
    ├── moltbook_interaction.py  # AI-first feed analysis + AI Judge + posting
    ├── research_discussion.py   # Research discussion replies
    ├── discussion_watcher.py    # Optional background watcher
    ├── public_safety.py         # Public-output sanitization
    └── api/
        ├── health.py
        ├── agents.py
        ├── ea_files.py
        ├── interactions.py
        ├── discussion.py
        └── moltbook.py
```

## V42.4 interaction pipeline

```text
Moltbook global feed
        ↓
Fetch full posts
        ↓
AI batch (hard cap 2 posts)
        ↓
AI self-evaluation
  ├─ out_of_scope → NO_VOTE
  ├─ uncertain    → NO_VOTE
  ├─ in_scope + useful/falsifiable → UP
  ├─ in_scope + low-value/unsupported → DOWN
  └─ human-harm safety override → DOWN
        ↓
Only UP candidates can become public comments
        ↓
Duplicate/source/domain/safety guards
        ↓
Maximum 1 public comment per cycle
```

## AI request efficiency

- Hard cycle budget: **50 requests**.
- Batch size is capped at **2**, even if an environment variable is accidentally higher.
- One retry for a truncated/parse-failed batch.
- One balanced split only after a failed retry.
- Split children cannot recursively split again.
- Retry output is capped at **220 tokens**.
- Normal output is capped at **360 tokens**.
- Input content is bounded to reduce context pressure.
- Truncated responses are parsed for complete JSON rows before another request is spent.
- OpenRouter daily free-tier quota errors stop immediately; retries cannot recover a provider-side daily quota.

## AI Judge semantics

Scope and quality are separate.

**Out of scope does not mean DOWN.** It is neutral and becomes `NO_VOTE`.

`DOWN` is reserved for an in-scope low-value/unsupported/misleading candidate or a safety override involving credible human harm/threats.

## Public research safety

The system never intentionally exposes proprietary EA implementation details, exact thresholds, credentials, internal identifiers, local paths, or private data in public research comments.

## Deployment

Render uses `backend/render.yaml` and `backend/Dockerfile`. The production container copies only `app/`, so the repository root no longer contains duplicate runtime modules.

## Verification

The V42.4 source is verified with:

- pytest
- Python compilation
- AST parsing
- ZIP integrity check

The repository intentionally retains only the current production modules and focused tests; superseded V20–V38 README/test copies and duplicate runtime modules are removed.

## V42.4 hard budget contract

- `MOLTBOOK_AI_REQUEST_BUDGET` controls the per-cycle AI request budget.
- The application clamps this value to **1–50**; configuration can lower the budget but cannot raise it above 50.
- `ai_requests_used` and `ai_requests_remaining` are returned by the scan endpoint.
- `ai_retry_batches` and `ai_retry_successes` expose retry behavior for operational telemetry.
- `MOLTBOOK_AI_BATCH_SIZE` is clamped to **1–2**.
- The application version is read from `backend/VERSION` so the FastAPI root and health endpoint stay aligned with the release version.
