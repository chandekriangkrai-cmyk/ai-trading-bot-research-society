# Fx Moly — Market Regime Research Engine

This version deliberately removes analysis that does not answer the research question.

Core research:
EA + Backtest + M30 Bars
→ reconstruct realized trades
→ build pre-entry M30 market context
→ classify market regime
→ compare win/loss behavior by regime and time period
→ detect market-condition shifts
→ measure EA sensitivity to those shifts
→ evidence gate
→ research findings

Not included:
- Tick analysis
- synthetic/fabricated market data
- look-ahead indicators
- MFE/MAE used as pre-entry evidence
- automatic causal claims
- filler metrics/pipeline labels

Statuses:
OBSERVED_PATTERN
VALIDATED_PATTERN
INSUFFICIENT_EVIDENCE
NOT_AVAILABLE_FROM_INPUT_DATA

API:
POST /api/research/data/upload
GET  /api/research/{experiment_id}
POST /api/research/{experiment_id}/run
