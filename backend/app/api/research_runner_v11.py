from __future__ import annotations

import csv
import io
import json
import math
import uuid
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult


router = APIRouter(
    prefix="/research/experiments",
    tags=["Research Engine v11 Sizing"],
)


DEFAULT_POLICIES = (
    "flat",
    "high_defensive",
    "low_defensive",
    "high_low_defensive",
)


def _read_upload_bytes(upload: UploadFile) -> bytes:
    # The endpoint is async, but the actual uploaded bytes are small research CSVs.
    # FastAPI's UploadFile exposes a synchronous-compatible file object here.
    return upload.file.read()


def _load_csv_bytes(raw: bytes, filename: str) -> pd.DataFrame:
    if not raw:
        raise HTTPException(status_code=400, detail=f"Empty CSV upload: {filename}")

    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unable to parse CSV '{filename}': {exc}",
        ) from exc

    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def _require_columns(
    df: pd.DataFrame,
    required: set[str],
    filename: str,
) -> None:
    missing = sorted(required - set(df.columns))
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CSV '{filename}' missing required columns: "
                + ", ".join(missing)
            ),
        )


def _parse_time_series(
    values: pd.Series,
    input_timezone: str,
) -> pd.Series:
    parsed = pd.to_datetime(values, errors="coerce")
    if parsed.isna().all():
        raise HTTPException(
            status_code=400,
            detail="No valid timestamps could be parsed from the supplied CSV.",
        )

    # Treat naive timestamps as the declared input timezone. Convert to UTC
    # so deals and market data can be aligned consistently.
    if getattr(parsed.dt, "tz", None) is None:
        try:
            parsed = parsed.dt.tz_localize(
                input_timezone,
                ambiguous="NaT",
                nonexistent="NaT",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid input_timezone '{input_timezone}': {exc}",
            ) from exc

    return parsed.dt.tz_convert("UTC")


def _build_market_regimes(
    market: pd.DataFrame,
    input_timezone: str,
) -> pd.DataFrame:
    _require_columns(
        market,
        {"time", "high", "low", "close"},
        "market",
    )

    m = market.copy()
    m["time"] = _parse_time_series(m["time"], input_timezone)
    for col in ("high", "low", "close"):
        m[col] = pd.to_numeric(m[col], errors="coerce")

    m = (
        m.dropna(subset=["time", "high", "low", "close"])
        .sort_values("time")
        .drop_duplicates("time", keep="last")
        .reset_index(drop=True)
    )

    if len(m) < 20:
        raise HTTPException(
            status_code=400,
            detail="Market CSV needs at least 20 valid OHLC rows for v11 sizing.",
        )

    previous_close = m["close"].shift(1)
    true_range = pd.concat(
        [
            m["high"] - m["low"],
            (m["high"] - previous_close).abs(),
            (m["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    m["atr_14"] = true_range.rolling(14, min_periods=14).mean()
    m["atr_pct"] = m["atr_14"] / m["close"].abs()

    # Fixed, deterministic percentile cutoffs from the supplied market sample.
    # This is a classification step, not parameter optimization.
    valid_vol = m["atr_pct"].dropna()
    if valid_vol.empty:
        raise HTTPException(
            status_code=400,
            detail="Unable to calculate ATR volatility from market CSV.",
        )

    low_cut = float(valid_vol.quantile(1 / 3))
    high_cut = float(valid_vol.quantile(2 / 3))

    def regime(x: Any) -> str:
        if pd.isna(x):
            return "unknown"
        x = float(x)
        if x <= low_cut:
            return "low"
        if x >= high_cut:
            return "high"
        return "normal"

    m["volatility_regime"] = m["atr_pct"].map(regime)
    return m[["time", "atr_14", "atr_pct", "volatility_regime"]].copy()


def _pair_realized_trades(
    deals: pd.DataFrame,
    input_timezone: str,
) -> pd.DataFrame:
    _require_columns(
        deals,
        {
            "time",
            "deal",
            "symbol",
            "type",
            "direction",
            "volume",
            "price",
            "commission",
            "swap",
            "profit",
        },
        "deals",
    )

    d = deals.copy()
    d["time"] = _parse_time_series(d["time"], input_timezone)

    for col in ("volume", "price", "commission", "swap", "profit"):
        d[col] = pd.to_numeric(d[col], errors="coerce")

    d["direction"] = d["direction"].astype(str).str.strip().str.lower()
    d["symbol"] = d["symbol"].astype(str).str.strip()

    d = (
        d.dropna(subset=["time", "symbol", "direction", "volume"])
        .sort_values(["time", "deal"], kind="stable")
        .reset_index(drop=True)
    )

    # MT5 netting-style reconstruction: an "in" deal opens exposure and an
    # "out" deal closes the oldest compatible open exposure (FIFO).
    # This keeps the reconstruction accounting-aware and does not invent trades.
    opens: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    trades: list[dict[str, Any]] = []

    for _, row in d.iterrows():
        direction = str(row["direction"])
        symbol = str(row["symbol"])
        volume = float(row["volume"] or 0.0)

        if volume <= 0:
            continue

        # Some MT5 exports use buy/sell for the transaction side and "in/out"
        # for direction. The supplied research CSV uses direction=in/out.
        if direction == "in":
            key = (symbol, str(row["type"]).strip().lower())
            opens[key].append(
                {
                    "entry_time": row["time"],
                    "entry_price": float(row["price"]),
                    "entry_volume": volume,
                    "entry_commission": float(row["commission"] or 0.0),
                    "entry_swap": float(row["swap"] or 0.0),
                    "symbol": symbol,
                    "type": str(row["type"]).strip().lower(),
                    "entry_deal": str(row["deal"]),
                }
            )
            continue

        if direction != "out":
            continue

        # For an exit, the compatible open side is the opposite trade type.
        exit_type = str(row["type"]).strip().lower()
        opposite = "sell" if exit_type == "buy" else "buy"
        key = (symbol, opposite)

        remaining = volume
        exit_commission = float(row["commission"] or 0.0)
        exit_swap = float(row["swap"] or 0.0)
        exit_profit = float(row["profit"] or 0.0)

        while remaining > 1e-12 and opens[key]:
            opened = opens[key][0]
            matched = min(remaining, float(opened["entry_volume"]))
            fraction = matched / float(row["volume"])

            trade_exit_profit = exit_profit * fraction
            trade_exit_commission = exit_commission * fraction
            trade_exit_swap = exit_swap * fraction

            pnl = (
                trade_exit_profit
                + float(opened["entry_commission"]) * (matched / opened["entry_volume"])
                + float(opened["entry_swap"]) * (matched / opened["entry_volume"])
                + trade_exit_commission
                + trade_exit_swap
            )

            trades.append(
                {
                    "entry_time": opened["entry_time"],
                    "exit_time": row["time"],
                    "symbol": symbol,
                    "type": opened["type"],
                    "volume": matched,
                    "entry_price": opened["entry_price"],
                    "exit_price": float(row["price"]),
                    "profit": pnl,
                    "entry_deal": opened["entry_deal"],
                    "exit_deal": str(row["deal"]),
                }
            )

            opened["entry_volume"] -= matched
            remaining -= matched
            if opened["entry_volume"] <= 1e-12:
                opens[key].popleft()

    result = pd.DataFrame(trades)
    if result.empty:
        raise HTTPException(
            status_code=400,
            detail="No completed MT5 trades could be reconstructed from deals.csv.",
        )

    return result.sort_values("entry_time").reset_index(drop=True)


def _attach_entry_regimes(
    trades: pd.DataFrame,
    market_regimes: pd.DataFrame,
) -> pd.DataFrame:
    t = trades.copy().sort_values("entry_time")
    m = market_regimes.copy().sort_values("time")

    merged = pd.merge_asof(
        t,
        m,
        left_on="entry_time",
        right_on="time",
        direction="backward",
    )

    merged["volatility_regime"] = merged["volatility_regime"].fillna("unknown")
    return merged


def _multiplier(policy: str, regime: str) -> float:
    # Pre-registered, fixed policies. The engine never chooses a winner.
    if policy == "flat":
        return 1.0
    if policy == "high_defensive":
        return 0.5 if regime == "high" else 1.0
    if policy == "low_defensive":
        return 0.5 if regime == "low" else 1.0
    if policy == "high_low_defensive":
        return 0.5 if regime in {"high", "low"} else 1.0

    raise ValueError(f"Unknown sizing policy: {policy}")


def _summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "trade_count": 0,
            "net_profit": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "win_rate": None,
            "average_trade": None,
            "max_drawdown": 0.0,
        }

    series = pd.Series(values, dtype=float)
    gross_profit = float(series[series > 0].sum())
    gross_loss_abs = float(-series[series < 0].sum())
    profit_factor = (
        gross_profit / gross_loss_abs if gross_loss_abs > 0 else None
    )

    equity = series.cumsum()
    running_max = equity.cummax()
    drawdown = equity - running_max

    return {
        "trade_count": int(len(series)),
        "net_profit": float(series.sum()),
        "gross_profit": gross_profit,
        "gross_loss": float(-gross_loss_abs),
        "profit_factor": profit_factor,
        "win_rate": float((series > 0).mean()),
        "average_trade": float(series.mean()),
        "max_drawdown": float(drawdown.min()),
    }


def _simulate_policy(
    trades: pd.DataFrame,
    policy: str,
) -> dict[str, Any]:
    adjusted: list[float] = []

    for _, row in trades.iterrows():
        regime = str(row.get("volatility_regime") or "unknown")
        factor = _multiplier(policy, regime)
        adjusted.append(float(row["profit"]) * factor)

    overall = _summarize(adjusted)

    by_regime: dict[str, Any] = {}
    for regime in ("low", "normal", "high", "unknown"):
        values = [
            float(row["profit"]) * _multiplier(policy, regime)
            for _, row in trades.iterrows()
            if str(row.get("volatility_regime") or "unknown") == regime
        ]
        if values:
            by_regime[regime] = _summarize(values)

    return {
        "policy": policy,
        "overall": overall,
        "by_entry_volatility": by_regime,
    }


def _parse_policies(raw: str) -> list[str]:
    selected = []
    for item in (raw or "").split(","):
        name = item.strip()
        if not name:
            continue
        if name not in DEFAULT_POLICIES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported v11 policy '{name}'. "
                    f"Allowed policies: {', '.join(DEFAULT_POLICIES)}"
                ),
            )
        if name not in selected:
            selected.append(name)

    if not selected:
        selected = list(DEFAULT_POLICIES)

    return selected


def _policy_comparison(
    policies: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for name, result in policies.items():
        overall = result["overall"]
        rows.append(
            {
                "policy": name,
                "trade_count": overall["trade_count"],
                "net_profit": overall["net_profit"],
                "profit_factor": overall["profit_factor"],
                "win_rate": overall["win_rate"],
                "average_trade": overall["average_trade"],
                "max_drawdown": overall["max_drawdown"],
            }
        )
    return rows


async def regime_sizing_simulation(
    experiment_id: str,
    deals_file: UploadFile = File(...),
    market_file: UploadFile = File(...),
    input_timezone: str = Query("UTC"),
    policies: str = Query(
        "flat,high_defensive,low_defensive,high_low_defensive",
        description=(
            "Comma-separated pre-registered policy names; "
            "no automatic winner selection."
        ),
    ),
):
    """
    v11 sizing simulation over realized MT5 trades.

    Important:
    - It does not rerun the MQL5 EA.
    - It does not optimize or select a winning policy.
    - It reconstructs completed trades from MT5 deals, includes entry/exit
      commission and swap, assigns an entry-time ATR volatility regime, and
      applies fixed pre-registered sizing multipliers.
    """
    db: Session = SessionLocal()

    try:
        experiment = (
            db.query(Experiment)
            .filter(Experiment.id == experiment_id)
            .first()
        )
        if not experiment:
            raise HTTPException(
                status_code=404,
                detail="Experiment not found",
            )

        selected = _parse_policies(policies)

        deals_raw = _read_upload_bytes(deals_file)
        market_raw = _read_upload_bytes(market_file)

        deals = _load_csv_bytes(
            deals_raw,
            deals_file.filename or "deals.csv",
        )
        market = _load_csv_bytes(
            market_raw,
            market_file.filename or "market.csv",
        )

        market_regimes = _build_market_regimes(
            market,
            input_timezone,
        )
        trades = _pair_realized_trades(
            deals,
            input_timezone,
        )
        trades = _attach_entry_regimes(
            trades,
            market_regimes,
        )

        policy_results: dict[str, dict[str, Any]] = {}
        for policy in selected:
            policy_results[policy] = _simulate_policy(
                trades,
                policy,
            )

        baseline = policy_results.get("flat")
        if baseline is None:
            # The baseline is always useful for comparison, even if the caller
            # explicitly requested a subset of defensive policies.
            baseline = _simulate_policy(trades, "flat")

        regime_counts = (
            trades["volatility_regime"]
            .value_counts(dropna=False)
            .to_dict()
        )

        analysis = {
            "experiment_id": experiment_id,
            "experiment_scope": {
                "sizing_context": "entry-time volatility regime",
                "source_type": "user_supplied_mt5_deals_and_ohlc",
                "ea_reexecution": False,
                "automatic_policy_selection": False,
            },
            "baseline_flat": baseline,
            "policies": policy_results,
            "comparison": _policy_comparison(policy_results),
            "method": {
                "trade_reconstruction": (
                    "FIFO pairing of MT5 in/out deals by symbol and "
                    "opposite trade type."
                ),
                "realized_trade_pnl": (
                    "Exit profit plus matched entry/exit commission and swap."
                ),
                "volatility_measure": "ATR(14) divided by close.",
                "regime_thresholds": "33rd and 67th percentiles of supplied market ATR percentage.",
                "sizing_multipliers": {
                    "flat": {"low": 1.0, "normal": 1.0, "high": 1.0},
                    "high_defensive": {"low": 1.0, "normal": 1.0, "high": 0.5},
                    "low_defensive": {"low": 0.5, "normal": 1.0, "high": 1.0},
                    "high_low_defensive": {"low": 0.5, "normal": 1.0, "high": 0.5},
                },
                "regime_trade_counts": {
                    str(k): int(v) for k, v in regime_counts.items()
                },
            },
            "limitations": [
                "This is a sizing simulation over realized MT5 trades; it does not rerun the MQL5 EA.",
                "Sizing multipliers are fixed pre-registered policies and are not optimized by the engine.",
                "No policy is declared superior automatically.",
                "ATR volatility regimes are research labels derived from the supplied OHLC data.",
                "The market sample determines the percentile cutoffs; this is a deterministic classification step, not parameter optimization.",
                "Partial/unmatched MT5 deals cannot be reconstructed as completed trades and are excluded.",
                "The simulation does not model spread/slippage beyond costs already represented in the supplied deals.",
            ],
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

        result = ExperimentResult(
            id=str(uuid.uuid4()),
            experiment_id=experiment.id,
            summary=(
                "Regime-conditioned position-sizing simulation over "
                "accounting-aware MT5 trades."
            ),
            metrics=json.dumps(
                analysis,
                ensure_ascii=False,
                default=str,
            ),
            evidence=json.dumps(
                {
                    "deals_filename": deals_file.filename,
                    "market_filename": market_file.filename,
                    "policies": selected,
                    "source_type": "user_supplied_mt5_deals_and_ohlc",
                },
                ensure_ascii=False,
            ),
            limitations=json.dumps(
                analysis["limitations"],
                ensure_ascii=False,
            ),
            conclusion=(
                "Sizing policies simulated; no policy is declared "
                "superior by the engine."
            ),
        )

        db.add(result)
        experiment.status = "completed"
        experiment.completed_at = datetime.utcnow()
        db.commit()
        db.refresh(result)

        return {
            "experiment_id": experiment.id,
            "result_id": result.id,
            "status": experiment.status,
            "analysis": analysis,
        }

    finally:
        db.close()
