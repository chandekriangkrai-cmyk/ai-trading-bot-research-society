from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.mt5_deals_importer import analyze_mt5_deals
from app.research_models import Experiment, ExperimentResult

router = APIRouter(prefix="/research", tags=["Research Engine"])


@router.post("/experiments/{experiment_id}/import-mt5-deals")
async def import_mt5_deals(
    experiment_id: str,
    deals_file: UploadFile = File(...),
    market_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    if not deals_file.filename or not deals_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="deals_file must be CSV")

    if market_file is not None and market_file.filename and not market_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="market_file must be CSV")

    deal_bytes = await deals_file.read()
    market_bytes = await market_file.read() if market_file is not None else None

    try:
        analysis = analyze_mt5_deals(
            deal_csv=deal_bytes,
            ohlc_csv=market_bytes,
            symbol=experiment.symbol,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    analysis["experiment_scope"] = {
        "symbol": experiment.symbol,
        "timeframe": experiment.timeframe,
    }

    result = ExperimentResult(
        id=str(uuid.uuid4()),
        experiment_id=experiment.id,
        summary=(
            f"Imported {analysis['overall']['trade_count']} reconstructed closed trades "
            f"from MT5 Deals with commission/swap accounting."
        ),
        metrics=json.dumps(analysis, ensure_ascii=False, default=str),
        evidence=json.dumps(
            {
                "deals_filename": deals_file.filename,
                "market_filename": market_file.filename if market_file else None,
                "source_type": "user_supplied_mt5_deals_csv",
                "accounting_method": analysis["accounting"]["method"],
                "matching_method": analysis["accounting"]["matching_method"],
            },
            ensure_ascii=False,
        ),
        limitations=json.dumps(analysis.get("limitations", []), ensure_ascii=False),
        conclusion=(
            "Deterministic realized P/L metrics were reconstructed from supplied MT5 Deals. "
            "This is evidence from exported trades, not an independent MQL5 execution."
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
