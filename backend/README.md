# Fx Moly — Unified EA + Backtest Research + Moltbook

This build keeps the existing Moltbook workflow and narrows research inputs to exactly what the supplied files contain:

- EA `.mq5`
- MT5 Backtest export/report

No M30 bars, all ticks, or external market data are required or used.

## Research philosophy

The engine separates:

1. **OBSERVED_PATTERN** — directly supported by the supplied EA/backtest evidence.
2. **HYPOTHESIS** — a reasoned inference when evidence is incomplete but there is a defensible basis in the EA/backtest. It includes missing evidence and alternative explanations.
3. **INSUFFICIENT_EVIDENCE** — the available data is too thin even for a useful comparison.

A hypothesis is never silently promoted to a finding.

If a question requires information absent from the inputs, the engine does not fabricate it. It may create a clearly labelled hypothesis only when the supplied EA/backtest provide a rational basis for that inference. Otherwise the question is omitted from the result.

## What is intentionally studied

- EA source structure: entry/exit/risk/indicator logic that is actually present in `.mq5`.
- Realized trade behavior in the backtest.
- BUY vs SELL differences.
- Time-period changes.
- Entry hour and weekday patterns when sample sizes support them.
- Win/loss sequences and transitions.
- Holding duration differences between wins and losses.
- Profit concentration / tail dependence.
- Observable alignment between EA-declared behavior and backtest fields.
- Evidence-backed hypotheses about why an observed pattern might exist, with alternatives and missing evidence.

## What is not claimed

- No market-regime or price-action conclusion without price/market data.
- No causal claim from correlation alone.
- No claim that an internal EA branch caused a specific trade unless the backtest exposes that evidence.
- No look-ahead market features.
- No filler sections for unavailable data.

## Moltbook

Moltbook remains the communication/discussion layer. It receives both validated observations and explicitly labelled hypotheses, and can discuss, challenge, and generate research questions. Moltbook discussion is not treated as ground truth.

Existing preview, publish, manual verification, and verification endpoints remain available.
