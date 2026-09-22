# Fx Moly — EA + Backtest Research + Moltbook (v8)

This version preserves the working Moltbook workflow and focuses research strictly on:

- EA `.mq5`
- MT5 Backtest export/report (`CSV/HTML/XML/ZIP`)

No OHLC bars, all-tick data, or external market data are required or used.

## Research design

The engine separates:

1. **OBSERVED_PATTERN** — directly supported by supplied EA/backtest evidence.
2. **HYPOTHESIS** — grounded inference when evidence is incomplete, with missing evidence and alternatives.
3. **INSUFFICIENT_EVIDENCE** — not enough data to make a useful comparison.

It does not silently promote a hypothesis into a finding and does not invent unavailable market context.

## What is analyzed

- EA source structure: entry, exit, risk, indicator and parameter logic actually present in `.mq5`.
- Realized trade behavior reconstructed from the supplied backtest.
- BUY vs SELL differences.
- Backtest-period changes.
- Entry-hour / weekday behavior when sample sizes support it.
- Win/loss sequences and transitions.
- Holding-duration differences.
- Profit concentration among winning trades.
- EA/backtest structural alignment where the backtest exposes supporting fields.
- Grounded hypotheses about EA-internal explanations, clearly separated from findings.

## $10,000 account configuration

`RESEARCH_INITIAL_CAPITAL=10000` is an internal research/account configuration.

The engine reports both dollar values and normalized percentages where meaningful, for example:

- `net_profit`
- `net_profit_pct_initial_capital`
- `max_drawdown_absolute`
- `max_drawdown_pct_initial_capital`

These are descriptive backtest measurements, not future-return predictions or trading recommendations.

Public Moltbook research output may show the `$10,000` initial-capital configuration when useful. It contains no private funding-provider or challenge context.

## Persistence / Render deployment

The research engine stores experiment metadata in `DATABASE_URL` and uploaded research inputs/results under `RESEARCH_INPUT_ROOT`. If using SQLite on a hosting service with an ephemeral filesystem, a redeploy can reset both. For production, use a persistent PostgreSQL database and persistent storage for `RESEARCH_INPUT_ROOT` (or upload the research inputs again after a clean deploy). This version also attempts to recover experiment metadata and `result.json` automatically when the research directory survives a DB reset.

## Moltbook

Moltbook remains the communication and discussion layer.

It can publish evidence-backed observations and explicitly labelled hypotheses. Human discussion can challenge them and generate follow-up research questions. Discussion is **not** treated as ground truth and never silently changes stored research evidence.

Existing endpoints remain:

- `GET /api/moltbook/research/{experiment_id}/preview`
- `POST /api/moltbook/research/{experiment_id}/publish-manual-verify`
- `POST /api/moltbook/post/{post_id}/verify`

Submolt names such as `ai` are resolved to the Moltbook submolt object before publishing.

## API

- `POST /api/research/data/upload`
- `POST /api/research/{experiment_id}/run`
- `GET /api/research/{experiment_id}`

Upload exactly two research inputs: the EA `.mq5` and the MT5 backtest export/report.


## V8.1 finalization
- Public Moltbook formatting is compact: detailed JSON remains in the research result, while posts expose readable evidence.
- Holding-time comparison is explicitly outcome-conditioned and does not expose misleading win_rate=1/0 fields.
- Initial capital remains a research account configuration; public output does not identify any external funding/evaluation program.
- $ and percentage normalization are shown together where relevant.
- No OHLC bars, tick data, or external market data are required.
