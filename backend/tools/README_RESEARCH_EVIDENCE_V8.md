# Research Evidence Builder v8

v8 fixes the evidence architecture after the v7.4 raw-tick coverage audit.

## Evidence hierarchy

1. **PRIMARY** — the complete MT5 EA + backtest sample (currently 475 trades).
2. **SUPPLEMENTARY** — raw-tick / market-quote evidence only where timestamps actually overlap.

The builder deliberately does **not**:

- manufacture tick coverage with an arbitrary timezone offset;
- treat 141 tick-covered trades as representative of all 475 trades;
- substitute SL/TP for a missing MT5 execution price;
- expose proprietary EA indicators, thresholds, parameters, or internal IDs in the evidence package.

## Colab / local usage

After the existing EA + backtest research has produced `result.json`, run:

```bash
python tools/research_evidence_v8.py \
  --primary /path/to/result.json \
  --market /path/to/EURUSD_TRADE_MARKET_EVIDENCE_v7_2.csv \
  --coverage /path/to/EURUSD_TRADE_COVERAGE_STRUCTURE_v7_4.csv
```

Outputs:

- `research_evidence_v8.json` — internal research evidence package.
- `research_evidence_v8_public_safe.json` — sanitized package suitable as input to the public-safety/publication layer.

If market evidence is omitted, v8 still works and keeps the 475-trade backtest as the primary evidence source.
