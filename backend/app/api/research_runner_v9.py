from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.market_context_engine import build_context_bars, context_for_time
from app.mt5_deals_importer import parse_mt5_deals_csv, _pair_fifo
from app.research_models import Experiment, ExperimentResult

router = APIRouter(prefix="/research", tags=["Research Engine"])


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    profits = [float(t["profit"]) for t in trades]
    n = len(profits)
    if not n:
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


def _group(trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        groups[str(t.get(key, "unknown"))].append(t)
    return {k: _metrics(v) for k, v in sorted(groups.items())}


def _attach_entry_context(closed: list[dict[str, Any]], bars: list[dict[str, Any]], input_timezone: str) -> None:
    for trade in closed:
        ctx = context_for_time(bars, trade["entry_time"], input_timezone=input_timezone)
        for key, value in ctx.items():
            trade[f"entry_{key}"] = value


def _prepare(deal_bytes: bytes, market_bytes: bytes, symbol: str, input_timezone: str) -> list[dict[str, Any]]:
    deals = parse_mt5_deals_csv(deal_bytes, symbol=symbol)
    closed = _pair_fifo(deals)
    bars = build_context_bars(market_bytes)
    _attach_entry_context(closed, bars, input_timezone)
    return closed


def _segmentation(trades: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "by_entry_volatility": _group(trades, "entry_volatility_regime"),
        "by_entry_trend": _group(trades, "entry_trend"),
        "by_entry_session": _group(trades, "entry_session"),
        "entry_volatility_x_trend": _group(
            [{**t, "combo": f"{t.get('entry_volatility_regime')}|{t.get('entry_trend')}"} for t in trades],
            "combo",
        ),
        "entry_volatility_x_session": _group(
            [{**t, "combo": f"{t.get('entry_volatility_regime')}|{t.get('entry_session')}"} for t in trades],
            "combo",
        ),
    }


def _compare_groups(is_groups: dict[str, Any], oos_groups: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(is_groups) | set(oos_groups))
    out: dict[str, Any] = {}
    for key in keys:
        ins = is_groups.get(key, {})
        oos = oos_groups.get(key, {})
        if not ins or not oos:
            out[key] = {
                "is": ins or None,
                "oos": oos or None,
                "present_both_periods": False,
                "net_profit_same_sign": None,
                "pf_gt_1_both": None,
            }
            continue
        is_net = float(ins["net_profit"])
        oos_net = float(oos["net_profit"])
        is_pf = ins["profit_factor"]
        oos_pf = oos["profit_factor"]
        out[key] = {
            "is": ins,
            "oos": oos,
            "present_both_periods": True,
            "net_profit_same_sign": (is_net > 0 and oos_net > 0) or (is_net < 0 and oos_net < 0),
            "pf_gt_1_both": (
                is_pf is not None and oos_pf is not None and
                float(is_pf) > 1.0 and float(oos_pf) > 1.0
            ),
        }
    return out


def _accounting(trades: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "raw_profit": sum(float(t["raw_profit"]) for t in trades),
        "commission": sum(float(t["commission"]) for t in trades),
        "swap": sum(float(t["swap"]) for t in trades),
        "realized_net_profit": sum(float(t["profit"]) for t in trades),
        "closed_trade_count": len(trades),
        "matching_method": "FIFO by symbol when MT5 export has no position ID",
    }


@router.post("/experiments/{experiment_id}/walk-forward-entry-context")
async def walk_forward_entry_context(
    experiment_id: str,
    is_deals_file: UploadFile = File(...),
    is_market_file: UploadFile = File(...),
    oos_deals_file: UploadFile = File(...),
    oos_market_file: UploadFile = File(...),
    input_timezone: str = Query("UTC", description="Timezone of timestamps in both MT5 exports"),
    db: Session = Depends(get_db),
):
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    files = [is_deals_file, is_market_file, oos_deals_file, oos_market_file]
    if any(not f.filename or not f.filename.lower().endswith(".csv") for f in files):
        raise HTTPException(status_code=400, detail="All uploaded files must be CSV")

    is_deals = await is_deals_file.read()
    is_market = await is_market_file.read()
    oos_deals = await oos_deals_file.read()
    oos_market = await oos_market_file.read()

    try:
        is_trades = _prepare(is_deals, is_market, experiment.symbol, input_timezone)
        oos_trades = _prepare(oos_deals, oos_market, experiment.symbol, input_timezone)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    is_seg = _segmentation(is_trades)
    oos_seg = _segmentation(oos_trades)

    comparisons = {}
    for name in is_seg:
        comparisons[name] = _compare_groups(is_seg[name], oos_seg[name])

    analysis = {
        "experiment_scope": {
            "symbol": experiment.symbol,
            "timeframe": experiment.timeframe,
            "validation_type": "walk-forward year generalization",
            "is_period": "2024",
            "oos_period": "2025",
            "context_at": "entry-trade timestamp",
            "session_timezone": input_timezone,
            "parameter_adjustment": "none",
        },
        "is_2024": {
            "overall": _metrics(is_trades),
            "segmentation": is_seg,
            "accounting": _accounting(is_trades),
        },
        "oos_2025": {
            "overall": _metrics(oos_trades),
            "segmentation": oos_seg,
            "accounting": _accounting(oos_trades),
        },
        "generalization": comparisons,
        "method": {
            "volatility": "ATR(14) rolling percentile: bottom third=low, middle third=normal, top third=high",
            "trend": "EMA20/EMA50 plus price confirmation: up, down, or range",
            "session": "UTC buckets: Asia 00-07, London 07-12, London/NY overlap 12-17, New York 17-22, late US 22-24",
            "timestamp": "context is attached at reconstructed trade ENTRY timestamp",
        },
        "limitations": [
            "This endpoint does not execute the MQL5 EA; it evaluates realized MT5 trades.",
            "2024 is treated as IS/development and 2025 as OOS/generalization; no parameter adjustment is performed by this endpoint.",
            "Entry timestamp is taken from the matched MT5 in-deal. Without position ID, matching remains FIFO by symbol.",
            "Volatility and trend are externally defined research labels, not the EA's internal logic.",
            "A subgroup passing both periods is not proof of future performance; sample size and multiple subgroup testing remain limitations.",
        ],
    }

    result = ExperimentResult(
        id=str(uuid.uuid4()),
        experiment_id=experiment.id,
        summary=f"Walk-forward entry-context generalization: 2024 IS vs 2025 OOS for {experiment.symbol} {experiment.timeframe}.",
        metrics=json.dumps(analysis, ensure_ascii=False, default=str),
        evidence=json.dumps({
            "is_deals_filename": is_deals_file.filename,
            "is_market_filename": is_market_file.filename,
            "oos_deals_filename": oos_deals_file.filename,
            "oos_market_filename": oos_market_file.filename,
            "source_type": "user_supplied_mt5_deals_and_ohlc",
            "input_timezone": input_timezone,
            "context_timestamp": "entry_time",
        }, ensure_ascii=False),
        limitations=json.dumps(analysis["limitations"], ensure_ascii=False),
        conclusion=(
            "Deterministic 2024-to-2025 walk-forward comparison of entry-time context. "
            "The result distinguishes patterns that reproduce across both periods from patterns that do not."
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
