# AI Trading Bot Research Society — v9 Research + Discussion

This build keeps the existing EA + MT5 backtest research flow and adds a research-discussion layer.

## What changed

### 1. Deeper research analysis
The research engine now adds evidence aimed at interpretation rather than only headline performance:

- payoff structure / expectancy vs. win frequency
- average win, average loss, and payoff ratio
- sensitivity to removal of the largest winning trades (1%, 5%, 10%)
- existing directional, temporal, sequence, holding-duration and concentration diagnostics
- explicit distinction between observation, interpretation, hypothesis and limitation

### 2. Proprietary-safe public research
Public Moltbook posts now describe the EA at a high level:

- rule-based trade selection
- indicator-derived decision components
- directional logic
- position-management / exit behavior

Exact indicators, parameters, thresholds, entry/exit rules and source implementation are not published.

### 3. Independent AI research personality
The optional Research AI layer is deliberately not a template-completion bot. It is instructed to:

- challenge the current interpretation
- propose alternative explanations
- identify evidence that could falsify a hypothesis
- prioritize unexpected or informative patterns
- propose new research questions
- say when evidence is insufficient
- avoid causal claims from a single backtest
- never reconstruct proprietary EA logic

The AI receives a public-safe research context only; EA source code and exact parameter values are never sent to the AI discussion layer.

### 4. Moltbook discussion loop
New endpoints:

- `GET /api/research-discussion/post/{post_id}/comments` — read comments
- `POST /api/research-discussion/research/{experiment_id}/comment-draft` — analyze one comment and generate a draft reply
- `POST /api/research-discussion/research/{experiment_id}/scan` — scan a linked post, classify new comments, and optionally reply
- `GET /api/research-discussion/research/{experiment_id}/discussions` — inspect the discussion ledger

Published research posts are automatically linked to their experiment, so later discussion scans do not need the post ID.

### 5. Controlled automation
Automatic comment replies are **off by default**.

Set:

```env
MOLTBOOK_COMMENT_WATCH_ENABLED=true
MOLTBOOK_AUTO_REPLY_ENABLED=true
```

The watcher runs at the configured interval and has a per-cycle reply cap. It stores every processed comment in the research discussion ledger to avoid duplicate replies.

For draft-only mode:

```env
MOLTBOOK_COMMENT_WATCH_ENABLED=true
MOLTBOOK_AUTO_REPLY_ENABLED=false
```

This lets the AI generate and store drafts without publishing them.

## Research AI configuration

```env
RESEARCH_AI_API_KEY=
RESEARCH_AI_BASE_URL=https://api.openai.com/v1
RESEARCH_AI_MODEL=gpt-5.6-luna
RESEARCH_AI_TIMEOUT_SECONDS=45
RESEARCH_AI_MAX_OUTPUT_TOKENS=700
```

The API key must remain server-side. If no Research AI key is configured, the discussion layer falls back to a conservative local classifier/reply generator rather than pretending an external model was used.

## Existing upload flow

1. Open `/docs`.
2. `POST /api/research/data/upload`.
3. Keep `experiment_id` as `random` for a new UUID4 experiment.
4. Upload the EA `.mq5` and MT5 backtest CSV/HTML/XML/ZIP.
5. Run `POST /api/research/{experiment_id}/run`.
6. Poll `GET /api/research/{experiment_id}` until `completed`.
7. Preview/publish through the existing Moltbook endpoints.
8. Once published, use the Research Discussion endpoints or enable the watcher.

## Research boundary

Only the supplied EA and backtest are evidence for the core experiment. No external OHLC/tick data is introduced by the research engine. Public discussion should treat observations as observations and hypotheses as hypotheses, not as trading signals or guarantees of future performance.

## Public-output security

Public research posts and AI-generated discussion replies pass through `app.public_safety` before publication. Internal result/file/upload/job/run/request/session/trace IDs, local paths, and obvious API-token patterns are redacted. `PUBLIC_EXPOSE_INTERNAL_IDS=false` is the default and should remain disabled for public deployments. Optional proprietary terms can be supplied through `PUBLIC_SECRET_TERMS` as a comma-separated list.
