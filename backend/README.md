# AI Trading Bot Research Society — Unified Research Engine

## Input model
The research engine accepts: 

1. `EA.mq5` — strategy source.
2. MT5 trades/deals — required to reconstruct completed trades.
3. OHLC bars — primary market-context source for pre-entry research.
4. Tick data — optional high-resolution supporting source for pre-entry tick context and tick-level MFE/MAE.

Bars and ticks may be supplied as separate files or together with trades/deals inside one ZIP. The upload endpoint classifies CSV/HTML/XML contents by schema; filenames are not the source of truth.

### Recommended first research package
For a 2.5-year M30 study:

```text
EA.mq5
backtest.zip
  ├── deals.csv
  ├── EURUSD_M30.csv
  └── EURUSD_ticks.csv
```

The exact filenames do not matter if the columns are recognizable.

### Core columns
Trades/deals: entry/exit time, position/deal/ticket identifier, entry/exit price, side, profit.

Bars: `Time, Open, High, Low, Close` (volume optional).

Ticks: `Time` plus any usable `Bid, Ask, Last/Price, Volume` fields.

## Research flow

```text
EA + Trades/Deals + OHLC Bars + optional Ticks
              ↓
        Normalize once
              ↓
        Market context
              ↓
     Evidence-gated analysis
              ↓
       Research Result
              ↓
           Moltbook
```

### Anti-look-ahead rules
- Pre-entry bar context uses only bars whose full close time is at or before the entry timestamp.
- Pre-entry tick context uses only ticks strictly before entry.
- Post-entry MFE/MAE is separated from pre-entry evidence.
- The engine reports insufficient evidence instead of inventing findings.

## API

- `POST /api/research/data/upload` — EA + required backtest/deals bundle, optional bars and ticks.
- `POST /api/research/{experiment_id}/run` — run the complete research pipeline once.
- `GET /api/research/{experiment_id}` — inspect the result.
- Moltbook endpoints remain separate from research computation.

### Upload fields
`ea_file` required; `backtest_file` required; `bars_file` optional; `ticks_file` optional.

`backtest_file` itself may be a ZIP containing deals, bars, and ticks.

## Evidence policy
No causality is claimed from observational backtest data. Findings are only published when they pass the minimum-sample/effect-size evidence gate. Otherwise the result is marked as insufficient evidence.
