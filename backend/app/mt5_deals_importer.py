from __future__ import annotations

import csv
import io
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from app.backtest_runner import analyze_trades
from app.market_data_engine import add_volatility_regimes, parse_ohlc_csv, regime_for_time


def _norm_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ")


def _parse_float(value: Any, default: float = 0.0) -> float:
    if value is None or str(value).strip() == "":
        return default
    return float(str(value).strip().replace(",", ""))


def _parse_time(value: Any) -> datetime:
    text = str(value).strip()
    for fmt in (
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f"Unsupported MT5 deal datetime: {text}")


def _find_key(fields: set[str], names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in fields:
            return name
    return None


def parse_mt5_deals_csv(raw: bytes, symbol: str | None = None) -> list[dict[str, Any]]:
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("MT5 Deals CSV is empty")

    normalized = []
    for row in rows:
        normalized.append({_norm_key(k): v for k, v in row.items()})

    fields = set(normalized[0].keys())
    time_key = _find_key(fields, ("time", "date", "datetime", "close time"))
    direction_key = _find_key(fields, ("direction", "entry"))
    symbol_key = _find_key(fields, ("symbol",))
    volume_key = _find_key(fields, ("volume", "lots", "size"))
    profit_key = _find_key(fields, ("profit", "profit/loss", "p/l", "pnl"))
    commission_key = _find_key(fields, ("commission",))
    swap_key = _find_key(fields, ("swap",))

    missing = [name for name, key in {
        "time": time_key,
        "direction": direction_key,
        "volume": volume_key,
        "profit": profit_key,
    }.items() if not key]
    if missing:
        raise ValueError(
            "MT5 Deals CSV missing required columns: " + ", ".join(missing)
        )

    wanted_symbol = str(symbol).strip().upper() if symbol else None
    deals: list[dict[str, Any]] = []

    for row in normalized:
        row_symbol = str(row.get(symbol_key, "")).strip().upper() if symbol_key else ""
        if wanted_symbol and row_symbol != wanted_symbol:
            continue

        direction = str(row.get(direction_key, "")).strip().lower()
        if direction not in {"in", "out"}:
            # Ignore balance/credit/other non-position rows.
            continue

        deals.append({
            "time": _parse_time(row[time_key]),
            "symbol": row_symbol,
            "direction": direction,
            "volume": _parse_float(row.get(volume_key)),
            "profit": _parse_float(row.get(profit_key)),
            "commission": _parse_float(row.get(commission_key)) if commission_key else 0.0,
            "swap": _parse_float(row.get(swap_key)) if swap_key else 0.0,
        })

    if not deals:
        raise ValueError("No position in/out deals found for the requested symbol")

    deals.sort(key=lambda x: x["time"])
    return deals


def _pair_fifo(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair MT5 in/out deals using FIFO by symbol when no position ID is exported."""
    opens: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    closed: list[dict[str, Any]] = []

    for deal in deals:
        symbol = deal["symbol"] or "UNKNOWN"
        if deal["direction"] == "in":
            opens[symbol].append(deal.copy())
            continue

        remaining = deal["volume"]
        if remaining <= 0:
            raise ValueError("MT5 out deal has non-positive volume")

        while remaining > 1e-12:
            if not opens[symbol]:
                raise ValueError(
                    f"MT5 deal matching failed: out deal at {deal['time']} "
                    f"has no open position for {symbol}"
                )

            entry = opens[symbol][0]
            matched = min(remaining, entry["volume"])
            entry_ratio = matched / entry["volume"]
            exit_ratio = matched / deal["volume"]

            # Allocate entry costs/profit proportionally for partial closes.
            realized = (
                entry["profit"] * entry_ratio
                + entry["commission"] * entry_ratio
                + entry["swap"] * entry_ratio
                + deal["profit"] * exit_ratio
                + deal["commission"] * exit_ratio
                + deal["swap"] * exit_ratio
            )

            closed.append({
                "time": deal["time"],
                "entry_time": entry["time"],
                "exit_time": deal["time"],
                "profit": realized,
                "raw_profit": entry["profit"] * entry_ratio + deal["profit"] * exit_ratio,
                "commission": entry["commission"] * entry_ratio + deal["commission"] * exit_ratio,
                "swap": entry["swap"] * entry_ratio + deal["swap"] * exit_ratio,
                "volume": matched,
                "accounting_method": "entry_profit_plus_exit_profit_plus_entry_commission_plus_exit_commission_plus_entry_swap_plus_exit_swap",
            })

            entry["volume"] -= matched
            remaining -= matched
            if entry["volume"] <= 1e-12:
                opens[symbol].popleft()

    unclosed = sum(len(q) for q in opens.values())
    if unclosed:
        raise ValueError(f"MT5 deal matching found {unclosed} unclosed entries")

    return closed


def analyze_mt5_deals(
    deal_csv: bytes,
    ohlc_csv: bytes | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    deals = parse_mt5_deals_csv(deal_csv, symbol=symbol)
    closed = _pair_fifo(deals)

    regime_bars = []
    if ohlc_csv:
        bars = parse_ohlc_csv(ohlc_csv)
        regime_bars = add_volatility_regimes(bars)

    normalized = []
    for trade in closed:
        regime = "unknown"
        if regime_bars:
            regime = regime_for_time(regime_bars, trade["time"])
        normalized.append({"profit": trade["profit"], "regime": regime})

    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=["profit", "regime"])
    writer.writeheader()
    writer.writerows(normalized)
    result = analyze_trades(csv_buffer.getvalue().encode("utf-8"))

    total_raw_profit = sum(t["raw_profit"] for t in closed)
    total_commission = sum(t["commission"] for t in closed)
    total_swap = sum(t["swap"] for t in closed)
    total_realized = sum(t["profit"] for t in closed)

    result["accounting"] = {
        "method": "closed trade realized P/L = entry profit + exit profit + entry commission + exit commission + entry swap + exit swap",
        "raw_profit": total_raw_profit,
        "commission": total_commission,
        "swap": total_swap,
        "realized_net_profit": total_realized,
        "closed_trade_count": len(closed),
        "matching_method": "FIFO by symbol when MT5 export has no position ID",
    }
    result["data_source"] = {
        "deal_rows": len(deals),
        "closed_trade_rows": len(closed),
        "ohlc_used": bool(ohlc_csv),
        "regime_method": (
            "ATR(14) rolling percentile: bottom third=low, middle third=normal, top third=high"
            if ohlc_csv else "not inferred; no OHLC supplied"
        ),
    }
    result["limitations"] = result.get("limitations", []) + [
        "This importer does not execute the MQL5 EA; it reconstructs realized trade P/L from supplied MT5 Deals data.",
        "Entry and exit costs are included in realized trade P/L when Commission and Swap columns are present.",
        "When MT5 export does not contain a position ID, in/out deals are matched FIFO by symbol; this is a documented reconstruction assumption.",
        "The volatility regime is an external deterministic research label from ATR(14), not proof of the EA's internal volatility logic.",
    ]
    return result
