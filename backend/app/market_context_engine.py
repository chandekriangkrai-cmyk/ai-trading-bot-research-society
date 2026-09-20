from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from app.market_data_engine import add_volatility_regimes, parse_ohlc_csv, regime_for_time


def _ema(values: list[float], period: int) -> list[float | None]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out: list[float | None] = [None] * len(values)
    ema = values[0]
    out[0] = ema
    for i in range(1, len(values)):
        ema = alpha * values[i] + (1.0 - alpha) * ema
        out[i] = ema
    return out


def _atr14(high: list[float], low: list[float], close: list[float]) -> list[float | None]:
    tr: list[float] = []
    for i in range(len(close)):
        if i == 0:
            tr.append(high[i] - low[i])
        else:
            tr.append(max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1])))
    out: list[float | None] = [None] * len(close)
    if len(tr) < 14:
        return out
    first = sum(tr[:14]) / 14.0
    out[13] = first
    prev = first
    for i in range(14, len(tr)):
        prev = ((prev * 13.0) + tr[i]) / 14.0
        out[i] = prev
    return out


def build_context_bars(ohlc_csv: bytes) -> list[dict[str, Any]]:
    bars = parse_ohlc_csv(ohlc_csv)
    if not bars:
        raise ValueError("OHLC CSV is empty")

    enriched = add_volatility_regimes(bars)
    closes = [float(b["close"]) for b in enriched]
    highs = [float(b["high"]) for b in enriched]
    lows = [float(b["low"]) for b in enriched]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    atr = _atr14(highs, lows, closes)

    result: list[dict[str, Any]] = []
    for i, bar in enumerate(enriched):
        c = closes[i]
        e20 = ema20[i]
        e50 = ema50[i]
        a = atr[i]
        if e20 is None or e50 is None:
            trend = "unknown"
        elif e20 > e50 and c > e20:
            trend = "up"
        elif e20 < e50 and c < e20:
            trend = "down"
        else:
            trend = "range"

        # Research proxy only: distance of the close from the prior 20-bar range,
        # scaled by ATR. It is NOT the EA's internal breakout level.
        start = max(0, i - 20)
        prior_highs = highs[start:i]
        prior_lows = lows[start:i]
        if a and a > 0 and prior_highs and prior_lows:
            prior_high = max(prior_highs)
            prior_low = min(prior_lows)
            breakout_distance_atr = max(
                (c - prior_high) / a,
                (prior_low - c) / a,
                0.0,
            )
        else:
            breakout_distance_atr = None

        result.append({
            **bar,
            "trend": trend,
            "ema20": e20,
            "ema50": e50,
            "atr14": a,
            "breakout_distance_atr": breakout_distance_atr,
        })
    return result


def _to_utc(dt: datetime, input_timezone: str) -> datetime:
    tz = ZoneInfo(input_timezone)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo("UTC"))


def session_for_time(dt: datetime, input_timezone: str = "UTC") -> str:
    utc = _to_utc(dt, input_timezone)
    hour = utc.hour + utc.minute / 60.0
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 17:
        return "london_ny_overlap"
    if 17 <= hour < 22:
        return "new_york"
    return "late_us"


def context_for_time(
    bars: list[dict[str, Any]],
    dt: datetime,
    input_timezone: str = "UTC",
) -> dict[str, Any]:
    # Use the latest bar at or before the timestamp; no future bar is consulted.
    chosen = None
    for bar in bars:
        if bar["time"] <= dt:
            chosen = bar
        else:
            break
    if chosen is None:
        return {
            "volatility_regime": "unknown",
            "trend": "unknown",
            "session": session_for_time(dt, input_timezone),
            "breakout_distance_atr": None,
            "atr14": None,
        }
    return {
        "volatility_regime": regime_for_time(bars, dt),
        "trend": chosen.get("trend", "unknown"),
        "session": session_for_time(dt, input_timezone),
        "breakout_distance_atr": chosen.get("breakout_distance_atr"),
        "atr14": chosen.get("atr14"),
    }
