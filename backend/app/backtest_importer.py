from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from app.backtest_runner import analyze_trades
from app.market_data_engine import (
    add_volatility_regimes,
    parse_ohlc_csv,
    regime_for_time,
)


def _parse_time(value: str) -> datetime:
    value = str(value).strip()
    formats = (
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    )
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(f"Unsupported datetime format: {value}")


def _float(value: str) -> float:
    return float(str(value).strip().replace(",", ""))


def parse_mt5_trade_csv(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("MT5 trade CSV is empty")

    normalized_rows = [
        {str(k).strip().lower(): v for k, v in row.items()}
        for row in rows
    ]

    fields = set(normalized_rows[0].keys())
    profit_key = next(
        (k for k in ("profit", "profit/loss", "p/l", "pnl") if k in fields),
        None,
    )
    if not profit_key:
        raise ValueError(
            "MT5 trade CSV needs a profit column "
            "(accepted: profit, profit/loss, p/l, pnl)"
        )

    time_key = next(
        (
            k
            for k in ("time", "close time", "closetime", "date", "datetime")
            if k in fields
        ),
        None,
    )

    trades = []
    for row in normalized_rows:
        item = {"profit": _float(row[profit_key])}
        if time_key and row.get(time_key):
            item["time"] = _parse_time(row[time_key])
        else:
            item["time"] = None
        trades.append(item)

    return trades


def analyze_mt5_trades(
    trade_csv: bytes,
    ohlc_csv: bytes | None = None,
) -> dict[str, Any]:
    trades = parse_mt5_trade_csv(trade_csv)

    regime_bars = []
    if ohlc_csv:
        bars = parse_ohlc_csv(ohlc_csv)
        regime_bars = add_volatility_regimes(bars)

    # Convert normalized trades back into the CSV shape expected by v2.
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["profit", "regime"])
    writer.writeheader()

    for trade in trades:
        regime = "unknown"
        if regime_bars and trade["time"] is not None:
            regime = regime_for_time(regime_bars, trade["time"])

        writer.writerow({
            "profit": trade["profit"],
            "regime": regime,
        })

    result = analyze_trades(
        output.getvalue().encode("utf-8")
    )

    result["data_source"] = {
        "trade_rows": len(trades),
        "ohlc_used": bool(ohlc_csv),
        "regime_method": (
            "ATR(14) rolling percentile: "
            "bottom third=low, middle third=normal, top third=high"
            if ohlc_csv
            else "not inferred; no OHLC supplied"
        ),
    }

    result["limitations"] = result.get("limitations", []) + [
        "This importer normalizes supplied MT5/export CSV data; it does not run the MQL5 EA.",
        "The exact MT5 export column names may vary; the importer accepts common profit/time names.",
        "If OHLC is supplied, volatility regime is inferred from ATR(14) over a rolling lookback of 100 bars.",
        "The inferred regime is a deterministic research label, not proof of the EA's internal regime logic.",
    ]

    return result
