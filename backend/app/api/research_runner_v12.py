from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.research_models import Experiment, ExperimentResult
from app.api.research_runner_v9 import _prepare, _segmentation, _metrics, _accounting


router = APIRouter(
    prefix="/research/experiments",
    tags=["Research Engine v12"],
)


@router.post("/{experiment_id}/unseen-year-validation")
async def unseen_year_validation(
    experiment_id: str,
    unseen_year: str = Query(..., min_length=4, max_length=4),
    unseen_deals_file: UploadFile = File(...),
    unseen_market_file: UploadFile = File(...),
    input_timezone: str = Query("UTC"),
    db: Session = Depends(get_db),
):
    """
    Blind/unseen-year validation.

    The endpoint does NOT optimize, filter, or modify the EA.
    It only compares a pre-existing historical research result against
    a newly supplied unseen-year MT5 trade set.
    """
    experiment = (
        db.query(Experiment)
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    if not unseen_deals_file.filename or not unseen_deals_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="unseen_deals_file must be CSV")
    if not unseen_market_file.filename or not unseen_market_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="unseen_market_file must be CSV")

    # Find the latest v9 walk-forward result already stored for this experiment.
    source = None
    for result in (
        db.query(ExperimentResult)
        .filter(ExperimentResult.experiment_id == experiment_id)
        .order_by(ExperimentResult.created_at.desc())
        .all()
    ):
        try:
            payload = json.loads(result.metrics or "{}")
        except Exception:
            continue
        if "is_2024" in payload and "oos_2025" in payload:
            source = result
            break

    if not source:
        raise HTTPException(
            status_code=409,
            detail="No existing 2024/2025 walk-forward result found for this experiment",
        )

    historical = json.loads(source.metrics)

    try:
        unseen_deals = await unseen_deals_file.read()
        unseen_market = await unseen_market_file.read()
        unseen_trades = _prepare(
            unseen_deals,
            unseen_market,
            experiment.symbol,
            input_timezone,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    unseen_overall = _metrics(unseen_trades)
    unseen_seg = _segmentation(unseen_trades)
    unseen_accounting = _accounting(unseen_trades)

    historical_2024 = historical.get("is_2024", {}).get("overall", {})
    historical_2025 = historical.get("oos_2025", {}).get("overall", {})

    analysis = {
        "experiment_scope": {
            "symbol": experiment.symbol,
            "timeframe": experiment.timeframe,
            "historical_periods": ["2024", "2025"],
            "unseen_period": unseen_year,
            "validation_type": "blind unseen-year validation",
            "context_at": "entry-trade timestamp",
            "parameter_adjustment": "none",
        },
        "historical": {
            "2024": historical_2024,
            "2025": historical_2025,
        },
        "unseen": {
            "year": unseen_year,
            "overall": unseen_overall,
            "segmentation": unseen_seg,
            "accounting": unseen_accounting,
        },
        "comparison": {
            "2024_net_profit": historical_2024.get("net_profit"),
            "2025_net_profit": historical_2025.get("net_profit"),
            "unseen_net_profit": unseen_overall.get("net_profit"),
            "2024_pf": historical_2024.get("profit_factor"),
            "2025_pf": historical_2025.get("profit_factor"),
            "unseen_pf": unseen_overall.get("profit_factor"),
            "unseen_positive": (unseen_overall.get("net_profit") or 0) > 0,
            "unseen_pf_gt_1": (
                unseen_overall.get("profit_factor") is not None
                and unseen_overall.get("profit_factor") > 1
            ),
        },
        "method": {
            "volatility": "ATR(14) rolling percentile: bottom third=low, middle third=normal, top third=high",
            "trend": "EMA20/EMA50 plus price confirmation: up, down, or range",
            "session": "UTC buckets: Asia 00-07, London 07-12, London/NY overlap 12-17, New York 17-22, late US 22-24",
            "timestamp": "context attached at reconstructed trade ENTRY timestamp",
            "accounting": "MT5 realized P/L including commission and swap",
        },
        "limitations": [
            "This endpoint does not execute the MQL5 EA; it evaluates supplied MT5 realized trades.",
            "The unseen year must not be used to choose parameters before this validation.",
            "External volatility/trend labels are research labels, not necessarily the EA's internal logic.",
            "One unseen year is stronger evidence than a reused sample but is not proof of future performance.",
            "Without position IDs, trade matching remains FIFO by symbol.",
        ],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    # Neutral status: this endpoint reports evidence and does not select a winner.
    result = ExperimentResult(
        id=str(uuid.uuid4()),
        experiment_id=experiment.id,
        summary="Blind unseen-year validation completed.",
        metrics=json.dumps(analysis, ensure_ascii=False),
        evidence=json.dumps({
            "source_result_id": source.id,
            "unseen_year": unseen_year,
            "deals_filename": unseen_deals_file.filename,
            "market_filename": unseen_market_file.filename,
        }, ensure_ascii=False),
        limitations=json.dumps(analysis["limitations"], ensure_ascii=False),
        conclusion="UNSEEN_YEAR_VALIDATED",
    )
    db.add(result)
    db.commit()
    db.refresh(result)

    return {
        "experiment_id": experiment.id,
        "result_id": result.id,
        "status": "completed",
        "analysis": analysis,
    }
