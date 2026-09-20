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
        return {"trade_count": 0, "net_profit": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
                "profit_factor": None, "expectancy": 0.0, "win_rate": 0.0,
                "max_drawdown_absolute": 0.0}
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


@router.post("/experiments/{experiment_id}/import-mt5-deals-context")
async def import_mt5_deals_context(
    experiment_id: str,
    deals_file: UploadFile = File(...),
    market_file: UploadFile = File(...),
    input_timezone: str = Query("UTC", description="Timezone of timestamps in the MT5 export, e.g. UTC or Europe/Helsinki"),
    db: Session = Depends(get_db),
):
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if not deals_file.filename or not deals_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="deals_file must be CSV")
    if not market_file.filename or not market_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="market_file must be CSV")

    deal_bytes = await deals_file.read()
    market_bytes = await market_file.read()

    try:
        # Parse and pair using the existing accounting-aware engine.
        deals = parse_mt5_deals_csv(deal_bytes, symbol=experiment.symbol)
        closed = _pair_fifo(deals)
        bars = build_context_bars(market_bytes)
        for t in closed:
            t.update(context_for_time(bars, t["time"], input_timezone=input_timezone))
    except (ValueError, KeyError, Exception) as exc:
        # Keep the public API error concise while preserving deterministic validation.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    analysis = {
        "experiment_scope": {
            "symbol": experiment.symbol,
            "timeframe": experiment.timeframe,
            "context_at": "closed-trade timestamp",
            "session_timezone": input_timezone,
        },
        "overall": _metrics(closed),
        "by_volatility": _group(closed, "volatility_regime"),
        "by_trend": _group(closed, "trend"),
        "by_session": _group(closed, "session"),
        "volatility_x_trend": _group(
            [{**t, "combo": f"{t.get('volatility_regime')}|{t.get('trend')}"} for t in closed], "combo"
        ),
        "volatility_x_session": _group(
            [{**t, "combo": f"{t.get('volatility_regime')}|{t.get('session')}"} for t in closed], "combo"
        ),
        "volatility_x_trend_x_session": _group(
            [{**t, "combo": f"{t.get('volatility_regime')}|{t.get('trend')}|{t.get('session')}"} for t in closed], "combo"
        ),
        "accounting": {
            "raw_profit": sum(float(t["raw_profit"]) for t in closed),
            "commission": sum(float(t["commission"]) for t in closed),
            "swap": sum(float(t["swap"]) for t in closed),
            "realized_net_profit": sum(float(t["profit"]) for t in closed),
            "closed_trade_count": len(closed),
            "matching_method": "FIFO by symbol when MT5 export has no position ID",
        },
        "method": {
            "volatility": "ATR(14) rolling percentile: bottom third=low, middle third=normal, top third=high",
            "trend": "EMA20/EMA50 plus price confirmation: up, down, or range",
            "breakout_distance": "max(close-prior20high, prior20low-close) / ATR14, floored at 0; research proxy, not EA internal breakout level",
            "session": "UTC buckets: Asia 00-07, London 07-12, London/NY overlap 12-17, New York 17-22, late US 22-24",
            "timestamp": "context is attached at reconstructed closed-trade timestamp because current MT5 export does not expose a position ID/entry timestamp in the normalized output",
        },
        "limitations": [
            "This importer does not execute the MQL5 EA; it reconstructs realized trade P/L from supplied MT5 Deals data.",
            "Context is descriptive at trade close time, not a causal entry-time filter. Use entry-level deal fields in a future revision if available.",
            "Session labels depend on the declared input timezone of the MT5 export.",
            "The volatility regime is externally defined by ATR(14), not the EA's internal volatility logic.",
            "The breakout-distance feature is a research proxy based on the prior 20-bar range, not a reconstruction of the EA's actual breakout level.",
            "Small cells can be unstable; do not treat a high profit factor in a tiny subgroup as evidence of a robust edge.",
        ],
    }

    result = ExperimentResult(
        id=str(uuid.uuid4()),
        experiment_id=experiment.id,
        summary=f"Accounting-aware market-context analysis for {experiment.symbol} {experiment.timeframe}.",
        metrics=json.dumps(analysis, ensure_ascii=False, default=str),
        evidence=json.dumps({
            "deals_filename": deals_file.filename,
            "market_filename": market_file.filename,
            "source_type": "user_supplied_mt5_deals_and_ohlc",
            "input_timezone": input_timezone,
        }, ensure_ascii=False),
        limitations=json.dumps(analysis["limitations"], ensure_ascii=False),
        conclusion=(
            "Deterministic accounting-aware performance was segmented by volatility, trend, "
            "and session context. This is a research segmentation, not a claim of causal edge."
        ),
    )
    db.add(result)
    experiment.status = "completed"
    experiment.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(result)

    return {"experiment_id": experiment.id, "result_id": result.id, "status": experiment.status, "analysis": analysis}
