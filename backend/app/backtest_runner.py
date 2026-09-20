from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Trade:
    profit: float
    regime: str = "unknown"


def _to_float(value: str | None) -> float:
    if value is None or value == "":
        raise ValueError("profit is required")
    return float(value)


def parse_trade_csv(content: bytes) -> list[Trade]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames or "profit" not in reader.fieldnames:
        raise ValueError("CSV must contain a 'profit' column.")

    trades: list[Trade] = []

    for row in reader:
        profit = _to_float(row.get("profit"))
        regime = (row.get("regime") or "unknown").strip() or "unknown"
        trades.append(Trade(profit=profit, regime=regime))

    if not trades:
        raise ValueError("CSV contains no trades.")

    return trades


def _metrics(trades: list[Trade]) -> dict:
    profits = [t.profit for t in trades]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]

    gross_profit = sum(wins)
    gross_loss_abs = abs(sum(losses))
    net_profit = sum(profits)

    profit_factor = (
        gross_profit / gross_loss_abs
        if gross_loss_abs > 0
        else None
    )

    expectancy = net_profit / len(profits)

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for profit in profits:
        equity += profit
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)

    return {
        "trade_count": len(profits),
        "net_profit": net_profit,
        "gross_profit": gross_profit,
        "gross_loss": sum(losses),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "win_rate": len(wins) / len(profits) if profits else None,
        "max_drawdown_absolute": max_drawdown,
    }


def analyze_trades(content: bytes) -> dict:
    trades = parse_trade_csv(content)

    by_regime: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        by_regime[trade.regime].append(trade)

    regime_metrics = {
        regime: _metrics(group)
        for regime, group in sorted(by_regime.items())
    }

    return {
        "overall": _metrics(trades),
        "by_regime": regime_metrics,
        "regimes_found": sorted(by_regime.keys()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "This engine calculates metrics from supplied trade results.",
            "It does not execute MQL5 code.",
            "It does not claim that the supplied trades came from the EA unless the source is documented.",
            "Regime labels must be supplied by the data producer; this version does not infer volatility regimes.",
        ],
    }
