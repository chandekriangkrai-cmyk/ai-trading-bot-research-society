from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Bar:
    time: datetime
    high: float
    low: float
    close: float


def _parse_float(value: str) -> float:
    return float(str(value).strip().replace(",", ""))


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


def parse_ohlc_csv(raw: bytes) -> list[Bar]:
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("OHLC CSV is empty")

    required = {"time", "high", "low", "close"}
    fields = {str(k).strip().lower() for k in rows[0].keys()}
    missing = required - fields
    if missing:
        raise ValueError(
            "OHLC CSV requires columns: time, high, low, close"
        )

    bars: list[Bar] = []
    for row in rows:
        normalized = {
            str(k).strip().lower(): v
            for k, v in row.items()
        }
        bars.append(
            Bar(
                time=_parse_time(normalized["time"]),
                high=_parse_float(normalized["high"]),
                low=_parse_float(normalized["low"]),
                close=_parse_float(normalized["close"]),
            )
        )

    bars.sort(key=lambda x: x.time)
    return bars


def _atr_values(bars: list[Bar], period: int = 14) -> list[float | None]:
    trs: list[float] = []
    prev_close: float | None = None

    for bar in bars:
        if prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        trs.append(tr)
        prev_close = bar.close

    out: list[float | None] = [None] * len(bars)
    for i in range(period - 1, len(bars)):
        window = trs[i - period + 1 : i + 1]
        out[i] = sum(window) / period
    return out


def add_volatility_regimes(
    bars: list[Bar],
    atr_period: int = 14,
    lookback: int = 100,
) -> list[dict[str, Any]]:
    if not bars:
        raise ValueError("No OHLC bars supplied")

    atrs = _atr_values(bars, atr_period)
    output: list[dict[str, Any]] = []

    for i, bar in enumerate(bars):
        atr = atrs[i]
        if atr is None:
            regime = "unknown"
        else:
            historical = [
                x for x in atrs[max(0, i - lookback + 1) : i + 1]
                if x is not None
            ]
            if len(historical) < max(20, atr_period):
                regime = "unknown"
            else:
                ordered = sorted(historical)
                rank = ordered.index(atr) / max(1, len(ordered) - 1)
                if rank < 1 / 3:
                    regime = "low"
                elif rank < 2 / 3:
                    regime = "normal"
                else:
                    regime = "high"

        output.append(
            {
                "time": bar.time,
                "atr": atr,
                "regime": regime,
            }
        )

    return output


def regime_for_time(
    regime_bars: list[dict[str, Any]],
    trade_time: datetime,
) -> str:
    selected = "unknown"
    for item in regime_bars:
        if item["time"] > trade_time:
            break
        selected = item["regime"]
    return selected
