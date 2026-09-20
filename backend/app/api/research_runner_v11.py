from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.market_context_engine import build_context_bars, context_for_time
from app.mt5_deals_importer import parse_mt5_deals_csv, _pair_fifo
from app.research_models import Experiment, ExperimentResult

router = APIRouter(
    prefix="/research/experiments",
    tags=["Research Engine v11 Sizing Simulation"],
)

POLICIES: dict[str, dict[str, float]] = {
    "flat": {"low": 1.0, "normal": 1.0, "high": 1.0, "unknown": 1.0},
    "high_defensive": {"low": 1.0, "normal": 1.0, "high": 0.75, "unknown": 1.0},
    "low_defensive": {"low": 0.75, "normal": 1.0, "high": 1.0, "unknown": 1.0},
    "high_low_defensive": {"low": 0.75, "normal": 1.0, "high": 0.75, "unknown": 1.0},
}


def _metrics(profits: list[float]) -> dict[str, Any]:
    n = len(profits)
    if n == 0:
        return {
            "trade_count": 0,
            "net_profit": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "expectancy": 0.0,
            "win_rate": 0.0,
            "max_drawdown_absolute": 0.0,
        }
    gp = sum(p for p in profits if p > 0)
    gl = sum(p for p in profits if p < 0)
    equity = peak = max_dd = 0.0
    for p in profits:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trade_count": n,
        "net_profit": sum(profits),
        "gross_profit": gp,
        "gross_loss": gl,
        "profit_factor": gp / abs(gl) if gl < 0 else None,
        "expectancy": sum(profits) / n,
        "win_rate": sum(1 for p in profits if p > 0) / n,
        "max_drawdown_absolute": max_dd,
    }


def _return_dd(metrics: dict[str, Any]) -> float | None:
    dd = float(metrics.get("max_drawdown_absolute") or 0.0)
    if dd <= 0:
        return None
    return float(metrics.get("net_profit") or 0.0) / dd


def _attach_entry_regime(closed: list[dict[str, Any]], bars: list[dict[str, Any]], timezone: str) -> None:
    for trade in closed:
        ctx = context_for_time(bars, trade["entry_time"], input_timezone=timezone)
        trade["entry_context"] = ctx
        trade["entry_volatility_regime"] = ctx.get("volatility_regime", "unknown")


def _simulate(closed: list[dict[str, Any]], policy: dict[str, float]) -> dict[str, Any]:
    scaled = []
    for trade in closed:
        regime = str(trade.get("entry_volatility_regime", "unknown"))
        multiplier = float(policy.get(regime, 1.0))
        scaled.append({
            **trade,
            "sizing_multiplier": multiplier,
            "scaled_profit": float(trade["profit"]) * multiplier,
            "scaled_raw_profit": float(trade["raw_profit"]) * multiplier,
            "scaled_commission": float(trade["commission"]) * multiplier,
            "scaled_swap": float(trade["swap"]) * multiplier,
        })

    metrics = _metrics([float(t["scaled_profit"]) for t in scaled])
    metrics["return_over_drawdown"] = _return_dd(metrics)

    by_regime: dict[str, list[float]] = defaultdict(list)
    for t in scaled:
        by_regime[str(t["entry_volatility_regime"])].append(float(t["scaled_profit"]))

    regime_metrics = {}
    for regime, profits in sorted(by_regime.items()):
        regime_metrics[regime] = _metrics(profits)
        regime_metrics[regime]["return_over_drawdown"] = _return_dd(regime_metrics[regime])

    return {
        "policy": policy,
        "overall": metrics,
        "by_entry_volatility": regime_metrics,
        "accounting": {
            "scaled_raw_profit": sum(float(t["scaled_raw_profit"]) for t in scaled),
            "scaled_commission": sum(float(t["scaled_commission"]) for t in scaled),
            "scaled_swap": sum(float(t["scaled_swap"]) for t in scaled),
            "scaled_realized_net_profit": sum(float(t["scaled_profit"]) for t in scaled),
            "linear_scaling_assumption": True,
        },
    }


def _year_of(dt: datetime) -> str:
    return str(dt.year)


def _policy_result(closed: list[dict[str, Any]], policy: dict[str, float]) -> dict[str, Any]:
    result = _simulate(closed, policy)
    by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in closed:
        by_year[_year_of(trade["entry_time"])].append(trade)
    result["by_entry_year"] = {
        year: _simulate(trades, policy)["overall"]
        for year, trades in sorted(by_year.items())
    }
    return result


@router.post("/{experiment_id}/regime-sizing-simulation")
async def regime_sizing_simulation(
    experiment_id: str,
    deals_file: UploadFile = File(...),
    market_file: UploadFile = File(...),
    input_timezone: str = Query("UTC"),
    policies: str = Query(
        "flat,high_defensive,low_defensive,high_low_defensive",
        description="Comma-separated pre-registered policy names; no automatic winner selection.",
    ),
):
    db: Session = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        if not deals_file.filename or not deals_file.filename.lower().endswith(".csv"):
            raise HTTPException(status_code=400, detail="deals_file must be CSV")
        if not market_file.filename or not market_file.filename.lower().endswith(".csv"):
            raise HTTPException(status_code=400, detail="market_file must be CSV")

        selected = [p.strip() for p in policies.split(",") if p.strip()]
        unknown = [p for p in selected if p not in POLICIES]
        if not selected or unknown:
            raise HTTPException(
                status_code=400,
                detail={"unknown_policies": unknown, "available": sorted(POLICIES)},
            )

        deal_bytes = await deals_file.read()
        market_bytes = await market_file.read()
        try:
            deals = parse_mt5_deals_csv(deal_bytes, symbol=experiment.symbol)
            closed = _pair_fifo(deals)
            bars = build_context_bars(market_bytes)
            _attach_entry_regime(closed, bars, input_timezone)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        results = {name: _policy_result(closed, POLICIES[name]) for name in selected}
        baseline = results.get("flat") or _policy_result(closed, POLICIES["flat"])

        analysis = {
            "experiment_scope": {
                "symbol": experiment.symbol,
                "timeframe": experiment.timeframe,
                "sizing_context": "entry-time volatility regime",
                "input_timezone": input_timezone,
                "policy_selection": "pre-registered fixed policies; no automatic winner selection",
            },
            "baseline_flat": baseline,
            "policies": results,
            "comparison": {
                name: {
                    "delta_net_profit_vs_flat": result["overall"]["net_profit"] - baseline["overall"]["net_profit"],
                    "delta_max_drawdown_vs_flat": result["overall"]["max_drawdown_absolute"] - baseline["overall"]["max_drawdown_absolute"],
                    "delta_return_over_drawdown_vs_flat": (
                        (result["overall"].get("return_over_drawdown") or 0.0)
                        - (baseline["overall"].get("return_over_drawdown") or 0.0)
                    ),
                    "profit_factor": result["overall"]["profit_factor"],
                }
                for name, result in results.items()
            },
            "method": {
                "volatility": "ATR(14) rolling percentile: bottom third=low, middle third=normal, top third=high",
                "sizing": "realized trade P/L and transaction costs are linearly scaled by the pre-registered regime multiplier",
                "entry_timestamp": "reconstructed MT5 in-deal timestamp",
            },
            "limitations": [
                "This is a sizing simulation over realized MT5 trades; it does not execute the MQL5 EA again.",
                "Linear scaling assumes P/L, commission and swap scale approximately with position size; margin, stop-distance, slippage and nonlinear broker effects are not re-simulated.",
                "The external ATR regime is a research label, not the EA's internal volatility logic.",
                "Policies are intentionally fixed before comparing results; this endpoint does not select a winning policy or optimize multipliers.",
                "A positive result on the same 2024/2025 sample is not evidence of future robustness; additional unseen periods and actual MQL5 execution are required.",
                "Small regime cells can be unstable and should not be interpreted as standalone evidence of an edge.",
            ],
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

        result = ExperimentResult(
            id=str(uuid.uuid4()),
            experiment_id=experiment.id,
            summary="Regime-conditioned position-sizing simulation over accounting-aware MT5 trades.",
            metrics=json.dumps(analysis, ensure_ascii=False, default=str),
            evidence=json.dumps({
                "deals_filename": deals_file.filename,
                "market_filename": market_file.filename,
                "policies": selected,
                "source_type": "user_supplied_mt5_deals_and_ohlc",
            }, ensure_ascii=False),
            limitations=json.dumps(analysis["limitations"], ensure_ascii=False),
            conclusion="Sizing policies simulated; no policy is declared superior by the engine.",
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
