# AI Trading Bot Research Society — Unified Research Engine

## New flow

1. Upload **one EA `.mq5`** and **one MT5 backtest input** (`.csv`, `.html`, `.htm`, `.xml`, or `.zip`).
2. `POST /api/research/{experiment_id}/run` normalizes the supplied evidence once.
3. The engine reconstructs completed trades when possible and uses market OHLC only when it is actually present in the backtest input.
4. Pre-entry context uses only candles available at/before the entry timestamp. No look-ahead.
5. Findings pass an evidence gate. Small samples remain `INSUFFICIENT_EVIDENCE`; no invented patterns.
6. `GET /api/research/{experiment_id}` returns the traceable result.
7. Moltbook is a separate publication layer: preview → publish/manual verification.

## Important data limitation

A normal MT5 HTML performance report may contain trade/deal records but not raw OHLC candles. In that case the engine **does not invent price-action findings**. The result explicitly reports `MARKET_OHLC_NOT_AVAILABLE_FROM_BACKTEST_INPUT`.

For price-action/regime research, the backtest upload must contain usable OHLC data (for example inside the supplied ZIP/CSV bundle) alongside the trade/deal data.

## Public endpoints

- `POST /api/research/data/upload`
  - `experiment_id` optional
  - `symbol` optional
  - `timeframe` optional
  - `ea_file` required `.mq5`
  - `backtest_file` required CSV/HTML/XML/ZIP
- `POST /api/research/{experiment_id}/run`
- `GET /api/research/{experiment_id}`
- `GET /api/moltbook/research/{experiment_id}/preview`
- `POST /api/moltbook/research/{experiment_id}/publish-manual-verify`
- `POST /api/moltbook/post/{post_id}/verify`

## What is intentionally not public anymore

The old v1/v3/v4/v5/v6/v7/v8/v9/v10/v11/v11.2 stage-by-stage pipeline is no longer mounted in `main.py`. Its implementation files are not required by the new public flow.
